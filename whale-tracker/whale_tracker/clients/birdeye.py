"""Birdeye client: token metadata, trade tape and historical prices.

Birdeye complements Helius in two places:

* it already quotes a USD price on every swap, which spares us a price lookup
  per trade and gives a sanity check on our own arithmetic;
* it serves historical OHLCV, which is how we value the SOL leg of older
  trades and mark open positions in the backtest.

Docs: https://docs.birdeye.so/reference
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterator, Optional

from ..budget import BudgetTracker
from ..config import QUOTE_MINTS, STABLE_MINTS, Settings, WSOL_MINT
from ..db import upsert_price_points
from ..logging_setup import get_logger
from ..models import SwapLeg
from .base import HttpClient

log = get_logger(__name__)

#: Historical prices are cached per hour; memecoin P&L does not need finer
#: granularity for the *quote* leg (SOL moves far less than the token).
PRICE_BUCKET_SECONDS = 3600
MAX_PAGE = 50


class BirdeyeClient:
    def __init__(
        self,
        settings: Settings,
        conn: Optional[sqlite3.Connection] = None,
        budget: Optional[BudgetTracker] = None,
    ):
        self.settings = settings
        self.conn = conn
        self.http = HttpClient(
            settings.birdeye_base_url,
            provider="birdeye",
            rate_limit_rps=settings.birdeye_rate_limit_rps,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            conn=conn,
            cache_ttl_seconds=settings.http_cache_ttl_seconds,
            default_headers={
                "X-API-KEY": settings.require_birdeye_key(),
                "x-chain": "solana",
                "accept": "application/json",
            },
            budget=budget,
            # Birdeye bills in its own compute units, which this tool does not
            # model; its calls are capped by request count only.
            cost_kind="free",
        )
        self._price_memo: dict[tuple[str, int], float] = {}

    # -- endpoints --------------------------------------------------------
    def token_overview(self, mint: str) -> dict[str, Any]:
        payload = self.http.get("/defi/token_overview", params={"address": mint}) or {}
        return payload.get("data") or {}

    def token_trades_page(
        self, mint: str, *, offset: int = 0, limit: int = MAX_PAGE, sort_type: str = "desc"
    ) -> tuple[list[dict[str, Any]], bool]:
        payload = self.http.get(
            "/defi/txs/token",
            params={
                "address": mint,
                "offset": offset,
                "limit": min(limit, MAX_PAGE),
                "tx_type": "swap",
                "sort_type": sort_type,
            },
        ) or {}
        data = payload.get("data") or {}
        items = data.get("items") or []
        return items, bool(data.get("hasNext"))

    def iter_token_trades(
        self,
        mint: str,
        *,
        max_items: int = 5_000,
        since_ts: Optional[int] = None,
    ) -> Iterator[dict[str, Any]]:
        """Walk the token's trade tape newest-first until exhausted or capped."""
        offset = 0
        seen = 0
        while seen < max_items:
            items, has_next = self.token_trades_page(mint, offset=offset, limit=MAX_PAGE)
            if not items:
                return
            for item in items:
                ts = int(item.get("blockUnixTime") or 0)
                if since_ts is not None and ts and ts < since_ts:
                    return
                seen += 1
                yield item
                if seen >= max_items:
                    return
            if not has_next:
                return
            offset += len(items)

    def price_now(self, mint: str) -> Optional[float]:
        payload = self.http.get("/defi/price", params={"address": mint}, use_cache=False) or {}
        value = (payload.get("data") or {}).get("value")
        return float(value) if value is not None else None

    def history_price(
        self, mint: str, time_from: int, time_to: int, interval: str = "1H"
    ) -> list[dict[str, Any]]:
        payload = self.http.get(
            "/defi/history_price",
            params={
                "address": mint,
                "address_type": "token",
                "type": interval,
                "time_from": int(time_from),
                "time_to": int(time_to),
            },
        ) or {}
        return (payload.get("data") or {}).get("items") or []

    # -- price resolution -------------------------------------------------
    def price_at(self, mint: str, ts: int) -> Optional[float]:
        """USD price of `mint` around `ts`, hour-bucketed and cached in SQLite.

        Stablecoins short-circuit to 1.0. Misses fall back to the nearest
        cached bucket within a day before returning None.
        """
        if mint in STABLE_MINTS:
            return 1.0
        bucket = (int(ts) // PRICE_BUCKET_SECONDS) * PRICE_BUCKET_SECONDS
        memo_key = (mint, bucket)
        if memo_key in self._price_memo:
            return self._price_memo[memo_key]

        if self.conn is not None:
            row = self.conn.execute(
                "SELECT price_usd FROM price_points WHERE mint = ? AND ts_bucket = ?",
                (mint, bucket),
            ).fetchone()
            if row is not None:
                price = float(row["price_usd"])
                self._price_memo[memo_key] = price
                return price

        # Fetch a window around the bucket so one call fills many lookups.
        window = PRICE_BUCKET_SECONDS * 24
        items = self.history_price(mint, bucket - window, bucket + window)
        rows = []
        for item in items:
            unix_time = int(item.get("unixTime") or 0)
            value = item.get("value")
            if not unix_time or value is None:
                continue
            item_bucket = (unix_time // PRICE_BUCKET_SECONDS) * PRICE_BUCKET_SECONDS
            rows.append({"mint": mint, "ts_bucket": item_bucket, "price_usd": float(value)})
            self._price_memo[(mint, item_bucket)] = float(value)
        if rows and self.conn is not None:
            upsert_price_points(self.conn, rows)
            self.conn.commit()

        if memo_key in self._price_memo:
            return self._price_memo[memo_key]

        nearest = self._nearest_cached(mint, bucket, window)
        if nearest is None:
            log.warning("birdeye.price_missing", extra={"ctx": {"mint": mint, "ts": ts}})
        return nearest

    def _nearest_cached(self, mint: str, bucket: int, window: int) -> Optional[float]:
        candidates = [
            (abs(b - bucket), p) for (m, b), p in self._price_memo.items() if m == mint
        ]
        if self.conn is not None:
            rows = self.conn.execute(
                "SELECT ts_bucket, price_usd FROM price_points "
                "WHERE mint = ? AND ts_bucket BETWEEN ? AND ?",
                (mint, bucket - window, bucket + window),
            ).fetchall()
            candidates.extend(
                (abs(int(r["ts_bucket"]) - bucket), float(r["price_usd"])) for r in rows
            )
        if not candidates:
            return None
        distance, price = min(candidates, key=lambda kv: kv[0])
        return price if distance <= window else None

    def sol_price_at(self, ts: int) -> Optional[float]:
        return self.price_at(WSOL_MINT, ts)

    def stats(self) -> dict[str, int]:
        return self.http.stats()


# ---------------------------------------------------------------------------
# Parsing: Birdeye trade item -> SwapLeg
# ---------------------------------------------------------------------------


def _side_amounts(item: dict[str, Any], mint: str) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (token_side, quote_side) for `mint`, whichever way round it came."""
    base = item.get("base") or item.get("from") or {}
    quote = item.get("quote") or item.get("to") or {}
    base_mint = base.get("address") or ""
    quote_mint = quote.get("address") or ""
    if base_mint == mint:
        return base, quote
    if quote_mint == mint:
        return quote, base
    return None


def _ui_change(side: dict[str, Any]) -> float:
    """Signed token movement from the trader's perspective."""
    for key in ("uiChangeAmount", "changeAmount", "uiAmount", "amount"):
        value = side.get(key)
        if value is None:
            continue
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if key in ("changeAmount", "amount"):
            decimals = int(side.get("decimals") or 0)
            amount = amount / (10**decimals) if decimals else amount
        return amount
    return 0.0


def normalise_trade(item: dict[str, Any], mint: str) -> Optional[SwapLeg]:
    """Convert one Birdeye tape entry into a SwapLeg, or None if unusable."""
    pair = _side_amounts(item, mint)
    if pair is None:
        return None
    token_side, quote_side = pair
    quote_mint = quote_side.get("address") or ""
    if quote_mint not in QUOTE_MINTS:
        return None

    token_delta = _ui_change(token_side)
    quote_delta = _ui_change(quote_side)
    if not token_delta or not quote_delta:
        return None

    # Birdeye's `side` field describes the *base* token; use it to fix signs
    # when the payload only carries unsigned amounts.
    side = (item.get("side") or "").lower()
    if token_delta > 0 and quote_delta > 0:
        if side == "sell":
            token_delta, quote_delta = -abs(token_delta), abs(quote_delta)
        else:
            token_delta, quote_delta = abs(token_delta), -abs(quote_delta)

    wallet = item.get("owner") or item.get("source_owner") or ""
    signature = item.get("txHash") or item.get("tx_hash") or ""
    ts = int(item.get("blockUnixTime") or item.get("block_unix_time") or 0)
    if not wallet or not signature or not ts:
        return None

    price_hint = token_side.get("price") or token_side.get("nearestPrice")
    try:
        price_usd_hint = float(price_hint) if price_hint is not None else None
    except (TypeError, ValueError):
        price_usd_hint = None

    return SwapLeg(
        signature=signature,
        wallet=wallet,
        mint=mint,
        token_delta=token_delta,
        quote_mint=quote_mint,
        quote_delta=quote_delta,
        ts=ts,
        slot=int(item.get("blockNumber") or item.get("block_number") or 0),
        dex=(item.get("source") or "").upper(),
        source="birdeye",
        price_usd_hint=price_usd_hint,
    )


def overview_to_token_row(mint: str, overview: dict[str, Any]) -> dict[str, Any]:
    """Map a Birdeye token overview onto our `tokens` table columns."""
    return {
        "mint": mint,
        "symbol": overview.get("symbol") or "",
        "name": overview.get("name") or "",
        "decimals": int(overview.get("decimals") or 0),
        "meta_json": {
            "liquidity": overview.get("liquidity"),
            "mc": overview.get("mc") or overview.get("marketCap"),
            "holders": overview.get("holder") or overview.get("holders"),
            "fetched_at": int(time.time()),
        },
    }
