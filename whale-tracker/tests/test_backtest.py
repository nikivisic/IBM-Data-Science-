"""Copy-trade simulation: does latency and slippage show up in the number?"""

from __future__ import annotations

import pytest

from whale_tracker.backtest import BacktestConfig, PriceTape, run_backtest
from whale_tracker.db import insert_trades


def build_tape(conn, trade_factory, *, mint="m1", start=1_000, ticks=600, step=10):
    """A token that doubles over 100 minutes, with a trade every `step` seconds."""
    rows = []
    for i in range(ticks):
        ts = start + i * step
        price = 0.001 * (1 + i / ticks)
        rows.append(trade_factory("tape", mint, "buy" if i % 2 else "sell", 1_000, 1_000 * price, ts))
    insert_trades(conn, rows)
    conn.commit()
    return start, start + ticks * step


def leader_round_trip(conn, trade_factory, *, wallet="leader", mint="m1", entry_ts=2_000, exit_ts=4_000):
    entry_price = 0.001 * (1 + (entry_ts - 1_000) / 6_000)
    exit_price = 0.001 * (1 + (exit_ts - 1_000) / 6_000)
    insert_trades(
        conn,
        [
            trade_factory(wallet, mint, "buy", 100_000, 100_000 * entry_price, entry_ts),
            trade_factory(wallet, mint, "sell", 100_000, 100_000 * exit_price, exit_ts),
        ],
    )
    conn.commit()


def base_cfg(start, end, **kwargs):
    params = dict(
        start_ts=start,
        end_ts=end,
        latency_seconds=(0.0, 0.0),
        slippage=(0.0, 0.0),
        position_size_usd=100.0,
        starting_capital_usd=1_000.0,
        fee_bps=0.0,
        network_fee_usd=0.0,
        seed=42,
    )
    params.update(kwargs)
    return BacktestConfig(**params)


def test_price_tape_lookup():
    class Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    tape = PriceTape([Row(mint="m", ts=100, price_usd=1.0), Row(mint="m", ts=200, price_usd=2.0)])
    assert tape.price_at("m", 150, 60) == (200, 2.0)      # next trade after us
    assert tape.price_at("m", 210, 60) == (200, 2.0)      # nothing ahead: fall back
    assert tape.price_at("m", 500, 60) is None            # too stale to trust
    assert tape.last_before("m", 250) == (200, 2.0)
    assert tape.price_at("absent", 100, 60) is None


def test_zero_friction_copy_tracks_the_leader(conn, trade_factory):
    start, end = build_tape(conn, trade_factory)
    leader_round_trip(conn, trade_factory)
    result = run_backtest(conn, ["leader"], base_cfg(start, end), persist=False)

    assert len(result.trades) == 1
    trade = result.trades[0]
    leader_roi = trade.leader_exit_price / trade.leader_entry_price - 1
    assert trade.roi == pytest.approx(leader_roi, rel=1e-6)
    assert result.metrics["net_pnl_usd"] > 0
    assert result.metrics["drag_usd"] == pytest.approx(0.0, abs=1e-6)


def test_latency_and_slippage_cost_money(conn, trade_factory):
    start, end = build_tape(conn, trade_factory)
    leader_round_trip(conn, trade_factory)

    ideal = run_backtest(conn, ["leader"], base_cfg(start, end), persist=False)
    real = run_backtest(
        conn,
        ["leader"],
        base_cfg(start, end, latency_seconds=(15.0, 30.0), slippage=(0.01, 0.03), fee_bps=30.0,
                 network_fee_usd=0.05),
        persist=False,
    )

    assert real.metrics["net_pnl_usd"] < ideal.metrics["net_pnl_usd"]
    assert real.metrics["drag_usd"] > 0
    assert real.trades[0].entry_price > ideal.trades[0].entry_price  # bought later, higher, wider
    assert real.trades[0].exit_price < ideal.trades[0].exit_price
    assert real.metrics["fees_usd"] > 0


