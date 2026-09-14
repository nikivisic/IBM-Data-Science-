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
