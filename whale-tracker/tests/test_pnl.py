"""P&L reconstruction: the arithmetic everything downstream depends on."""

from __future__ import annotations

import pytest

from whale_tracker.db import insert_trades
from whale_tracker.pnl import compute_position, last_prices, rebuild_positions


def trade(side, amount, value, ts, slot=0):
    return {
        "side": side,
        "token_amount": amount,
        "value_usd": value,
        "ts": ts,
        "slot": slot,
        "signature": f"s{ts}",
    }


def test_weighted_average_cost_basis():
    # Buy 1000 @ $0.001 then 1000 @ $0.003 -> average entry $0.002.
    position = compute_position(
        "w", "m", [trade("buy", 1000, 1.0, 0), trade("buy", 1000, 3.0, 100), trade("sell", 1000, 5.0, 200)]
    )
    assert position.avg_entry_price == pytest.approx(0.002)
    assert position.avg_exit_price == pytest.approx(0.005)
    assert position.cost_usd == pytest.approx(2.0)      # half the basis consumed
    assert position.proceeds_usd == pytest.approx(5.0)
    assert position.realised_pnl_usd == pytest.approx(3.0)
    assert position.roi == pytest.approx(1.5)
    assert position.remaining_tokens == pytest.approx(1000)
    assert position.remaining_cost_usd == pytest.approx(2.0)
    assert position.is_closed is False


def test_hold_time_is_cost_weighted():
    # $1 bought at t=0 and $3 at t=100 -> weighted acquisition at t=75.
    position = compute_position(
        "w", "m", [trade("buy", 1000, 1.0, 0), trade("buy", 1000, 3.0, 100), trade("sell", 2000, 9.0, 200)]
    )
    assert position.hold_seconds == pytest.approx(125.0)


def test_trades_are_sorted_before_accounting():
    out_of_order = [trade("sell", 500, 5.0, 300), trade("buy", 500, 1.0, 100)]
    position = compute_position("w", "m", out_of_order)
    assert position.realised_pnl_usd == pytest.approx(4.0)
    assert position.zero_cost_tokens == 0


def test_tokens_sold_without_a_buy_are_quarantined():
    """Airdrops are real money but have no basis: no ROI, flagged separately."""
    position = compute_position("w", "m", [trade("sell", 500, 7.0, 10)])
    assert position.roi is None
    assert position.zero_cost_tokens == pytest.approx(500)
    assert position.zero_cost_proceeds_usd == pytest.approx(7.0)
    assert position.realised_pnl_usd == pytest.approx(7.0)


def test_partially_uncovered_sell_splits_proceeds():
    position = compute_position(
        "w", "m", [trade("buy", 100, 10.0, 0), trade("sell", 200, 40.0, 50)]
    )
    assert position.cost_usd == pytest.approx(10.0)
    assert position.proceeds_usd == pytest.approx(20.0)   # half the sale had basis
    assert position.zero_cost_proceeds_usd == pytest.approx(20.0)
    assert position.roi == pytest.approx(1.0)


def test_dust_counts_as_closed():
    position = compute_position(
        "w", "m", [trade("buy", 1000, 10.0, 0), trade("sell", 995, 20.0, 50)]
    )
    assert position.is_closed is True
    position_with_bag = compute_position(
        "w", "m", [trade("buy", 1000, 10.0, 0), trade("sell", 500, 10.0, 50)]
    )
    assert position_with_bag.is_closed is False


def test_unrealised_marked_at_last_price():
    position = compute_position(
        "w", "m", [trade("buy", 1000, 10.0, 0), trade("sell", 500, 8.0, 50)], last_price=0.02
    )
    assert position.remaining_tokens == pytest.approx(500)
    assert position.remaining_cost_usd == pytest.approx(5.0)
    assert position.unrealised_pnl_usd == pytest.approx(500 * 0.02 - 5.0)


def test_rebuild_positions_round_trip(conn, trade_factory):
    rows = [
        trade_factory("w1", "m1", "buy", 1000, 100.0, 1_000),
        trade_factory("w1", "m1", "sell", 1000, 250.0, 2_000),
        trade_factory("w2", "m1", "buy", 500, 100.0, 1_500),
        trade_factory("w2", "m1", "sell", 500, 50.0, 2_500),
    ]
    insert_trades(conn, rows)
    conn.commit()

    assert rebuild_positions(conn) == 2
    stored = {r["wallet"]: r for r in conn.execute("SELECT * FROM wallet_token_pnl")}
    assert stored["w1"]["realised_pnl_usd"] == pytest.approx(150.0)
    assert stored["w2"]["realised_pnl_usd"] == pytest.approx(-50.0)
    assert stored["w1"]["is_closed"] == 1
    assert last_prices(conn)["m1"] == pytest.approx(0.1)  # the latest trade's price


def test_rebuild_respects_min_trade_usd(conn, trade_factory):
    insert_trades(
        conn,
        [
            trade_factory("w1", "m1", "buy", 1000, 5.0, 1_000),   # below the floor
            trade_factory("w1", "m1", "sell", 1000, 500.0, 2_000),
        ],
    )
    conn.commit()
    rebuild_positions(conn, min_trade_usd=10.0)
    row = conn.execute("SELECT * FROM wallet_token_pnl").fetchone()
    # The dust buy was filtered out, so the sale has no basis at all.
    assert row["buys"] == 0
    assert row["zero_cost_proceeds_usd"] == pytest.approx(500.0)