def test_results_are_reproducible_for_a_seed(conn, trade_factory):
    start, end = build_tape(conn, trade_factory)
    leader_round_trip(conn, trade_factory)
    cfg = base_cfg(start, end, latency_seconds=(15.0, 30.0), slippage=(0.01, 0.03))
    first = run_backtest(conn, ["leader"], cfg, persist=False)
    second = run_backtest(conn, ["leader"], cfg, persist=False)
    assert first.metrics["net_pnl_usd"] == pytest.approx(second.metrics["net_pnl_usd"])

    different = run_backtest(
        conn, ["leader"], base_cfg(start, end, latency_seconds=(15.0, 30.0),
                                   slippage=(0.01, 0.03), seed=99), persist=False
    )
    assert different.metrics["net_pnl_usd"] != first.metrics["net_pnl_usd"]


def test_partial_exit_mirrors_the_leaders_fraction(conn, trade_factory):
    start, end = build_tape(conn, trade_factory)
    entry_price = 0.001 * (1 + 1_000 / 6_000)
    mid_price = 0.001 * (1 + 2_000 / 6_000)
    insert_trades(
        conn,
        [
            trade_factory("leader", "m1", "buy", 100_000, 100_000 * entry_price, 2_000),
            trade_factory("leader", "m1", "sell", 50_000, 50_000 * mid_price, 3_000),
        ],
    )
    conn.commit()
    result = run_backtest(conn, ["leader"], base_cfg(start, end), persist=False)
    trade = result.trades[0]
    # Half was sold with the leader; the rest is closed at the window's end.
    assert trade.exit_reason == "window_end"
    assert trade.proceeds_usd > 0


def test_open_position_is_closed_at_window_end(conn, trade_factory):
    start, end = build_tape(conn, trade_factory)
    entry_price = 0.001 * (1 + 1_000 / 6_000)
    insert_trades(
        conn, [trade_factory("leader", "m1", "buy", 100_000, 100_000 * entry_price, 2_000)]
    )
    conn.commit()
    result = run_backtest(conn, ["leader"], base_cfg(start, end), persist=False)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "window_end"
    assert result.trades[0].proceeds_usd > result.trades[0].cost_usd  # price rose


def test_max_open_positions_limits_exposure(conn, trade_factory):
    start = 1_000
    rows = []
    for m in range(5):
        mint = f"m{m}"
        for i in range(400):
            ts = start + i * 10
            price = 0.001 * (1 + i / 400)
            rows.append(trade_factory("tape", mint, "buy", 1_000, 1_000 * price, ts))
        rows.append(trade_factory("leader", mint, "buy", 100_000, 120.0, start + 100 + m))
    insert_trades(conn, rows)
    conn.commit()
    result = run_backtest(
        conn, ["leader"], base_cfg(start, start + 4_000, max_open_positions=2), persist=False
    )
    assert result.metrics["skipped"]["max_open"] == 3
    assert len(result.trades) == 2


def test_capital_constrains_entries(conn, trade_factory):
    start = 1_000
    rows = []
    for m in range(4):
        mint = f"m{m}"
        for i in range(300):
            ts = start + i * 10
            rows.append(trade_factory("tape", mint, "buy", 1_000, 1_000 * 0.001, ts))
        rows.append(trade_factory("leader", mint, "buy", 100_000, 120.0, start + 50 + m))
    insert_trades(conn, rows)
    conn.commit()
    result = run_backtest(
        conn,
        ["leader"],
        base_cfg(start, start + 3_000, starting_capital_usd=250.0, position_size_usd=100.0),
        persist=False,
    )
    assert result.metrics["skipped"]["no_cash"] >= 1
    assert len(result.trades) <= 3


def test_run_is_persisted_with_its_trades(conn, trade_factory):
    start, end = build_tape(conn, trade_factory)
    leader_round_trip(conn, trade_factory)
    result = run_backtest(conn, ["leader"], base_cfg(start, end, label="unit"), persist=True)
    assert result.run_id is not None
    run = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["label"] == "unit"
    stored = conn.execute(
        "SELECT COUNT(*) AS c FROM backtest_trades WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert stored["c"] == len(result.trades)


def test_rejects_an_empty_wallet_set_or_bad_window(conn):
    with pytest.raises(ValueError):
        run_backtest(conn, [], base_cfg(1_000, 2_000), persist=False)
    with pytest.raises(ValueError):
        run_backtest(conn, ["w"], base_cfg(2_000, 1_000), persist=False)
