"""Copy-trade backtest: the go/no-go number.

The question this answers is not "were these wallets profitable" — the scoring
table already said yes. It is: **what would have been left for me** after
reacting 15-30 seconds late, paying 1-3% slippage on both sides, and paying
fees on every leg.

Fills are taken from the real tape. When a leader buys at t, the copier's order
is assumed to land at `t + latency` and to fill at the price of the next trade
actually observed in that token at or after that moment — not at the leader's
price. That is the whole point: on a memecoin, those seconds are usually the
entire edge.

Modelling choices, stated plainly:

* Latency is drawn uniformly from the configured range per fill, slippage
  likewise, both from a seeded RNG so a run is reproducible.
* Market impact beyond the configured slippage is **not** modelled. On thin
  tokens with a large position size, real results would be worse.
* The copier mirrors the leader's *fraction*: if the leader sells half, the
  copier sells half.
* Positions still open at the end of the window are closed at the last observed
  price (with slippage) and reported separately, so the headline number is not
  propped up by an imaginary bag.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .logging_setup import get_logger
from .models import BUY

log = get_logger(__name__)


@dataclass
class BacktestConfig:
    """Everything that defines a simulated copy-trading run."""

    start_ts: int
    end_ts: int
    #: Reaction delay, seconds: (min, max), drawn uniformly per fill.
    latency_seconds: tuple[float, float] = (15.0, 30.0)
    #: Slippage as a fraction: (min, max), drawn uniformly per fill.
    slippage: tuple[float, float] = (0.01, 0.03)
    #: USD committed per copied entry.
    position_size_usd: float = 100.0
    starting_capital_usd: float = 1_000.0
    #: Concurrency limits.
    max_open_positions: int = 10
    allow_duplicate_mints: bool = False
    #: Round-trip costs.
    fee_bps: float = 30.0
    network_fee_usd: float = 0.05
    #: Force an exit after this long if the leader never sells (0 = never).
    max_hold_seconds: int = 0
    #: Give up on a fill if the tape has no trade within this many seconds.
    max_price_staleness_seconds: int = 900
    #: Ignore leader entries smaller than this (they are usually test buys).
    min_leader_trade_usd: float = 0.0
    seed: int = 1337
    label: str = ""

    def as_json(self) -> str:
        payload = asdict(self)
        payload["latency_seconds"] = list(self.latency_seconds)
        payload["slippage"] = list(self.slippage)
        return json.dumps(payload, separators=(",", ":"))


@dataclass
class SimTrade:
    """One completed (or force-closed) copied round trip."""

    leader: str
    mint: str
    entry_ts: int
    exit_ts: Optional[int]
    leader_entry_ts: int
    leader_exit_ts: Optional[int]
    entry_price: float
    exit_price: Optional[float]
    leader_entry_price: float
    leader_exit_price: Optional[float]
    tokens: float
    cost_usd: float
    proceeds_usd: float
    fees_usd: float
    pnl_usd: float
    roi: Optional[float]
    hold_seconds: Optional[float]
    exit_reason: str = ""

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    run_id: Optional[int] = None

    def summary_lines(self) -> list[str]:
        m = self.metrics
        return [
            f"copied trades      : {m.get('trades', 0)}",
            f"win rate           : {m.get('win_rate', 0):.1%}",
            f"net P&L            : ${m.get('net_pnl_usd', 0):,.2f}",
            f"ROI on capital     : {m.get('roi_on_capital', 0):.1%}",
            f"ROI on deployed    : {m.get('roi_on_deployed', 0):.1%}",
            f"fees paid          : ${m.get('fees_usd', 0):,.2f}",
            f"max drawdown       : {m.get('max_drawdown_pct', 0):.1%}",
            f"leader P&L (same size, no latency/slippage): ${m.get('leader_pnl_usd', 0):,.2f}",
            f"latency+slippage drag: ${m.get('drag_usd', 0):,.2f}",
        ]


class PriceTape:
    """Per-mint time series of observed trade prices, queried by timestamp."""

    def __init__(self, rows: Iterable[sqlite3.Row]):
        self._ts: dict[str, list[int]] = {}
        self._px: dict[str, list[float]] = {}
        for row in rows:
            mint = row["mint"]
            price = float(row["price_usd"] or 0.0)
            if price <= 0:
                continue
            self._ts.setdefault(mint, []).append(int(row["ts"]))
            self._px.setdefault(mint, []).append(price)

    def price_at(self, mint: str, ts: int, max_staleness: int) -> Optional[tuple[int, float]]:
        """First trade at or after `ts`; falls back to the last one before it."""
        times = self._ts.get(mint)
        if not times:
            return None
        index = bisect_left(times, ts)
        if index < len(times) and times[index] - ts <= max_staleness:
            return times[index], self._px[mint][index]
        if index > 0 and ts - times[index - 1] <= max_staleness:
            return times[index - 1], self._px[mint][index - 1]
        return None

    def last_before(self, mint: str, ts: int) -> Optional[tuple[int, float]]:
        times = self._ts.get(mint)
        if not times:
            return None
        index = bisect_left(times, ts)
        if index == 0:
            return None
        return times[index - 1], self._px[mint][index - 1]


@dataclass
class _OpenPosition:
    leader: str
    mint: str
    tokens: float
    cost_usd: float
    fees_usd: float
    entry_ts: int
    entry_price: float
    leader_entry_ts: int
    leader_entry_price: float
    leader_tokens_at_entry: float
    proceeds_usd: float = 0.0
    last_price: float = 0.0
    leader_exit_value: float = 0.0
    leader_exit_tokens: float = 0.0
    leader_exit_ts: Optional[int] = None


def _load_leader_trades(
    conn: sqlite3.Connection, wallets: Sequence[str], cfg: BacktestConfig
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in wallets)
    sql = (
        f"SELECT * FROM trades WHERE wallet IN ({placeholders}) "
        "AND ts >= ? AND ts <= ? ORDER BY ts, slot, id"
    )
    return list(conn.execute(sql, [*wallets, cfg.start_ts, cfg.end_ts]).fetchall())


def _load_tape(conn: sqlite3.Connection, mints: Sequence[str], cfg: BacktestConfig) -> PriceTape:
    if not mints:
        return PriceTape([])
    rows: list[sqlite3.Row] = []
    chunk = 500  # stay under SQLite's variable limit
    for i in range(0, len(mints), chunk):
        part = mints[i : i + chunk]
        placeholders = ",".join("?" for _ in part)
        rows.extend(
            conn.execute(
                f"SELECT mint, ts, price_usd FROM trades WHERE mint IN ({placeholders}) "
                "AND ts >= ? AND ts <= ? ORDER BY mint, ts",
                [*part, cfg.start_ts, cfg.end_ts + cfg.max_price_staleness_seconds],
            ).fetchall()
        )
    return PriceTape(rows)


def run_backtest(
    conn: sqlite3.Connection,
    wallets: Sequence[str],
    cfg: BacktestConfig,
    *,
    persist: bool = True,
) -> BacktestResult:
    """Simulate copying `wallets` over the configured window."""
    if not wallets:
        raise ValueError("backtest needs at least one wallet to follow")
    if cfg.end_ts <= cfg.start_ts:
        raise ValueError("backtest end must be after start")

    rng = random.Random(cfg.seed)
    leader_trades = _load_leader_trades(conn, wallets, cfg)
    if not leader_trades:
        log.warning(
            "backtest.no_leader_trades",
            extra={"ctx": {"wallets": len(wallets), "start": cfg.start_ts, "end": cfg.end_ts}},
        )
    mints = sorted({row["mint"] for row in leader_trades})
    tape = _load_tape(conn, mints, cfg)

    cash = float(cfg.starting_capital_usd)
    peak_deployed = 0.0
    open_positions: dict[tuple[str, str], _OpenPosition] = {}
    leader_books: dict[tuple[str, str], float] = {}
    completed: list[SimTrade] = []
    equity_curve: list[tuple[int, float]] = []
    skipped = {"no_cash": 0, "no_price": 0, "max_open": 0, "duplicate_mint": 0, "too_small": 0}
    unmatched_sells = 0
    fee_rate = cfg.fee_bps / 10_000.0

    def mark_equity(ts: int) -> None:
        open_value = sum(p.tokens * (p.last_price or p.entry_price) for p in open_positions.values())
        equity_curve.append((ts, cash + open_value))

    def close_position(pos: _OpenPosition, ts: int, reason: str) -> None:
        leader_exit_price = (
            pos.leader_exit_value / pos.leader_exit_tokens if pos.leader_exit_tokens else None
        )
        pnl = pos.proceeds_usd - pos.cost_usd - pos.fees_usd
        # Report the size originally taken, not whatever dust is left.
        entry_tokens = (pos.cost_usd / pos.entry_price) if pos.entry_price else pos.tokens
        completed.append(
            SimTrade(
                leader=pos.leader,
                mint=pos.mint,
                entry_ts=pos.entry_ts,
                exit_ts=ts,
                leader_entry_ts=pos.leader_entry_ts,
                leader_exit_ts=pos.leader_exit_ts,
                entry_price=pos.entry_price,
                exit_price=(pos.proceeds_usd / entry_tokens) if (pos.proceeds_usd and entry_tokens) else pos.last_price,
                leader_entry_price=pos.leader_entry_price,
                leader_exit_price=leader_exit_price,
                tokens=entry_tokens,
                cost_usd=pos.cost_usd,
                proceeds_usd=pos.proceeds_usd,
                fees_usd=pos.fees_usd,
                pnl_usd=pnl,
                roi=(pnl / pos.cost_usd) if pos.cost_usd > 0 else None,
                hold_seconds=float(ts - pos.entry_ts),
                exit_reason=reason,
            )
        )

    def sell_tokens(pos: _OpenPosition, tokens: float, ts: int, price: float) -> None:
        nonlocal cash
        tokens = min(tokens, pos.tokens)
        if tokens <= 0:
            return
        gross = tokens * price
        fees = gross * fee_rate + cfg.network_fee_usd
        pos.proceeds_usd += gross
        pos.fees_usd += fees
        pos.last_price = price
        cash += gross - fees

    for row in leader_trades:
        leader, mint, ts = row["wallet"], row["mint"], int(row["ts"])
        amount = float(row["token_amount"] or 0.0)
        price = float(row["price_usd"] or 0.0)
        value = float(row["value_usd"] or 0.0)
        key = (leader, mint)

        # --- force-exit anything that has outstayed its welcome -----------
        if cfg.max_hold_seconds:
            for stale_key, pos in list(open_positions.items()):
                if ts - pos.entry_ts >= cfg.max_hold_seconds:
                    quote = tape.price_at(mint=pos.mint, ts=ts, max_staleness=cfg.max_price_staleness_seconds)
                    exit_price = (quote[1] if quote else pos.last_price or pos.entry_price) * (
                        1 - rng.uniform(*cfg.slippage)
                    )
                    sell_tokens(pos, pos.tokens, ts, exit_price)
                    close_position(pos, ts, "max_hold")
                    del open_positions[stale_key]

        if row["side"] == BUY:
            leader_books[key] = leader_books.get(key, 0.0) + amount
            if value < cfg.min_leader_trade_usd:
                skipped["too_small"] += 1
                continue
            if key in open_positions:
                # Leader is averaging in; the copier already has exposure.
                continue
            if not cfg.allow_duplicate_mints and any(p.mint == mint for p in open_positions.values()):
                skipped["duplicate_mint"] += 1
                continue
            if len(open_positions) >= cfg.max_open_positions:
                skipped["max_open"] += 1
                continue

            entry_ts = ts + int(rng.uniform(*cfg.latency_seconds))
            quote = tape.price_at(mint, entry_ts, cfg.max_price_staleness_seconds)
            if quote is None:
                skipped["no_price"] += 1
                continue
            fill_ts, fill_price = quote
            fill_price *= 1 + rng.uniform(*cfg.slippage)

            notional = min(cfg.position_size_usd, cash - cfg.network_fee_usd)
            if notional <= 1.0:
                skipped["no_cash"] += 1
                continue
            fees = notional * fee_rate + cfg.network_fee_usd
            tokens = notional / fill_price if fill_price > 0 else 0.0
            if tokens <= 0:
                skipped["no_price"] += 1
                continue
            cash -= notional + fees
            open_positions[key] = _OpenPosition(
                leader=leader,
                mint=mint,
                tokens=tokens,
                cost_usd=notional,
                fees_usd=fees,
                entry_ts=max(fill_ts, entry_ts),
                entry_price=fill_price,
                leader_entry_ts=ts,
                leader_entry_price=price,
                leader_tokens_at_entry=leader_books.get(key, amount),
                last_price=fill_price,
            )
            peak_deployed = max(peak_deployed, cfg.starting_capital_usd - cash)
            mark_equity(entry_ts)
            continue

        # --- leader sell ---------------------------------------------------
        held_by_leader = leader_books.get(key, 0.0)
        leader_books[key] = max(0.0, held_by_leader - amount)
        pos = open_positions.get(key)
        if pos is None:
            unmatched_sells += 1
            continue

        fraction = min(1.0, amount / held_by_leader) if held_by_leader > 0 else 1.0
        exit_ts = ts + int(rng.uniform(*cfg.latency_seconds))
        quote = tape.price_at(mint, exit_ts, cfg.max_price_staleness_seconds)
        exit_price = (quote[1] if quote else price) * (1 - rng.uniform(*cfg.slippage))

        pos.leader_exit_value += amount * price
        pos.leader_exit_tokens += amount
        pos.leader_exit_ts = ts

        tokens_to_sell = pos.tokens * fraction
        sell_tokens(pos, tokens_to_sell, exit_ts, exit_price)
        pos.tokens -= min(tokens_to_sell, pos.tokens)
        mark_equity(exit_ts)

        if pos.tokens <= 1e-12 or fraction >= 0.999:
            pos.tokens = pos.tokens if pos.tokens > 1e-12 else 0.0
            if pos.tokens > 0:  # dust: dump it with the rest
                sell_tokens(pos, pos.tokens, exit_ts, exit_price)
                pos.tokens = 0.0
            close_position(pos, exit_ts, "leader_exit")
            del open_positions[key]

    # --- close whatever is still open at the end of the window -------------
    for key, pos in list(open_positions.items()):
        quote = tape.last_before(pos.mint, cfg.end_ts) or tape.price_at(
            pos.mint, cfg.end_ts, cfg.max_price_staleness_seconds
        )
        mark_price = (quote[1] if quote else pos.entry_price) * (1 - rng.uniform(*cfg.slippage))
        sell_tokens(pos, pos.tokens, cfg.end_ts, mark_price)
        pos.tokens = 0.0
        close_position(pos, cfg.end_ts, "window_end")
        del open_positions[key]
    mark_equity(cfg.end_ts)

    result = BacktestResult(config=cfg, trades=completed, equity_curve=equity_curve)
    result.metrics = _metrics(
        completed, equity_curve, cfg, cash, skipped, unmatched_sells, wallets, peak_deployed
    )
    log.info("backtest.done", extra={"ctx": {k: result.metrics[k] for k in
             ("trades", "net_pnl_usd", "roi_on_capital", "win_rate") if k in result.metrics}})

    if persist:
        result.run_id = _persist(conn, result)
    return result


def _metrics(
    trades: list[SimTrade],
    equity_curve: list[tuple[int, float]],
    cfg: BacktestConfig,
    ending_cash: float,
    skipped: dict[str, int],
    unmatched_sells: int,
    wallets: Sequence[str],
    peak_deployed: float,
) -> dict[str, Any]:
    net_pnl = sum(t.pnl_usd for t in trades)
    deployed = sum(t.cost_usd for t in trades)
    fees = sum(t.fees_usd for t in trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]

    # What the same notional would have made at the leader's own fills — the
    # difference is precisely what latency, slippage and fees cost.
    leader_pnl = 0.0
    for t in trades:
        if t.leader_exit_price and t.leader_entry_price:
            leader_pnl += t.cost_usd * (t.leader_exit_price / t.leader_entry_price - 1.0)

    peak = -float("inf")
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        drop = peak - equity
        max_dd_abs = max(max_dd_abs, drop)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, drop / peak)

    holds = [t.hold_seconds for t in trades if t.hold_seconds is not None]
    per_leader: dict[str, dict[str, Any]] = {}
    for t in trades:
        entry = per_leader.setdefault(t.leader, {"trades": 0, "pnl_usd": 0.0, "wins": 0})
        entry["trades"] += 1
        entry["pnl_usd"] += t.pnl_usd
        entry["wins"] += 1 if t.pnl_usd > 0 else 0

    return {
        "wallets_followed": len(wallets),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "net_pnl_usd": net_pnl,
        "gross_profit_usd": sum(t.pnl_usd for t in wins),
        "gross_loss_usd": abs(sum(t.pnl_usd for t in losses)),
        "profit_factor": (
            sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses))
            if losses and sum(t.pnl_usd for t in losses) != 0
            else None
        ),
        "fees_usd": fees,
        "deployed_usd": deployed,
        "peak_deployed_usd": peak_deployed,
        "starting_capital_usd": cfg.starting_capital_usd,
        "ending_equity_usd": ending_cash,
        "roi_on_capital": (net_pnl / cfg.starting_capital_usd) if cfg.starting_capital_usd else 0.0,
        "roi_on_deployed": (net_pnl / deployed) if deployed else 0.0,
        "max_drawdown_usd": max_dd_abs,
        "max_drawdown_pct": max_dd_pct,
        "median_hold_seconds": sorted(holds)[len(holds) // 2] if holds else None,
        "leader_pnl_usd": leader_pnl,
        "drag_usd": leader_pnl - net_pnl,
        "drag_pct_of_leader": ((leader_pnl - net_pnl) / abs(leader_pnl)) if leader_pnl else None,
        "skipped": skipped,
        "unmatched_leader_sells": unmatched_sells,
        "per_leader": per_leader,
    }


def _persist(conn: sqlite3.Connection, result: BacktestResult) -> int:
    cur = conn.execute(
        "INSERT INTO backtest_runs(created_at, label, params_json, metrics_json) VALUES (?,?,?,?)",
        (
            int(time.time()),
            result.config.label,
            result.config.as_json(),
            json.dumps(result.metrics, default=str, separators=(",", ":")),
        ),
    )
    run_id = int(cur.lastrowid)
    rows = [{"run_id": run_id, **t.as_row()} for t in result.trades]
    if rows:
        cols = list(rows[0].keys())
        conn.executemany(
            f"INSERT INTO backtest_trades ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [tuple(r[c] for c in cols) for r in rows],
        )
    conn.commit()
    return run_id
