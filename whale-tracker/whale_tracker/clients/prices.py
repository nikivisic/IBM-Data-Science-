"""Keyless price sources, tried in order, behind one interface.

Pricing the quote leg of a swap does not need a paid API. Three public
endpoints cover it between them, and they fail in different ways, so the chain
tries each in turn:

1. **Jupiter** — fast, generous limits, spot price only.
2. **DexScreener** — spot price from the deepest pool; good coverage of new
   mints that the others do not know yet.
3. **GeckoTerminal** — the only keyless source here with **historical** OHLCV,
   which is what valuing an old trade actually requires. Slow (30 req/min on
   the free tier), so it sits last and its answers are cached hard.

Birdeye still works and is strictly better for history, but it needs a key and
is therefore off unless `ENABLE_BIRDEYE=true`.

Two rules keep this inside free-tier limits:

* every answer is written into `price_points`, bucketed by hour, so one
  historical call serves hundreds of lookups;
* a source that rate-limits us is put in **cooldown** for the rest of the run
  (configurable) rather than being hammered — the chain simply moves on, and
  says so in the log.

Only the quote currencies (SOL, and stablecoins at par) are ever priced here,
so total call volume is tiny even across a large ingest.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from ..budget import BudgetExceeded, BudgetTracker
from ..config import STABLE_MINTS, Settings, WSOL_MINT
from ..db import get_token_pool, put_token_pool, upsert_price_points
from ..logging_setup import get_logger
from .base import ApiError, HttpClient

log = get_logger(__name__)

#: Historical prices are kept per hour: SOL barely moves against USD inside an
#: hour relative to the memecoin being priced.
PRICE_BUCKET_SECONDS = 3_600


def bucket_of(ts: int, bucket_seconds: int = PRICE_BUCKET_SECONDS) -> int:
    return (int(ts) // bucket_seconds) * bucket_seconds


@dataclass
class PricePoint:
    mint: str
    ts_bucket: int
    price_usd: float

    def as_row(self) -> dict[str, Any]:
        return {"mint": self.mint, "ts_bucket": self.ts_bucket, "price_usd": self.price_usd}


class PriceSource:
    """One upstream price API.

    `spot` returns the current price; `history` returns points around a
    timestamp. A source that cannot do history returns None from `history` and
    reports `supports_history = False`, so the chain does not waste a call.
    """

    name: str = "base"
    supports_history: bool = False

    def spot(self, mint: str) -> Optional[float]:  # pragma: no cover - interface
        return None

    def history(self, mint: str, ts: int) -> list[PricePoint]:  # pragma: no cover - interface
        return []


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class JupiterPriceSource(PriceSource):
    """Jupiter price API. Spot only.

    Handles both the v2 (`{"data": {mint: {"price": ...}}}`) and v3
    (`{mint: {"usdPrice": ...}}`) response shapes, because which one a
    deployment sees depends on the base URL configured.
    """

    name = "jupiter"
    supports_history = False

    def __init__(self, settings: Settings, conn=None, budget=None):
        self.http = HttpClient(
            settings.jupiter_base_url,
            provider=self.name,
            rate_limit_rps=settings.jupiter_rate_limit_rps,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            conn=conn,
            cache_ttl_seconds=settings.spot_price_ttl_seconds,
            budget=budget,
            cost_kind="free",
        )

    def spot(self, mint: str) -> Optional[float]:
        payload = self.http.get("/price/v2", params={"ids": mint}) or {}
        entry = (payload.get("data") or payload).get(mint)
        if not isinstance(entry, dict):
            return None
        for key in ("price", "usdPrice", "usd_price"):
            price = _as_float(entry.get(key))
            if price is not None:
                return price
        return None


class DexScreenerPriceSource(PriceSource):
    """DexScreener token endpoint. Spot only, taken from the deepest pool."""

    name = "dexscreener"
    supports_history = False

    def __init__(self, settings: Settings, conn=None, budget=None):
        self.http = HttpClient(
            settings.dexscreener_base_url,
            provider=self.name,
            rate_limit_rps=settings.dexscreener_rate_limit_rps,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            conn=conn,
            cache_ttl_seconds=settings.spot_price_ttl_seconds,
            budget=budget,
            cost_kind="free",
        )

    def token_metadata(self, mint: str) -> dict[str, Any]:
        """Symbol/name for a mint, free of charge.

        Worth a call purely on budget grounds: the alternative is Helius's DAS
        `getAsset`, which is billed at the heavy rate.
        """
        payload = self.http.get(f"/latest/dex/tokens/{mint}") or {}
        for pair in payload.get("pairs") or []:
            if not isinstance(pair, dict):
                continue
            for side in ("baseToken", "quoteToken"):
                token = pair.get(side) or {}
                if token.get("address") == mint:
                    return {
                        "symbol": token.get("symbol") or "",
                        "name": token.get("name") or "",
                    }
        return {}

    def spot(self, mint: str) -> Optional[float]:
        payload = self.http.get(f"/latest/dex/tokens/{mint}") or {}
        pairs = payload.get("pairs") or []
        best_price: Optional[float] = None
        best_liquidity = -1.0
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            if pair.get("chainId") not in (None, "solana"):
                continue
            price = _as_float(pair.get("priceUsd"))
            if price is None:
                continue
            liquidity = _as_float((pair.get("liquidity") or {}).get("usd")) or 0.0
            if liquidity > best_liquidity:
                best_liquidity, best_price = liquidity, price
        return best_price


class GeckoTerminalPriceSource(PriceSource):
    """GeckoTerminal. The only keyless source here that serves history.

    History needs a pool: the token's deepest pool is looked up once and cached
    in `token_pools`, then hourly OHLCV is pulled around the timestamp, which
    fills a wide span of buckets from a single call.
    """

    name = "geckoterminal"
    supports_history = True
    network = "solana"

    def __init__(self, settings: Settings, conn=None, budget=None):
        self.conn = conn
        self.http = HttpClient(
            settings.geckoterminal_base_url,
            provider=self.name,
            rate_limit_rps=settings.geckoterminal_rate_limit_rps,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            conn=conn,
            cache_ttl_seconds=settings.price_cache_ttl_seconds,
            budget=budget,
            cost_kind="free",
            default_headers={"accept": "application/json;version=20230302"},
        )
        self.spot_ttl = settings.spot_price_ttl_seconds

    def spot(self, mint: str) -> Optional[float]:
        payload = (
            self.http.get(
                f"/api/v2/simple/networks/{self.network}/token_price/{mint}",
                cache_ttl_seconds=self.spot_ttl,
            )
            or {}
        )
        prices = ((payload.get("data") or {}).get("attributes") or {}).get("token_prices") or {}
        return _as_float(prices.get(mint) or prices.get(mint.lower()))

    def top_pool(self, mint: str) -> Optional[str]:
        if self.conn is not None:
            cached = get_token_pool(self.conn, mint, self.name)
            if cached:
                return cached
        payload = self.http.get(f"/api/v2/networks/{self.network}/tokens/{mint}/pools") or {}
        entries = payload.get("data") or []
        for entry in entries:
            address = (entry.get("attributes") or {}).get("address") or entry.get("id", "")
            # Ids come back as "solana_<address>"; attributes.address is bare.
            address = str(address).split("_")[-1]
            if address:
                if self.conn is not None:
                    put_token_pool(self.conn, mint, self.name, address)
                return address
        return None

    def history(self, mint: str, ts: int) -> list[PricePoint]:
        pool = self.top_pool(mint)
        if not pool:
            log.debug("price.no_pool", extra={"ctx": {"source": self.name, "mint": mint}})
            return []
        # Ask for the 100 hours ending a little after the target, so the bucket
        # we want is inside the window along with plenty of neighbours.
        before = int(ts) + PRICE_BUCKET_SECONDS * 2
        payload = (
            self.http.get(
                f"/api/v2/networks/{self.network}/pools/{pool}/ohlcv/hour",
                params={
                    "aggregate": 1,
                    "before_timestamp": before,
                    "limit": 100,
                    "currency": "usd",
                    "token": mint,
                },
            )
            or {}
        )
        candles = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        points: list[PricePoint] = []
        for candle in candles:
            if not isinstance(candle, Sequence) or len(candle) < 5:
                continue
            candle_ts = _as_float(candle[0])
            close = _as_float(candle[4])
            if candle_ts is None or close is None:
                continue
            points.append(PricePoint(mint, bucket_of(int(candle_ts)), close))
        return points


class BirdeyePriceSource(PriceSource):
    """Birdeye, behind the same interface. Needs a key; off unless enabled."""

    name = "birdeye"
    supports_history = True

    def __init__(self, client):
        self.client = client

    def spot(self, mint: str) -> Optional[float]:
        return self.client.price_now(mint)

    def history(self, mint: str, ts: int) -> list[PricePoint]:
        window = PRICE_BUCKET_SECONDS * 24
        items = self.client.history_price(mint, int(ts) - window, int(ts) + window)
        points: list[PricePoint] = []
        for item in items:
            unix_time = item.get("unixTime")
            value = _as_float(item.get("value"))
            if not unix_time or value is None:
                continue
            points.append(PricePoint(mint, bucket_of(int(unix_time)), value))
        return points


@dataclass
class SourceState:
    served: int = 0
    failures: int = 0
    unavailable_until: float = 0.0
    last_error: str = ""


class PriceChain:
    """Ordered fallback across price sources, with caching and cooldowns.

    This is the only thing the rest of the pipeline talks to, so swapping
    sources — or dropping Birdeye entirely — changes nothing upstream.
    """

    def __init__(
        self,
        conn: Optional[sqlite3.Connection],
        sources: Sequence[PriceSource],
        *,
        cooldown_seconds: int = 300,
        bucket_seconds: int = PRICE_BUCKET_SECONDS,
        spot_window_seconds: int = 2 * PRICE_BUCKET_SECONDS,
        now_fn=time.time,
    ):
        self.conn = conn
        self.sources = list(sources)
        self.cooldown_seconds = cooldown_seconds
        self.bucket_seconds = bucket_seconds
        self.spot_window_seconds = spot_window_seconds
        self.now_fn = now_fn
        self.state: dict[str, SourceState] = {s.name: SourceState() for s in self.sources}
        self.cache_hits = 0
        self._memo: dict[tuple[str, int], float] = {}

    # -- availability -----------------------------------------------------
    def _available(self, source: PriceSource) -> bool:
        state = self.state[source.name]
        if state.unavailable_until and self.now_fn() < state.unavailable_until:
            return False
        return True

    def _cool_down(self, source: PriceSource, reason: str, *, status: Optional[int] = None) -> None:
        state = self.state[source.name]
        state.failures += 1
        state.last_error = reason
        # Rate limiting is the one failure worth backing off from wholesale;
        # anything else may just be an unknown mint, so keep the source in play.
        if status == 429 or "429" in reason:
            state.unavailable_until = self.now_fn() + self.cooldown_seconds
            log.warning(
                "price.source_cooldown",
                extra={
                    "ctx": {
                        "source": source.name,
                        "seconds": self.cooldown_seconds,
                        "reason": reason[:200],
                    }
                },
            )
        else:
            log.warning(
                "price.source_failed",
                extra={"ctx": {"source": source.name, "reason": reason[:200]}},
            )

    # -- cache ------------------------------------------------------------
    def _cached_bucket(self, mint: str, bucket: int) -> Optional[float]:
        if (mint, bucket) in self._memo:
            return self._memo[(mint, bucket)]
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT price_usd FROM price_points WHERE mint = ? AND ts_bucket = ?", (mint, bucket)
        ).fetchone()
        if row is None:
            return None
        price = float(row["price_usd"])
        self._memo[(mint, bucket)] = price
        return price

    def _store(self, points: Iterable[PricePoint]) -> None:
        rows = []
        for point in points:
            self._memo[(point.mint, point.ts_bucket)] = point.price_usd
            rows.append(point.as_row())
        if rows and self.conn is not None:
            upsert_price_points(self.conn, rows)
            self.conn.commit()

    def _nearest_cached(self, mint: str, bucket: int, window: int) -> Optional[float]:
        candidates = [
            (abs(b - bucket), price) for (m, b), price in self._memo.items() if m == mint
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

    # -- public interface -------------------------------------------------
    def price_now(self, mint: str) -> Optional[float]:
        """Current USD price, from the first source that answers."""
        if mint in STABLE_MINTS:
            return 1.0
        for source in self.sources:
            if not self._available(source):
                continue
            try:
                price = source.spot(mint)
            except BudgetExceeded:
                raise
            except ApiError as exc:
                self._cool_down(source, str(exc), status=exc.status)
                continue
            except Exception as exc:  # a malformed payload must not kill a run
                self._cool_down(source, f"{type(exc).__name__}: {exc}")
                continue
            if price:
                self.state[source.name].served += 1
                log.debug("price.served", extra={"ctx": {"source": source.name, "mint": mint,
                                                         "kind": "spot", "price": price}})
                return price
        log.warning("price.unavailable", extra={"ctx": {"mint": mint, "kind": "spot"}})
        return None

    def price_at(self, mint: str, ts: int) -> Optional[float]:
        """USD price of `mint` around `ts`, hour-bucketed and cached."""
        if mint in STABLE_MINTS:
            return 1.0
        bucket = bucket_of(int(ts), self.bucket_seconds)

        cached = self._cached_bucket(mint, bucket)
        if cached is not None:
            self.cache_hits += 1
            return cached

        recent = abs(self.now_fn() - int(ts)) <= self.spot_window_seconds
        for source in self.sources:
            if not self._available(source):
                continue
            if not source.supports_history and not recent:
                continue
            try:
                if source.supports_history:
                    points = source.history(mint, int(ts))
                    if points:
                        self._store(points)
                        price = self._cached_bucket(mint, bucket)
                        if price is not None:
                            self.state[source.name].served += 1
                            log.debug(
                                "price.served",
                                extra={"ctx": {"source": source.name, "mint": mint,
                                               "kind": "history", "points": len(points)}},
                            )
                            return price
                else:
                    price = source.spot(mint)
                    if price:
                        self._store([PricePoint(mint, bucket, price)])
                        self.state[source.name].served += 1
                        log.debug(
                            "price.served",
                            extra={"ctx": {"source": source.name, "mint": mint, "kind": "spot"}},
                        )
                        return price
            except BudgetExceeded:
                raise
            except ApiError as exc:
                self._cool_down(source, str(exc), status=exc.status)
            except Exception as exc:
                self._cool_down(source, f"{type(exc).__name__}: {exc}")

        nearest = self._nearest_cached(mint, bucket, self.bucket_seconds * 24)
        if nearest is None:
            log.warning(
                "price.unavailable",
                extra={"ctx": {"mint": mint, "ts": int(ts), "kind": "history"}},
            )
        return nearest

    def sol_price_at(self, ts: int) -> Optional[float]:
        return self.price_at(WSOL_MINT, ts)

    def stats(self) -> dict[str, dict[str, Any]]:
        now = self.now_fn()
        return {
            name: {
                "served": state.served,
                "failures": state.failures,
                "in_cooldown": bool(state.unavailable_until and now < state.unavailable_until),
                "last_error": state.last_error[:120],
            }
            for name, state in self.state.items()
        }


def build_price_chain(
    settings: Settings,
    conn: Optional[sqlite3.Connection] = None,
    budget: Optional[BudgetTracker] = None,
    *,
    sources: Optional[Sequence[str]] = None,
) -> PriceChain:
    """Construct the configured chain. Unknown names are rejected by config."""
    names = tuple(sources) if sources is not None else settings.active_price_sources()
    built: list[PriceSource] = []
    for name in names:
        if name == "jupiter":
            built.append(JupiterPriceSource(settings, conn, budget))
        elif name == "dexscreener":
            built.append(DexScreenerPriceSource(settings, conn, budget))
        elif name == "geckoterminal":
            built.append(GeckoTerminalPriceSource(settings, conn, budget))
        elif name == "birdeye":
            from .birdeye import BirdeyeClient

            built.append(BirdeyePriceSource(BirdeyeClient(settings, conn, budget)))
    log.info(
        "price.chain_ready",
        extra={"ctx": {"sources": ",".join(s.name for s in built) or "(none)"}},
    )
    return PriceChain(
        conn,
        built,
        cooldown_seconds=settings.price_source_cooldown_seconds,
    )
