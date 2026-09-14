"""Bait / insider / sybil detection."""

from __future__ import annotations

import pytest

from whale_tracker.db import insert_trades, upsert_token
from whale_tracker.patterns import (
    PatternConfig,
    UnionFind,
    analyse_patterns,
    binomial_sf,
    detect_insiders,
    detect_launch_snipers,
    detect_lockstep_clusters,
)
from whale_tracker.pnl import rebuild_positions


def test_binomial_tail():
    assert binomial_sf(10, 10, 0.5) == pytest.approx(0.5**10)
    assert binomial_sf(0, 10, 0.5) == pytest.approx(1.0)
    assert binomial_sf(9, 10, 0.5) == pytest.approx(0.0107421875)
    assert binomial_sf(5, 10, 0.5) > binomial_sf(8, 10, 0.5)


def test_union_find_groups_transitively():
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")
    assert uf.find("d") != uf.find("a")


def test_launch_sniper_detection():
    launches = {f"m{i}": {"launch_ts": 1_000, "launch_slot": 2_500} for i in range(4)}
    positions = []
    for i in range(4):
        positions.append(
            {"wallet": "sniper", "mint": f"m{i}", "first_buy_ts": 1_000, "first_buy_slot": 2_500}
        )
        # Late entrant: an hour after launch, hundreds of slots later.
        positions.append(
            {"wallet": "human", "mint": f"m{i}", "first_buy_ts": 4_600, "first_buy_slot": 11_500}
        )

    stats = detect_launch_snipers(positions, launches, PatternConfig())
    assert stats["sniper"]["sniper_rate"] == pytest.approx(1.0)
    assert stats["sniper"]["launch_block_buys"] == 4
    assert stats["human"]["sniper_rate"] == 0.0
    assert stats["human"]["median_entry_lag_seconds"] == pytest.approx(3_600)


def test_insider_flag_needs_statistical_weight():
    cfg = PatternConfig(min_positions_for_insider=5)
    # 6/6 against a 0.5 baseline is p=0.016 — suspicious.
    flagged = detect_insiders({"w": (6, 6)}, cfg, baseline=0.5)
    assert flagged["w"]["insider_suspicion"] >= 0.6
    # 4/6 is ordinary, and a short record is not assessed at all.
    ordinary = detect_insiders({"w": (4, 6)}, cfg, baseline=0.5)
    assert ordinary["w"]["insider_suspicion"] < 0.2
    assert detect_insiders({"w": (3, 3)}, cfg, baseline=0.5) == {}


def test_perfect_record_alone_is_not_enough():
    """6-for-6 against a 70% baseline happens by chance to 1 wallet in 8."""
    cfg = PatternConfig(min_positions_for_insider=5)
    result = detect_insiders({"w": (6, 6)}, cfg, baseline=0.7)
    assert result["w"]["insider_p_value"] > 0.05
    assert result["w"]["insider_suspicion"] < 0.2


def test_lockstep_cluster_groups_co_entrants():
    entries = {
        "m0": [("a", 100), ("b", 105), ("c", 100_000)],
        "m1": [("a", 200), ("b", 203), ("c", 200_000)],
        "m2": [("a", 300), ("b", 299), ("c", 300_000)],
        "m3": [("a", 400), ("b", 402), ("c", 400_000)],
    }
    clusters = detect_lockstep_clusters(entries, PatternConfig())
    assert clusters["a"]["cluster_id"] == clusters["b"]["cluster_id"]
    assert clusters["a"]["cluster_size"] == 2
    assert clusters["a"]["lockstep_score"] == pytest.approx(1.0)
    assert "c" not in clusters


def test_lockstep_ignores_coincidence_on_too_few_tokens():
    entries = {"m0": [("a", 100), ("b", 102)], "m1": [("a", 200), ("b", 205)]}
    assert detect_lockstep_clusters(entries, PatternConfig(min_shared_tokens=3)) == {}


