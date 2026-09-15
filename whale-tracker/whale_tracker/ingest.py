"""Ingestion: token trade tapes and wallet histories -> normalised `trades`.

Two directions of discovery, and you generally want both:

1. `ingest_token(mint)` — everyone who traded a seed token. This is how
   candidate wallets are found in the first place.
2. `expand_wallet(wallet)` — everything a candidate wallet traded elsewhere.
   Repeatability cannot be judged from the seed tokens alone: a wallet that
   looks like a genius across three tokens you picked may look ordinary across
   the forty it actually traded.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .clients.birdeye import BirdeyeClient, normalise_trade, overview_to_token_row
from .clients.helius import HeliusClient, extract_legs
from .clients.prices import DexScreenerPriceSource, PriceChain
from .config import STABLE_MINTS, Settings, WSOL_MINT
from .db import finish_ingest_run, insert_trades, start_ingest_run, upsert_token
from .logging_setup import get_logger
from .models import SwapLeg, Trade

log = get_logger(__name__)


class PriceOracle:
    """Values the quote leg of a swap in USD.

    Order of preference: stablecoin par -> the configured price chain (keyless
    by default: Jupiter, DexScreener, GeckoTerminal) -> a price already cached
    in `price_points` -> a static fallback the operator supplied. Anything else
    returns None and the trade is priced from the provider's own per-token
    price hint, or dropped.

    `source` is anything exposing `price_at(mint, ts)` — a `PriceChain`, or a
    `BirdeyeClient` directly — so the rest of the pipeline is indifferent to
    which upstream actually answered.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        source: Optional[PriceChain | BirdeyeClient] = None,
        static_sol_price: Optional[float] = None,
    ):
        self.conn = conn
        self.source = source
        self.static_sol_price = static_sol_price
        self.misses = 0

    def sol_price(self, ts: int) -> Optional[float]:
        if self.source is not None:
            price = self.source.price_at(WSOL_MINT, ts)
            if price:
                return price
        row = self.conn.execute(
            "SELECT price_usd FROM price_points WHERE mint = ? "
            "ORDER BY ABS(ts_bucket - ?) LIMIT 1",
            (WSOL_MINT, int(ts)),
        ).fetchone()
        if row is not None:
            return float(row["price_usd"])
        if self.static_sol_price:
            return float(self.static_sol_price)
        self.misses += 1
        return None

    def quote_usd(self, quote_mint: str, amount: float, ts: int) -> Optional[float]:
        amount = abs(float(amount))
        if quote_mint in STABLE_MINTS:
            return amount
        if quote_mint == WSOL_MINT:
            price = self.sol_price(ts)
            return amount * price if price else None
        return None


def leg_to_trade(leg: SwapLeg, oracle: PriceOracle) -> Optional[Trade]:
    """Attach USD values to a swap leg. Returns None when it cannot be priced."""
    token_amount = abs(leg.token_delta)
    if token_amount <= 0:
        return None

    value_usd = oracle.quote_usd(leg.quote_mint, leg.quote_delta, leg.ts)
    if value_usd is None and leg.price_usd_hint:
        value_usd = token_amount * leg.price_usd_hint
    if value_usd is None or value_usd <= 0:
        log.debug(
            "ingest.unpriced_leg",
            extra={"ctx": {"sig": leg.signature, "mint": leg.mint, "quote": leg.quote_mint}},
        )
        return None

    return Trade(
        signature=leg.signature,
        wallet=leg.wallet,
        mint=leg.mint,
        side=leg.side,
        token_amount=token_amount,
        quote_mint=leg.quote_mint,
        quote_amount=abs(leg.quote_delta),
        price_usd=value_usd / token_amount,
        value_usd=value_usd,
        ts=leg.ts,
        slot=leg.slot,
        dex=leg.dex,
        source=leg.source,
    )


@dataclass
class IngestResult:
    mint: str
    txs_seen: int = 0
    legs: int = 0
    trades_written: int = 0
    wallets: int = 0
    unpriced: int = 0
    provider: str = ""

    def as_ctx(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "txs": self.txs_seen,
            "legs": self.legs,
            "trades_new": self.trades_written,
            "wallets": self.wallets,
            "unpriced": self.unpriced,
            "provider": self.provider,
        }


def _persist(
    conn: sqlite3.Connection,
    trades: Sequence[Trade],
    min_trade_usd: float,
) -> tuple[int, set[str]]:
    kept = [t for t in trades if t.value_usd >= min_trade_usd]
    wallets = {t.wallet for t in kept}
    if not kept:
        return 0, wallets
    written = insert_trades(conn, [t.as_row() for t in kept])
    conn.commit()
    return written, wallets


