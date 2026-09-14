"""Scoring: does the table actually reward repeatability over one big win?"""

from __future__ import annotations

import json

import pytest

from whale_tracker.scoring import ScoringConfig, max_drawdown, score_wallet


def position(mint, roi, pnl, *, cost=500.0, closed=True, last_sell_ts=1_000, **extra):
    """A closed position whose proceeds are consistent with its P&L."""
    row = {
        "wallet": "w",
        "mint": mint,
        "roi": roi,
        "realised_pnl_usd": pnl,
        "cost_usd": cost,
        "proceeds_usd": cost + pnl,
        "tokens_sold": 1_000.0,
        "zero_cost_tokens": 0.0,
        "zero_cost_proceeds_usd": 0.0,
        "is_closed": int(closed),
        "buys": 1,
        "sells": 1,
        "first_buy_ts": last_sell_ts - 600,
        "last_sell_ts": last_sell_ts,
        "hold_seconds": 600.0,
    }
    row.update(extra)
    return row


NOW = 2_000_000


def test_consistent_wallet_outranks_one_hit_wonder():
    consistent = [position(f"m{i}", 0.6, 300.0, last_sell_ts=NOW - i * 3_600) for i in range(8)]
    one_hit = [position("big", 20.0, 9_000.0, last_sell_ts=NOW - 3_600)] + [
        position(f"m{i}", -0.5, -250.0, last_sell_ts=NOW - i * 3_600) for i in range(7)
    ]

    good = score_wallet("w", consistent, now_ts=NOW)
    lucky = score_wallet("w", one_hit, now_ts=NOW)

    assert lucky.realised_pnl_usd > good.realised_pnl_usd  # more money...
    assert good.score > lucky.score                        # ...worse wallet
    assert lucky.single_outlier is True
    assert good.single_outlier is False
    assert lucky.top1_profit_share == pytest.approx(1.0)


def test_single_outlier_flag_from_median_vs_mean():
    rows = [position("win", 9.0, 4_500.0)] + [position(f"m{i}", -0.2, -100.0) for i in range(4)]
    score = score_wallet("w", rows, now_ts=NOW)
    assert score.mean_roi > 0 > score.median_roi
    assert score.single_outlier is True
    assert score.pnl_without_best_usd < 0


def test_win_rate_is_shrunk_towards_a_prior():
    """Three-for-three must not beat a long, strong record."""
    tiny = score_wallet("w", [position(f"m{i}", 0.8, 400.0) for i in range(3)], now_ts=NOW)
    long_run = score_wallet(
        "w",
        [position(f"m{i}", 0.8, 400.0) for i in range(34)]
        + [position(f"l{i}", -0.4, -200.0) for i in range(16)],
        now_ts=NOW,
    )
    assert tiny.win_rate == 1.0
    assert long_run.win_rate == pytest.approx(0.68)
    assert tiny.win_rate_shrunk < long_run.win_rate_shrunk
    assert long_run.score > tiny.score


def test_sample_confidence_scales_thin_records():
    cfg = ScoringConfig(min_tokens=4)
    one = score_wallet("w", [position("m0", 1.0, 500.0)], cfg=cfg, now_ts=NOW)
    four = score_wallet("w", [position(f"m{i}", 1.0, 500.0) for i in range(4)], cfg=cfg, now_ts=NOW)
    assert json.loads(one.components_json)["sample_confidence"] == pytest.approx(0.25)
    assert four.score > one.score


def test_open_and_undersized_positions_are_not_scored():
    rows = [
        position("open", 5.0, 5_000.0, closed=False),
        position("dust", 5.0, 5.0, cost=1.0),
        position("real", 0.5, 250.0),
    ]
    score = score_wallet("w", rows, now_ts=NOW)
    assert score.closed_positions == 1
    assert score.tokens_traded == 3


def test_unattributable_positions_are_excluded_but_reported():
    """A wallet that only sold tokens it never bought has no track record."""
    rows = [
        position(
            "airdrop", 0.01, 10_000.0, cost=100.0, tokens_sold=1_000.0,
            zero_cost_tokens=900.0, zero_cost_proceeds_usd=9_900.0,
        ),
        position("real", 0.4, 200.0),
    ]
    score = score_wallet("w", rows, now_ts=NOW)
    assert score.closed_positions == 1
    assert score.windfall_pnl_usd == pytest.approx(9_900.0)
    assert score.realised_pnl_usd == pytest.approx(200.0)
    assert json.loads(score.components_json)["positions_excluded_unattributable"] == 1


def test_recency_decays_the_score():
    rows = [position(f"m{i}", 0.7, 350.0, last_sell_ts=NOW - 600) for i in range(6)]
    stale = [position(f"m{i}", 0.7, 350.0, last_sell_ts=NOW - 180 * 86_400) for i in range(6)]
    assert score_wallet("w", rows, now_ts=NOW).score > score_wallet("w", stale, now_ts=NOW).score


def test_max_drawdown_on_the_equity_curve():
    absolute, fraction = max_drawdown([100.0, 100.0, -150.0, 50.0])
    assert absolute == pytest.approx(150.0)
    assert fraction == pytest.approx(0.75)


def test_never_profitable_wallet_is_fully_drawn_down():
    """A wallet with no peak has nothing to fall from — report a 100% drawdown."""
    absolute, fraction = max_drawdown([-50.0, -25.0])
    assert absolute == pytest.approx(75.0)
    assert fraction == pytest.approx(1.0)

    losing = score_wallet("w", [position(f"m{i}", -0.3, -150.0) for i in range(6)], now_ts=NOW)
    assert losing.max_drawdown_pct == pytest.approx(1.0)
    assert losing.consistency_score == 0.0          # no profits is not "diversified"
    assert losing.profit_factor == 0.0
    assert losing.score < 25.0


def test_profit_factor_is_none_only_when_truly_infinite():
    flawless = score_wallet("w", [position(f"m{i}", 0.5, 250.0) for i in range(5)], now_ts=NOW)
    assert flawless.profit_factor is None
    mixed = score_wallet(
        "w",
        [position("a", 1.0, 600.0), position("b", -0.4, -200.0)],
        now_ts=NOW,
    )
    assert mixed.profit_factor == pytest.approx(3.0)


def test_pattern_penalty_is_applied_to_the_final_score():
    rows = [position(f"m{i}", 0.7, 350.0) for i in range(6)]
    clean = score_wallet("w", rows, now_ts=NOW)
    penalised = score_wallet("w", rows, now_ts=NOW, penalty=0.5)
    assert penalised.raw_score == pytest.approx(clean.raw_score)
    assert penalised.score == pytest.approx(clean.raw_score * 0.5)


def test_wallet_with_no_closed_positions_scores_zero():
    score = score_wallet("w", [position("m", 3.0, 900.0, closed=False)], now_ts=NOW)
    assert score.score == 0.0
    assert score.closed_positions == 0
    assert "reason" in json.loads(score.components_json)