def test_lockstep_ignores_wallets_that_merely_share_tokens():
    """Same tokens, hours apart: not one actor."""
    entries = {f"m{i}": [("a", i * 10_000), ("b", i * 10_000 + 7_200)] for i in range(5)}
    assert detect_lockstep_clusters(entries, PatternConfig()) == {}


def _seed(conn, trade_factory, wallet, mint, entry_ts, entry_slot, exit_ts, profit=True):
    value_out = 300.0 if profit else 40.0
    insert_trades(
        conn,
        [
            trade_factory(wallet, mint, "buy", 1_000, 100.0, entry_ts, slot=entry_slot),
            trade_factory(wallet, mint, "sell", 1_000, value_out, exit_ts),
        ],
    )


def test_analyse_patterns_end_to_end(conn, trade_factory):
    launch_ts, launch_slot = 1_000_000, 2_500_000
    for i in range(5):
        mint = f"mint{i}"
        upsert_token(
            conn,
            {"mint": mint, "launch_ts": launch_ts + i, "launch_slot": launch_slot + i},
        )
        # the token's own first trade
        insert_trades(
            conn,
            [trade_factory("deployer", mint, "buy", 5_000, 500.0, launch_ts + i, slot=launch_slot + i)],
        )
        # a sniper in the launch block, winning every time
        _seed(conn, trade_factory, "sniper", mint, launch_ts + i, launch_slot + i, launch_ts + 5_000)
        # two wallets entering seconds apart, token after token
        _seed(conn, trade_factory, "sybilA", mint, launch_ts + 3_600, launch_slot + 9_000, launch_ts + 7_200)
        _seed(conn, trade_factory, "sybilB", mint, launch_ts + 3_605, launch_slot + 9_012, launch_ts + 7_205)
        # an ordinary trader, entering at a random-ish time, mixed results
        _seed(
            conn, trade_factory, "human", mint, launch_ts + 20_000 + i * 900,
            launch_slot + 50_000, launch_ts + 40_000, profit=(i % 2 == 0),
        )
    conn.commit()
    rebuild_positions(conn)

    flags = analyse_patterns(conn, cfg=PatternConfig(min_positions_for_insider=5))

    assert flags["sniper"].sniper_rate == pytest.approx(1.0)
    assert flags["sniper"].launch_block_buys == 5
    assert flags["sniper"].penalty > 0.4
    assert any("launch-window" in r for r in flags["sniper"].reasons)

    assert flags["sybilA"].cluster_id == flags["sybilB"].cluster_id
    assert flags["sybilA"].cluster_size == 2
    assert flags["sybilA"].penalty > 0
    assert any("lockstep" in r for r in flags["sybilA"].reasons)

    assert flags["human"].penalty == 0.0
    assert flags["human"].reasons == []

    stored = {r["wallet"]: r for r in conn.execute("SELECT * FROM wallet_flags")}
    assert stored["sniper"]["penalty"] > 0
    assert stored["human"]["penalty"] == 0


def test_penalty_is_capped(conn, trade_factory):
    cfg = PatternConfig(max_penalty=0.9, min_positions_for_insider=3)
    launch_ts, launch_slot = 500_000, 1_250_000
    for i in range(6):
        mint = f"m{i}"
        upsert_token(conn, {"mint": mint, "launch_ts": launch_ts, "launch_slot": launch_slot})
        for wallet in ("bot1", "bot2", "bot3"):
            _seed(conn, trade_factory, wallet, mint, launch_ts, launch_slot, launch_ts + 600)
    conn.commit()
    rebuild_positions(conn)
    flags = analyse_patterns(conn, cfg=cfg)
    assert flags["bot1"].penalty == pytest.approx(0.9)  # 0.55 + 0.38, capped
    assert len(flags["bot1"].reasons) == 2
    # Nobody is an "insider" here: when every wallet in the population wins
    # every time, a perfect record is the baseline, not an anomaly.
    assert flags["bot1"].insider_suspicion < 0.2
