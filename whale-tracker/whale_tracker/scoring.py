"""Repeatability scoring.

The thesis: one 300x does not make a wallet worth copying. What matters is
whether the wallet does the same thing again, so every component here is
designed to reward *distribution* over *peak*:

* win rate is shrunk towards a prior, so 3-for-3 does not outrank 34-for-50;
* the ROI term uses the **median**, which an outlier cannot move;
* profit concentration (top-1 share, HHI) is scored directly, and a wallet
  whose P&L collapses without its best trade is flagged `single_outlier`;
* drawdown and recency stop a wallet that blew up, or stopped trading six
  months ago, from sitting at the top of the table.

Every sub-score is bounded to [0, 1] with a fixed transform rather than a
cross-sectional z-score, so a wallet's score means the same thing between runs
and is not silently re-based by whoever else happens to be in the table.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .logging_setup import get_logger
from .models import WalletScore

log = get_logger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "win_rate": 0.22,
    "roi": 0.22,
    "profit_factor": 0.16,
    "consistency": 0.16,
    "drawdown": 0.10,
    "recency": 0.08,
    "activity": 0.06,
}


@dataclass
class ScoringConfig:
    """Knobs for the scorer. Defaults are deliberately conservative."""

    #: Distinct tokens with a closed position required for a full-confidence score.
    min_tokens: int = 3
    #: Positions smaller than this are noise (bot dust, failed entries).
    min_position_usd: float = 25.0
    #: Beta prior on win rate: start every wallet at 2 wins / 3 losses.
    prior_wins: float = 2.0
    prior_losses: float = 3.0
    #: Median ROI that maps to a strong (but not maximal) ROI sub-score.
    roi_scale: float = 1.0
    #: Profit factor is capped here before scoring (and ∞ maps to the cap).
    profit_factor_cap: float = 10.0
    #: Trade count that counts as "fully active".
    activity_target: int = 30
    #: Recency half-life in days.
    recency_halflife_days: float = 30.0
    #: Top-1 profit share above which a wallet is called a one-hit wonder.
    outlier_share_threshold: float = 0.6
    #: Positions where more than this share of the tokens sold were never
    #: bought (airdrops, transfers in, history older than our ingest window)
    #: are excluded: their "profit" is unattributable, not skill.
    max_zero_cost_share: float = 0.5
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def normalised_weights(self) -> dict[str, float]:
        total = sum(self.weights.values()) or 1.0
        return {k: v / total for k, v in self.weights.items()}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def basis_pnl(row: Mapping[str, Any]) -> float:
    """P&L on tokens the wallet demonstrably paid for.

    `realised_pnl_usd` also contains proceeds from tokens that arrived without
    a buy. Those dollars are real but unattributable, so ranking must not use
    them: otherwise a wallet that was merely gifted a supply outranks a trader.
    """
    return float(row.get("proceeds_usd") or 0.0) - float(row.get("cost_usd") or 0.0)


def max_drawdown(pnls: Sequence[float]) -> tuple[float, float]:
    """Max peak-to-trough decline of the cumulative P&L curve.

    Returns (absolute_usd, fraction_of_peak). The fraction is relative to the
    running peak equity, which is the number a copier actually feels. A wallet
    whose cumulative P&L never rose above zero has no peak to measure against
    and is reported as fully drawn down — it was underwater the entire time.
    """
    equity = 0.0
    peak = 0.0
    worst_abs = 0.0
    worst_pct = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drop = peak - equity
        if drop > worst_abs:
            worst_abs = drop
        if peak > 0:
            worst_pct = max(worst_pct, drop / peak)
    if peak <= 0 and worst_abs > 0:
        worst_pct = 1.0
    return worst_abs, worst_pct


def _sub_scores(score: WalletScore, cfg: ScoringConfig) -> dict[str, float]:
    pf = score.profit_factor
    pf_effective = cfg.profit_factor_cap if pf is None else min(pf, cfg.profit_factor_cap)

    days = score.days_since_last_trade
    recency = 0.5 ** (days / cfg.recency_halflife_days) if days is not None else 0.0

    return {
        "win_rate": _clamp(score.win_rate_shrunk),
        "roi": _clamp(math.tanh(max(score.median_roi, 0.0) / max(cfg.roi_scale, 1e-9))),
        "profit_factor": _clamp(pf_effective / (pf_effective + 2.0)),
        "consistency": _clamp(score.consistency_score),
        "drawdown": _clamp(1.0 - score.max_drawdown_pct),
        "recency": _clamp(recency),
        "activity": _clamp(math.log1p(score.trades) / math.log1p(max(cfg.activity_target, 1))),
    }


def score_wallet(
    wallet: str,
    positions: Iterable[Mapping[str, Any]],
    *,
    cfg: Optional[ScoringConfig] = None,
    now_ts: Optional[int] = None,
    penalty: float = 0.0,
) -> WalletScore:
    """Build a scorecard from a wallet's per-token P&L rows."""
    cfg = cfg or ScoringConfig()
    now = int(now_ts if now_ts is not None else time.time())
    rows = [dict(p) for p in positions]

    out = WalletScore(wallet=wallet)
    out.tokens_traded = len({r["mint"] for r in rows})
    out.trades = sum(int(r.get("buys") or 0) + int(r.get("sells") or 0) for r in rows)

    first_ts = [int(r["first_buy_ts"]) for r in rows if r.get("first_buy_ts")]
    last_ts = [int(r["last_sell_ts"]) for r in rows if r.get("last_sell_ts")]
    out.first_trade_ts = min(first_ts) if first_ts else None
    out.last_trade_ts = max(last_ts) if last_ts else None
    if out.last_trade_ts:
        out.days_since_last_trade = max(0.0, (now - out.last_trade_ts) / 86_400.0)

    # A position only counts towards repeatability once it has been exited with
    # real money at risk. Open bags and dust are excluded.
    def _attributable(row: Mapping[str, Any]) -> bool:
        sold = float(row.get("tokens_sold") or 0.0)
        zero_cost = float(row.get("zero_cost_tokens") or 0.0)
        return sold <= 0 or (zero_cost / sold) <= cfg.max_zero_cost_share

    eligible = [
        r
        for r in rows
        if int(r.get("is_closed") or 0)
        and r.get("roi") is not None
        and float(r.get("cost_usd") or 0.0) >= cfg.min_position_usd
    ]
    scored = [r for r in eligible if _attributable(r)]
    unattributable = len(eligible) - len(scored)
    out.closed_positions = len(scored)
    # Reported, never ranked on: proceeds from tokens the wallet never paid for.
    out.windfall_pnl_usd = sum(float(r.get("zero_cost_proceeds_usd") or 0.0) for r in rows)

    if not scored:
        out.components_json = json.dumps(
            {
                "reason": "no closed, attributable positions above the size floor",
                "positions_excluded_unattributable": unattributable,
                "windfall_pnl_usd": round(out.windfall_pnl_usd, 2),
            }
        )
        out.penalty = penalty
        return out

    rois = [float(r["roi"]) for r in scored]
    pnls = [basis_pnl(r) for r in scored]

    out.wins = sum(1 for p in pnls if p > 0)
    out.losses = sum(1 for p in pnls if p <= 0)
    out.win_rate = out.wins / len(pnls)
    out.win_rate_shrunk = (out.wins + cfg.prior_wins) / (
        len(pnls) + cfg.prior_wins + cfg.prior_losses
    )
    out.median_roi = statistics.median(rois)
    out.mean_roi = statistics.fmean(rois)
    out.roi_stdev = statistics.pstdev(rois) if len(rois) > 1 else 0.0

    out.gross_profit_usd = sum(p for p in pnls if p > 0)
    out.gross_loss_usd = abs(sum(p for p in pnls if p < 0))
    out.realised_pnl_usd = sum(pnls)
    if out.gross_loss_usd > 0:
        out.profit_factor = out.gross_profit_usd / out.gross_loss_usd
    else:
        # None means "no losing position" (an infinite profit factor). A wallet
        # with neither profit nor loss gets 0.0, not infinity.
        out.profit_factor = None if out.gross_profit_usd > 0 else 0.0

    ordered = sorted(scored, key=lambda r: int(r.get("last_sell_ts") or 0))
    out.max_drawdown_usd, out.max_drawdown_pct = max_drawdown(
        [basis_pnl(r) for r in ordered]
    )

    holds = [float(r["hold_seconds"]) for r in scored if r.get("hold_seconds") is not None]
    out.median_hold_seconds = statistics.median(holds) if holds else None

    profits = sorted((p for p in pnls if p > 0), reverse=True)
    if out.gross_profit_usd > 0:
        out.top1_profit_share = profits[0] / out.gross_profit_usd
        out.top3_profit_share = sum(profits[:3]) / out.gross_profit_usd
        out.profit_hhi = sum((p / out.gross_profit_usd) ** 2 for p in profits)
    best = profits[0] if profits else 0.0
    out.pnl_without_best_usd = out.realised_pnl_usd - best

    out.consistency_score = (
        _clamp(0.5 * (1.0 - out.top1_profit_share) + 0.5 * (1.0 - out.profit_hhi))
        if out.gross_profit_usd > 0
        else 0.0
    )
    out.single_outlier = bool(
        out.top1_profit_share >= cfg.outlier_share_threshold
        or (out.realised_pnl_usd > 0 and out.pnl_without_best_usd <= 0)
        or (out.mean_roi > 0 and out.median_roi <= 0)
    )

    components = _sub_scores(out, cfg)
    weights = cfg.normalised_weights()
    weighted = sum(components[k] * weights.get(k, 0.0) for k in components)

    # Thin samples are shrunk towards zero rather than trusted: three tokens is
    # the difference between a track record and an anecdote.
    sample_confidence = _clamp(out.closed_positions / max(cfg.min_tokens, 1))
    out.raw_score = 100.0 * weighted * sample_confidence

    # A one-hit wonder keeps its P&L but loses a third of its rank: the profit
    # was real, the repeatability was not.
    outlier_haircut = 0.35 if out.single_outlier else 0.0
    out.penalty = _clamp(1.0 - (1.0 - _clamp(penalty)) * (1.0 - outlier_haircut))
    out.score = out.raw_score * (1.0 - out.penalty)

    out.components_json = json.dumps(
        {
            "components": {k: round(v, 4) for k, v in components.items()},
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "sample_confidence": round(sample_confidence, 4),
            "outlier_haircut": outlier_haircut,
            "pattern_penalty": round(_clamp(penalty), 4),
            "positions_excluded_unattributable": unattributable,
            "windfall_pnl_usd": round(out.windfall_pnl_usd, 2),
        },
        separators=(",", ":"),
    )
    return out


