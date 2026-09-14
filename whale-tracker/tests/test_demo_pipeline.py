"""Integration test: the synthetic universe must sort the way it was built.

This is the regression test for the whole stack — P&L, scoring and pattern
detection together. If a change makes a launch-block sniper outrank a steady
performer, this fails.
"""

from __future__ import annotations

import pytest

from whale_tracker.demo import seed_demo
from whale_tracker.patterns import analyse_patterns
from whale_tracker.pnl import rebuild_positions
from whale_tracker.scoring import score_all


@pytest.fixture(scope="module")
def universe(tmp_path_factory):
    from whale_tracker.db import init_db

    conn = init_db(tmp_path_factory.mktemp("demo") / "demo.db")
    summary = seed_demo(conn, tokens=8, seed=21, days=40)
    rebuild_positions(conn)
    flags = analyse_patterns(conn)
    score_all(conn)
    scores = {r["wallet"]: r for r in conn.execute("SELECT * FROM wallet_scores")}
    yield {"conn": conn, "summary": summary, "flags": flags, "scores": scores}
    conn.close()


def archetype(universe, name):
    return universe["summary"]["archetypes"][name]


def test_seeding_produces_a_dense_tape(universe):
    conn = universe["conn"]
    assert conn.execute("SELECT COUNT(*) AS c FROM tokens").fetchone()["c"] == 8
    assert conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"] > 10_000
    assert conn.execute("SELECT COUNT(*) AS c FROM wallet_token_pnl").fetchone()["c"] > 20


def test_consistent_wallets_beat_the_one_hit_wonder(universe):
    scores = universe["scores"]
    best_consistent = max(scores[w]["score"] for w in archetype(universe, "consistent"))
    one_shot = scores[archetype(universe, "oneshot")[0]]
    assert best_consistent > one_shot["score"]
    assert one_shot["single_outlier"] == 1
    assert one_shot["top1_profit_share"] > 0.6
    # The one-hit wonder made more money and is still ranked lower.
    assert one_shot["realised_pnl_usd"] > 0


def test_consistent_wallets_beat_the_loser(universe):
    scores = universe["scores"]
    loser = scores[archetype(universe, "loser")[0]]
    assert loser["win_rate"] == 0.0
    assert loser["max_drawdown_pct"] == pytest.approx(1.0)
    for wallet in archetype(universe, "consistent"):
        assert scores[wallet]["score"] > loser["score"]


def test_sniper_is_detected_and_down_ranked(universe):
    sniper = archetype(universe, "sniper")[0]
    flags = universe["flags"][sniper]
    scores = universe["scores"]
    assert flags.sniper_rate == pytest.approx(1.0)
    assert flags.launch_block_buys == 8
    assert flags.penalty >= 0.5
    assert any("launch-window" in reason for reason in flags.reasons)
    # Raw performance is stellar; the ranked score is not.
    assert scores[sniper]["raw_score"] > 50
    assert scores[sniper]["score"] < scores[sniper]["raw_score"] * 0.5
    assert max(scores[w]["score"] for w in archetype(universe, "consistent")) > scores[sniper]["score"]


def test_sybil_set_lands_in_one_cluster(universe):
    sybils = archetype(universe, "sybil")
    flags = universe["flags"]
    cluster_ids = {flags[w].cluster_id for w in sybils}
    assert len(cluster_ids) == 1 and None not in cluster_ids
    for wallet in sybils:
        assert flags[wallet].cluster_size == len(sybils)
        assert flags[wallet].lockstep_score > 0.6
        assert flags[wallet].penalty > 0


def test_ordinary_wallets_are_not_flagged(universe):
    flags = universe["flags"]
    for wallet in archetype(universe, "consistent"):
        assert flags[wallet].penalty == 0.0
        assert flags[wallet].cluster_id is None


def test_backtest_drag_is_visible_on_the_demo_universe(universe):
    from whale_tracker.backtest import BacktestConfig, run_backtest

    conn = universe["conn"]
    window = conn.execute("SELECT MIN(ts) AS a, MAX(ts) AS b FROM trades").fetchone()
    leaders = archetype(universe, "consistent")

    def simulate(latency, slippage):
        cfg = BacktestConfig(
            start_ts=window["a"],
            end_ts=window["b"],
            latency_seconds=latency,
            slippage=slippage,
            position_size_usd=100.0,
            starting_capital_usd=1_000.0,
            seed=5,
        )
        return run_backtest(conn, leaders, cfg, persist=False)

    ideal = simulate((0.0, 0.0), (0.0, 0.0))
    realistic = simulate((15.0, 30.0), (0.01, 0.03))

    assert realistic.metrics["trades"] == ideal.metrics["trades"] > 0
    assert realistic.metrics["net_pnl_usd"] < ideal.metrics["net_pnl_usd"]
    assert realistic.metrics["drag_usd"] > ideal.metrics["drag_usd"]