def _refresh_token_stats(conn: sqlite3.Connection, mint: str) -> None:
    row = conn.execute(
        "SELECT COUNT(*) AS trades, COUNT(DISTINCT wallet) AS wallets, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts, MIN(slot) AS first_slot "
        "FROM trades WHERE mint = ?",
        (mint,),
    ).fetchone()
    if row is None or not row["trades"]:
        return
    upsert_token(
        conn,
        {
            "mint": mint,
            "first_trade_ts": row["first_ts"],
            "last_trade_ts": row["last_ts"],
            "launch_ts": row["first_ts"],
            "launch_slot": row["first_slot"],
            "trade_count": row["trades"],
            "wallet_count": row["wallets"],
        },
    )
    conn.commit()


def fetch_token_metadata(
    conn: sqlite3.Connection,
    mint: str,
    *,
    birdeye: Optional[BirdeyeClient] = None,
    helius: Optional[HeliusClient] = None,
    free_metadata: Optional[DexScreenerPriceSource] = None,
) -> None:
    """Best-effort symbol/name/decimals for a mint; never fatal.

    Ordered by cost, not by quality: a free DexScreener lookup is tried before
    falling back to Helius DAS, which is billed at the heavy rate.
    """
    row: dict[str, Any] = {"mint": mint}
    try:
        if birdeye is not None:
            overview = birdeye.token_overview(mint)
            if overview:
                row = overview_to_token_row(mint, overview)
        if not row.get("symbol") and free_metadata is not None:
            row.update(free_metadata.token_metadata(mint))
            row["mint"] = mint
        if not row.get("symbol") and helius is not None:
            row.update(helius.token_metadata(mint))
            row["mint"] = mint
    except Exception as exc:  # metadata is a nicety, not a dependency
        log.warning("ingest.metadata_failed", extra={"ctx": {"mint": mint, "error": str(exc)}})
    upsert_token(conn, row)
    conn.commit()


def ingest_token(
    conn: sqlite3.Connection,
    mint: str,
    *,
    settings: Settings,
    helius: Optional[HeliusClient] = None,
    birdeye: Optional[BirdeyeClient] = None,
    oracle: Optional[PriceOracle] = None,
    free_metadata: Optional[DexScreenerPriceSource] = None,
    max_txs: Optional[int] = None,
    since_ts: Optional[int] = None,
    provider: str = "helius",
) -> IngestResult:
    """Pull every swap touching `mint` and store the wallet-level trades."""
    if helius is None and birdeye is None:
        raise ValueError("ingest_token needs at least one provider client")

    oracle = oracle or PriceOracle(conn, birdeye)
    cap = max_txs if max_txs is not None else settings.max_txs_per_token
    result = IngestResult(mint=mint, provider=provider)
    run_id = start_ingest_run(conn, mint, provider)
    buffer: list[Trade] = []
    wallets: set[str] = set()

    try:
        fetch_token_metadata(
            conn, mint, birdeye=birdeye, helius=helius, free_metadata=free_metadata
        )

        def flush() -> None:
            nonlocal buffer
            if not buffer:
                return
            written, batch_wallets = _persist(conn, buffer, settings.min_trade_usd)
            result.trades_written += written
            wallets.update(batch_wallets)
            buffer = []

        if provider in ("helius", "both") and helius is not None:
            for tx in helius.iter_address_transactions(
                mint, max_txs=cap, tx_type="SWAP", since_ts=since_ts
            ):
                result.txs_seen += 1
                for leg in extract_legs(tx, mints={mint}):
                    result.legs += 1
                    trade = leg_to_trade(leg, oracle)
                    if trade is None:
                        result.unpriced += 1
                        continue
                    buffer.append(trade)
                if len(buffer) >= 500:
                    flush()

        if provider in ("birdeye", "both") and birdeye is not None:
            for item in birdeye.iter_token_trades(mint, max_items=cap, since_ts=since_ts):
                result.txs_seen += 1
                leg = normalise_trade(item, mint)
                if leg is None:
                    continue
                result.legs += 1
                trade = leg_to_trade(leg, oracle)
                if trade is None:
                    result.unpriced += 1
                    continue
                buffer.append(trade)
                if len(buffer) >= 500:
                    flush()

        flush()
        _refresh_token_stats(conn, mint)
        result.wallets = len(wallets)
        finish_ingest_run(
            conn, run_id, txs_seen=result.txs_seen, trades_new=result.trades_written
        )
        log.info("ingest.token.done", extra={"ctx": result.as_ctx()})
        return result
    except Exception as exc:
        # Keep whatever was already parsed. A budget stop or a provider outage
        # halfway through a token should not throw away the pages that did
        # come back — the next run resumes from a larger base.
        try:
            flush()
            _refresh_token_stats(conn, mint)
        except Exception:  # pragma: no cover - never mask the original failure
            log.warning("ingest.partial_flush_failed", extra={"ctx": {"mint": mint}})
        finish_ingest_run(
            conn,
            run_id,
            txs_seen=result.txs_seen,
            trades_new=result.trades_written,
            status="error",
            detail=str(exc)[:300],
        )
        log.error(
            "ingest.token.failed",
            extra={
                "ctx": {
                    "mint": mint,
                    "error": str(exc),
                    "trades_kept": result.trades_written,
                }
            },
        )
        raise