def load_penalties(conn: sqlite3.Connection) -> dict[str, float]:
    return {
        row["wallet"]: float(row["penalty"] or 0.0)
        for row in conn.execute("SELECT wallet, penalty FROM wallet_flags")
    }


def score_all(
    conn: sqlite3.Connection,
    *,
    cfg: Optional[ScoringConfig] = None,
    penalties: Optional[Mapping[str, float]] = None,
    now_ts: Optional[int] = None,
) -> int:
    """Score every wallet with stored P&L. Returns wallets written."""
    from .db import upsert_scores

    cfg = cfg or ScoringConfig()
    if penalties is None:
        penalties = load_penalties(conn)

    batch: list[dict[str, Any]] = []
    written = 0
    current: Optional[str] = None
    bucket: list[sqlite3.Row] = []

    def flush(wallet: Optional[str], rows: list[sqlite3.Row]) -> None:
        nonlocal written
        if wallet is None or not rows:
            return
        score = score_wallet(
            wallet, rows, cfg=cfg, now_ts=now_ts, penalty=float(penalties.get(wallet, 0.0))
        )
        batch.append(score.as_row())
        written += 1
        if len(batch) >= 500:
            upsert_scores(conn, batch)
            batch.clear()

    for row in conn.execute("SELECT * FROM wallet_token_pnl ORDER BY wallet"):
        if row["wallet"] != current:
            flush(current, bucket)
            current, bucket = row["wallet"], []
        bucket.append(row)
    flush(current, bucket)

    if batch:
        upsert_scores(conn, batch)
    conn.commit()
    log.info("score.done", extra={"ctx": {"wallets": written}})
    return written


def ranked_wallets(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    min_tokens: int = 3,
    min_closed: int = 3,
    max_penalty: float = 1.0,
    include_outliers: bool = True,
    min_pnl_usd: Optional[float] = None,
) -> list[sqlite3.Row]:
    """The ranked candidate table, with the usual sanity filters applied."""
    sql = [
        "SELECT * FROM v_ranked_wallets WHERE tokens_traded >= ? AND closed_positions >= ? "
        "AND penalty <= ?"
    ]
    params: list[Any] = [min_tokens, min_closed, max_penalty]
    if not include_outliers:
        sql.append("AND single_outlier = 0")
    if min_pnl_usd is not None:
        sql.append("AND realised_pnl_usd >= ?")
        params.append(min_pnl_usd)
    sql.append("ORDER BY score DESC LIMIT ?")
    params.append(limit)
    return list(conn.execute(" ".join(sql), params).fetchall())
