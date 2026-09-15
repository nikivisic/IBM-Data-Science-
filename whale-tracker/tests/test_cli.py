"""End-to-end CLI smoke tests over a synthetic universe."""

from __future__ import annotations

import json

import pytest

from whale_tracker.cli import main, parse_range, parse_time


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    path = tmp_path / "cli.db"
    return ["--db", str(path), "--log-level", "ERROR", "--log-format", "console"]


def run(argv):
    return main(argv)


def test_parse_time_accepts_several_forms():
    assert parse_time("2025-06-01") == 1_748_736_000
    assert parse_time("1748736000") == 1_748_736_000
    assert parse_time("30d") < parse_time("1d")
    with pytest.raises(Exception):
        parse_time("last tuesday")


def test_parse_range():
    assert parse_range("15-30") == (15.0, 30.0)
    assert parse_range("20") == (20.0, 20.0)
    assert parse_range("3-1") == (1.0, 3.0)
    assert parse_range("1-3", scale=0.01) == (0.01, 0.03)


def test_init_db_and_status(db, capsys):
    assert run(db + ["init-db"]) == 0
    assert run(db + ["status"]) == 0
    out = capsys.readouterr().out
    assert "trades" in out and "MISSING" in out  # no keys configured in the test env


def test_full_pipeline(db, capsys):
    assert run(db + ["demo-seed", "--tokens", "5", "--seed", "3", "--days", "30"]) == 0
    seeded = capsys.readouterr().out
    assert "synthetic tokens" in seeded
    sniper = next(line.split()[-1] for line in seeded.splitlines() if line.strip().startswith("sniper"))
    consistent = next(
        line.split()[-1] for line in seeded.splitlines() if line.strip().startswith("consistent")
    )

    assert run(db + ["analyse"]) == 0
    assert "wallets scored" in capsys.readouterr().out

    # --- ranking ---
    assert run(db + ["rank", "--limit", "5", "--min-tokens", "2", "--min-closed", "2", "--json"]) == 0
    ranked = json.loads(capsys.readouterr().out)
    assert ranked, "expected ranked candidates"
    assert ranked == sorted(ranked, key=lambda r: -r["score"])
    assert {"wallet", "score", "win_rate", "median_roi", "penalty"} <= set(ranked[0])

    # The launch-block sniper must be penalised and explained.
    by_wallet = {r["wallet"]: r for r in
                 json.loads(_rank_all(db, capsys))}
    assert by_wallet[sniper]["penalty"] > 0
    assert json.loads(by_wallet[sniper]["reasons_json"])
    assert by_wallet[consistent]["penalty"] == 0

    # --- wallet inspection ---
    assert run(db + ["wallet", consistent, "--trades", "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["wallet"] == consistent
    assert detail["positions"] and detail["trades"]
    assert detail["score"]["closed_positions"] >= 1

    assert run(db + ["wallet", "NotAWalletThatExists"]) == 1
    capsys.readouterr()

    # --- backtest ---
    assert run(db + ["backtest", "--wallet", consistent, "--size", "100", "--capital", "1000",
                     "--latency", "15-30", "--slippage", "1-3", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["config"]["latency_seconds"] == [15.0, 30.0]
    assert result["config"]["slippage"] == [0.01, 0.03]
    assert result["metrics"]["trades"] >= 1
    assert "net_pnl_usd" in result["metrics"]
    run_id = result["run_id"]

    assert run(db + ["backtests", "--json"]) == 0
    runs = json.loads(capsys.readouterr().out)
    assert any(r["id"] == run_id for r in runs)

    # --- export ---
    assert run(db + ["export", "--table", "ranked", "--out", "out/ranked.csv"]) == 0
    assert "wrote" in capsys.readouterr().out


def _rank_all(db, capsys):
    run(db + ["rank", "--limit", "50", "--min-tokens", "1", "--min-closed", "1", "--json"])
    return capsys.readouterr().out


def test_ingest_without_tokens_is_rejected(db):
    with pytest.raises(SystemExit):
        run(db + ["ingest"])


def test_ingest_without_a_key_reports_config_error(db, capsys):
    assert run(db + ["ingest", "--token", "SomeMint"]) == 2
    assert "HELIUS_API_KEY" in capsys.readouterr().err


def test_backtest_without_wallets_is_rejected(db):
    run(db + ["init-db"])
    with pytest.raises(SystemExit):
        run(db + ["backtest"])


# ---------------------------------------------------------------------------
# budget controls and the free price layer
# ---------------------------------------------------------------------------


def test_ingest_dry_run_makes_no_calls_and_reports_credits(db, capsys):
    assert run(db + ["ingest", "--token", "MintA", "--token", "MintB", "--max-txs", "1000",
                     "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["dry_run"] is True
    # 2 tokens x 10 pages x 10 credits on the default (cheap) history
    # endpoint, plus the DAS metadata fallback.
    assert payload["totals"]["helius"]["credits"] == 2 * 10 * 10 + 2 * 10
    assert payload["totals"]["helius"]["requests"] == 2 * 10 + 2
    assert all(check["fits"] for check in payload["budget_check"])
    # No API key is configured in the test environment: a real run would have
    # failed, so reaching here proves nothing was called.


def test_ingest_dry_run_exits_nonzero_when_over_budget(db, capsys):
    assert run(db + ["ingest", "--token", "M1", "--token", "M2", "--token", "M3",
                     "--max-txs", "100000", "--history-strategy", "enhanced_tx",
                     "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "This run would breach" in out
    assert "credits/run" in out


def test_dry_run_respects_a_cap_override(db, capsys):
    assert run(db + ["ingest", "--token", "M1", "--max-txs", "1000",
                     "--max-credits", "100", "--dry-run"]) == 1
    assert "OVER" in capsys.readouterr().out


def test_expand_dry_run_counts_real_candidates(db, capsys):
    assert run(db + ["demo-seed", "--tokens", "4", "--seed", "8", "--days", "20"]) == 0
    capsys.readouterr()

    assert run(db + ["expand", "--dry-run", "--min-tokens", "2", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    wallets = int(payload["facts"]["candidate wallets"])
    assert wallets > 0
    assert payload["facts"]["transactions per wallet (cap)"] == "200"
    assert payload["totals"]["helius"]["requests"] == wallets * 2


def test_expand_fan_out_cap_shrinks_the_projection(db, capsys):
    run(db + ["demo-seed", "--tokens", "4", "--seed", "8", "--days", "20"])
    capsys.readouterr()

    run(db + ["expand", "--dry-run", "--min-tokens", "2", "--json"])
    wide = json.loads(capsys.readouterr().out)
    run(db + ["expand", "--dry-run", "--min-tokens", "2", "--max-wallets-per-token", "2",
              "--max-txs-per-wallet", "100", "--json"])
    narrow = json.loads(capsys.readouterr().out)

    assert int(narrow["facts"]["candidate wallets"]) < int(wide["facts"]["candidate wallets"])
    assert narrow["totals"]["helius"]["credits"] < wide["totals"]["helius"]["credits"]


def test_status_reports_month_to_date_usage(db, capsys):
    from whale_tracker.budget import utc_day
    from whale_tracker.db import init_db

    run(db + ["init-db"])
    capsys.readouterr()
    conn = init_db(db[1])
    conn.execute(
        "INSERT INTO api_usage(provider, day, requests, credits, cache_hits) "
        "VALUES ('helius', ?, 120, 12000, 4)",
        (utc_day(),),
    )
    conn.commit()
    conn.close()

    assert run(db + ["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    helius = next(row for row in payload["usage"] if row["provider"] == "helius")
    assert helius["mtd_credits"] == 12_000
    assert helius["today_requests"] == 120
    assert helius["monthly_budget"] == 1_000_000
    assert helius["monthly_pct"] == pytest.approx(0.012)
    assert payload["price_sources"] == ["jupiter", "dexscreener", "geckoterminal"]
    assert payload["budget_enforce"] is True

    assert run(db + ["status"]) == 0
    text = capsys.readouterr().out
    assert "month to date" in text
    assert "1.2%" in text


def test_price_sources_flag_overrides_the_configured_chain(db, capsys):
    assert run(db + ["ingest", "--token", "M1", "--dry-run",
                     "--price-sources", "geckoterminal", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    providers = {item["provider"] for item in payload["items"]}
    assert "dexscreener" not in providers
    assert "geckoterminal" in providers
    # With DexScreener out of the chain, metadata falls back to billed DAS.
    metadata = next(i for i in payload["items"] if i["label"] == "token metadata")
    assert metadata["provider"] == "helius"
    assert metadata["credits_each"] == 10