def expand_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    settings: Settings,
    helius: HeliusClient,
    oracle: Optional[PriceOracle] = None,
    max_txs: Optional[int] = None,
    since_ts: Optional[int] = None,
) -> IngestResult:
    """Pull a wallet's own swap history, across every token it has touched.

    `max_txs` defaults to `MAX_TXS_PER_WALLET`, which is deliberately low: a
    wallet's tail is long, and the first pages carry most of the signal.
    """
    oracle = oracle or PriceOracle(conn)
    max_txs = max_txs if max_txs is not None else settings.max_txs_per_wallet
    result = IngestResult(mint=wallet, provider="helius:wallet")
    run_id = start_ingest_run(conn, wallet, "helius:wallet")
    buffer: list[Trade] = []
    mints: set[str] = set()

    try:
        for tx in helius.iter_address_transactions(
            wallet, max_txs=max_txs, tx_type="SWAP", since_ts=since_ts
        ):
            result.txs_seen += 1
            for leg in extract_legs(tx):
                if leg.wallet != wallet:
                    continue
                result.legs += 1
                trade = leg_to_trade(leg, oracle)
                if trade is None:
                    result.unpriced += 1
                    continue
                mints.add(trade.mint)
                buffer.append(trade)
            if len(buffer) >= 500:
                written, _ = _persist(conn, buffer, settings.min_trade_usd)
                result.trades_written += written
                buffer = []

        if buffer:
            written, _ = _persist(conn, buffer, settings.min_trade_usd)
            result.trades_written += written

        for mint in mints:
            _refresh_token_stats(conn, mint)
        result.wallets = 1
        finish_ingest_run(
            conn, run_id, txs_seen=result.txs_seen, trades_new=result.trades_written
        )
        log.info(
            "ingest.wallet.done",
            extra={"ctx": {"wallet": wallet, "txs": result.txs_seen,
                           "trades_new": result.trades_written, "mints": len(mints)}},
        )
        return result
    except Exception as exc:
        try:
            if buffer:
                written, _ = _persist(conn, buffer, settings.min_trade_usd)
                result.trades_written += written
            for mint in mints:
                _refresh_token_stats(conn, mint)
        except Exception:  # pragma: no cover - never mask the original failure
            log.warning("ingest.partial_flush_failed", extra={"ctx": {"wallet": wallet}})
        finish_ingest_run(
            conn,
            run_id,
            txs_seen=result.txs_seen,
            trades_new=result.trades_written,
            status="error",
            detail=str(exc)[:300],
        )
        log.error(
            "ingest.wallet.failed",
            extra={
                "ctx": {"wallet": wallet, "error": str(exc), "trades_kept": result.trades_written}
            },
        )
        raise


def candidate_wallets(
    conn: sqlite3.Connection,
    *,
    min_tokens: int = 2,
    min_trades: int = 2,
    min_volume_usd: float = 0.0,
    limit: Optional[int] = None,
    per_token_limit: Optional[int] = None,
) -> list[str]:
    """Wallets worth expanding: seen in enough seed tokens to be more than noise.

    `per_token_limit` caps the fan-out: at most that many wallets are taken
    from each seed token, ranked by USD volume in that token. Without it, one
    busy mint with 40,000 traders would decide the whole expansion budget.
    The result is then ordered by total volume and truncated to `limit`.
    """
    base = """
        SELECT wallet,
               COUNT(DISTINCT mint) AS tokens,
               COUNT(*)             AS trades,
               SUM(value_usd)       AS volume
        FROM trades
        GROUP BY wallet
        HAVING tokens >= ? AND trades >= ? AND volume >= ?
    """
    params: list[Any] = [min_tokens, min_trades, min_volume_usd]

    if per_token_limit and per_token_limit > 0:
        sql = f"""
            WITH eligible AS ({base}),
            per_token AS (
                SELECT t.wallet,
                       t.mint,
                       SUM(t.value_usd) AS token_volume,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.mint ORDER BY SUM(t.value_usd) DESC
                       ) AS rank_in_token
                FROM trades t
                JOIN eligible e ON e.wallet = t.wallet
                GROUP BY t.wallet, t.mint
            )
            SELECT e.wallet AS wallet, e.volume AS volume
            FROM eligible e
            WHERE e.wallet IN (SELECT wallet FROM per_token WHERE rank_in_token <= ?)
            ORDER BY e.volume DESC
        """
        params.append(int(per_token_limit))
    else:
        sql = f"{base} ORDER BY volume DESC"

    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [row["wallet"] for row in conn.execute(sql, params)]


def ingest_tokens(
    conn: sqlite3.Connection,
    mints: Iterable[str],
    **kwargs: Any,
) -> list[IngestResult]:
    results = []
    started = time.time()
    for mint in mints:
        results.append(ingest_token(conn, mint, **kwargs))
    log.info(
        "ingest.batch.done",
        extra={
            "ctx": {
                "tokens": len(results),
                "trades_new": sum(r.trades_written for r in results),
                "seconds": round(time.time() - started, 2),
            }
        },
    )
    return results
