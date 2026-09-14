"""Realised P&L reconstruction.

Accounting model: **weighted-average cost basis**.

Every buy raises the wallet's average entry for that mint; every sell realises
`(exit_price - avg_entry) * qty` against it. This matches how a trader talks
about their own position ("my average is 0.0004") and, unlike FIFO, does not
invent an ordering that the on-chain data cannot support.

Two situations are handled explicitly rather than swept up:

* **Tokens sold that were never bought** — airdrops, dev allocations, transfers
  from another wallet. Their proceeds are real USD, so they count towards P&L,
  but they have no cost basis and are therefore excluded from ROI and recorded
  separately. Otherwise a wallet that was gifted a supply would score as an
  infinite-ROI genius.
* **Dust** — memecoin traders rarely sell the last 0.4%. A position is treated
  as closed once the remainder is below `dust_tolerance` of what was bought.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping, Optional, Sequence

from .logging_setup import get_logger
from .models import BUY, PositionPnL

log = get_logger(__name__)

DUST_TOLERANCE = 0.01  # 1% of tokens bought may be left behind


def _as_mapping(trade: Any) -> Mapping[str, Any]:
    if isinstance(trade, sqlite3.Row):
        return dict(trade)
    if isinstance(trade, Mapping):
        return trade
    return trade.as_row()


def compute_position(
    wallet: str,
    mint: str,
    trades: Iterable[Any],
    *,
    last_price: Optional[float] = None,
    dust_tolerance: float = DUST_TOLERANCE,
) -> PositionPnL:
    """Reconstruct one wallet's realised P&L in one token.

    `trades` may be Trade objects, sqlite3.Rows or dicts; they are sorted by
    timestamp (then slot) internally, so callers need not pre-order them.
    """
    rows = [_as_mapping(t) for t in trades]
    rows.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("slot") or 0), str(r.get("signature") or "")))

    pos = PositionPnL(wallet=wallet, mint=mint)

    qty = 0.0              # tokens currently held
    basis = 0.0            # USD cost of those tokens
    basis_ts_usd = 0.0     # Σ (cost_usd × acquisition_ts), for hold-time weighting
    hold_num = 0.0         # Σ (basis_consumed × holding_seconds)
    hold_den = 0.0

    for row in rows:
        amount = abs(float(row.get("token_amount") or 0.0))
        value = abs(float(row.get("value_usd") or 0.0))
        ts = int(row.get("ts") or 0)
        if amount <= 0:
            continue

        if row.get("side") == BUY:
            pos.buys += 1
            pos.tokens_bought += amount
            pos.total_buy_usd += value
            qty += amount
            basis += value
            basis_ts_usd += value * ts
            if pos.first_buy_ts is None or ts < pos.first_buy_ts:
                pos.first_buy_ts = ts
                pos.first_buy_slot = int(row.get("slot") or 0)
            continue

        # --- sell ---
        pos.sells += 1
        pos.tokens_sold += amount
        pos.total_sell_usd += value
        pos.last_sell_ts = ts if pos.last_sell_ts is None else max(pos.last_sell_ts, ts)

        covered = min(amount, qty)
        uncovered = amount - covered
        value_per_token = value / amount if amount else 0.0

        if covered > 0 and qty > 0:
            consumed_basis = basis * (covered / qty)
            proceeds = value_per_token * covered
            pos.cost_usd += consumed_basis
            pos.proceeds_usd += proceeds

            avg_acq_ts = (basis_ts_usd / basis) if basis > 0 else float(ts)
            hold_num += consumed_basis * max(0.0, ts - avg_acq_ts)
            hold_den += consumed_basis

            # Shrink the remaining position proportionally.
            remaining_fraction = (qty - covered) / qty
            basis *= remaining_fraction
            basis_ts_usd *= remaining_fraction
            qty -= covered

        if uncovered > 0:
            pos.zero_cost_tokens += uncovered
            pos.zero_cost_proceeds_usd += value_per_token * uncovered

    pos.realised_pnl_usd = pos.proceeds_usd - pos.cost_usd + pos.zero_cost_proceeds_usd
    pos.roi = (pos.proceeds_usd - pos.cost_usd) / pos.cost_usd if pos.cost_usd > 0 else None
    pos.avg_entry_price = pos.total_buy_usd / pos.tokens_bought if pos.tokens_bought > 0 else None
    pos.avg_exit_price = pos.total_sell_usd / pos.tokens_sold if pos.tokens_sold > 0 else None
    pos.hold_seconds = (hold_num / hold_den) if hold_den > 0 else None
    pos.remaining_tokens = max(0.0, qty)
    pos.remaining_cost_usd = max(0.0, basis)
    if last_price is not None and pos.remaining_tokens > 0:
        pos.unrealised_pnl_usd = pos.remaining_tokens * last_price - pos.remaining_cost_usd
    pos.is_closed = bool(
        pos.sells > 0
        and pos.remaining_tokens <= max(dust_tolerance * pos.tokens_bought, 0.0)
    )
    return pos


def last_prices(conn: sqlite3.Connection, mints: Optional[Sequence[str]] = None) -> dict[str, float]:
    """Most recent observed trade price per mint, used to mark open positions."""
    sql = """
        SELECT t.mint AS mint, t.price_usd AS price_usd
        FROM trades t
        JOIN (SELECT mint, MAX(ts) AS ts FROM trades GROUP BY mint) m
          ON m.mint = t.mint AND m.ts = t.ts
    """
    params: list[Any] = []
    if mints:
        placeholders = ",".join("?" for _ in mints)
        sql += f" WHERE t.mint IN ({placeholders})"
        params.extend(mints)
    out: dict[str, float] = {}
    for row in conn.execute(sql, params):
        price = float(row["price_usd"] or 0.0)
        if price > 0:
            out[row["mint"]] = price
    return out


def rebuild_positions(
    conn: sqlite3.Connection,
    *,
    wallets: Optional[Sequence[str]] = None,
    mints: Optional[Sequence[str]] = None,
    min_trade_usd: float = 0.0,
) -> int:
    """Recompute `wallet_token_pnl` from the `trades` table.

    Returns the number of (wallet, mint) positions written.
    """
    from .db import upsert_pnl  # local import keeps db -> pnl dependency one-way

    sql = "SELECT * FROM trades WHERE value_usd >= ?"
    params: list[Any] = [float(min_trade_usd)]
    if wallets:
        sql += f" AND wallet IN ({','.join('?' for _ in wallets)})"
        params.extend(wallets)
    if mints:
        sql += f" AND mint IN ({','.join('?' for _ in mints)})"
        params.extend(mints)
    sql += " ORDER BY wallet, mint, ts, slot"

    marks = last_prices(conn, mints)
    batch: list[dict[str, Any]] = []
    written = 0
    current_key: Optional[tuple[str, str]] = None
    bucket: list[sqlite3.Row] = []

    def flush(key: Optional[tuple[str, str]], rows: list[sqlite3.Row]) -> None:
        nonlocal written
        if key is None or not rows:
            return
        wallet, mint = key
        position = compute_position(wallet, mint, rows, last_price=marks.get(mint))
        batch.append(position.as_row())
        written += 1
        if len(batch) >= 500:
            upsert_pnl(conn, batch)
            batch.clear()

    for row in conn.execute(sql, params):
        key = (row["wallet"], row["mint"])
        if key != current_key:
            flush(current_key, bucket)
            current_key, bucket = key, []
        bucket.append(row)
    flush(current_key, bucket)

    if batch:
        upsert_pnl(conn, batch)
    conn.commit()
    log.info("pnl.rebuilt", extra={"ctx": {"positions": written}})
    return written
