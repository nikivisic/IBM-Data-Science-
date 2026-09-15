"""The keyless price layer: fallback order, caching, and 429 handling."""

from __future__ import annotations

import pytest
from conftest import FakeResponse, FakeSession

from whale_tracker.budget import BudgetTracker, ProviderBudget
from whale_tracker.clients.base import ApiError
from whale_tracker.clients.prices import (
    DexScreenerPriceSource,
    GeckoTerminalPriceSource,
    JupiterPriceSource,
    PriceChain,
    PricePoint,
    PriceSource,
    bucket_of,
    build_price_chain,
)
from whale_tracker.config import WSOL_MINT, load_settings

MEME = "MemeMint1111111111111111111111111111111111"
NOW = 1_800_000_000


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("HELIUS_API_KEY", "BIRDEYE_API_KEY", "ENABLE_BIRDEYE", "PRICE_SOURCES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_MAX_RETRIES", "2")
    return load_settings()


def wire(source, session):
    """Swap a source's transport for a scripted one."""
    source.http.session = session
    return source


# ---------------------------------------------------------------------------
# individual sources
# ---------------------------------------------------------------------------


def test_jupiter_handles_both_response_shapes(settings, conn):
    v2 = wire(
        JupiterPriceSource(settings, conn),
        FakeSession(default=FakeResponse(200, {"data": {WSOL_MINT: {"price": "142.5"}}})),
    )
    assert v2.spot(WSOL_MINT) == pytest.approx(142.5)

    # No connection: these variants must not be served from the shared cache.
    v3 = wire(
        JupiterPriceSource(settings, None),
        FakeSession(default=FakeResponse(200, {WSOL_MINT: {"usdPrice": 143.25}})),
    )
    assert v3.spot(WSOL_MINT) == pytest.approx(143.25)

    empty = wire(JupiterPriceSource(settings, None), FakeSession(default=FakeResponse(200, {})))
    assert empty.spot(WSOL_MINT) is None


def test_spot_prices_are_cached_for_the_configured_ttl(settings, conn):
    """Repeated spot lookups inside the TTL must not re-hit a free API."""
    session = FakeSession(default=FakeResponse(200, {"data": {WSOL_MINT: {"price": "142.5"}}}))
    source = wire(JupiterPriceSource(settings, conn), session)
    assert source.spot(WSOL_MINT) == pytest.approx(142.5)
    assert source.spot(WSOL_MINT) == pytest.approx(142.5)
    assert len(session.calls) == 1
    assert source.http.cache_hits == 1


def test_dexscreener_takes_the_deepest_pool(settings, conn):
    payload = {
        "pairs": [
            {"chainId": "solana", "priceUsd": "0.00010", "liquidity": {"usd": 5_000}},
            {"chainId": "solana", "priceUsd": "0.00012", "liquidity": {"usd": 90_000}},
            {"chainId": "ethereum", "priceUsd": "999", "liquidity": {"usd": 1_000_000}},
        ]
    }
    source = wire(
        DexScreenerPriceSource(settings, conn), FakeSession(default=FakeResponse(200, payload))
    )
    assert source.spot(MEME) == pytest.approx(0.00012)


def test_dexscreener_serves_free_token_metadata(settings, conn):
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "baseToken": {"address": MEME, "symbol": "MEME", "name": "Meme Token"},
                "quoteToken": {"address": WSOL_MINT, "symbol": "SOL", "name": "Wrapped SOL"},
            }
        ]
    }
    source = wire(
        DexScreenerPriceSource(settings, conn), FakeSession(default=FakeResponse(200, payload))
    )
    assert source.token_metadata(MEME) == {"symbol": "MEME", "name": "Meme Token"}
    assert source.token_metadata("UnknownMint") == {}


def test_geckoterminal_history_and_pool_caching(settings, conn):
    pools = {"data": [{"id": "solana_PoolAddr123", "attributes": {"address": "PoolAddr123"}}]}
    candles = {
        "data": {
            "attributes": {
                "ohlcv_list": [
                    [NOW - 3_600, 1.0, 1.1, 0.9, 141.0, 100],
                    [NOW, 1.0, 1.1, 0.9, 142.0, 100],
                ]
            }
        }
    }
    session = FakeSession(routes={"/pools/": candles and FakeResponse(200, candles),
                                  "/tokens/": FakeResponse(200, pools)})
    source = wire(GeckoTerminalPriceSource(settings, conn), session)

    points = source.history(WSOL_MINT, NOW)
    assert [p.price_usd for p in points] == [141.0, 142.0]
    assert points[1].ts_bucket == bucket_of(NOW)

    # The pool is remembered, so a second call skips the discovery request.
    before = len(session.calls)
    source.history(WSOL_MINT, NOW)
    assert "/tokens/" not in "".join(session.urls()[before:])
    assert conn.execute("SELECT pool FROM token_pools").fetchone()["pool"] == "PoolAddr123"


def test_geckoterminal_without_a_pool_returns_nothing(settings, conn):
    source = wire(
        GeckoTerminalPriceSource(settings, conn),
        FakeSession(default=FakeResponse(200, {"data": []})),
    )
    assert source.history(MEME, NOW) == []


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


class StubSource(PriceSource):
    """A scriptable source for testing ordering and failure handling."""

    def __init__(self, name, *, spot=None, points=None, supports_history=False, raises=None):
        self.name = name
        self.supports_history = supports_history
        self._spot = spot
        self._points = points or []
        self._raises = raises
        self.spot_calls = 0
        self.history_calls = 0

    def spot(self, mint):
        self.spot_calls += 1
        if self._raises:
            raise self._raises
        return self._spot

    def history(self, mint, ts):
        self.history_calls += 1
        if self._raises:
            raise self._raises
        return list(self._points)


def chain_of(conn, *sources, **kwargs):
    kwargs.setdefault("now_fn", lambda: NOW)
    return PriceChain(conn, list(sources), **kwargs)


def test_chain_uses_the_first_source_that_answers(conn):
    first = StubSource("jupiter", spot=None)
    second = StubSource("dexscreener", spot=142.0)
    third = StubSource("geckoterminal", spot=99.0, supports_history=True)
    chain = chain_of(conn, first, second, third)

    assert chain.price_now(WSOL_MINT) == pytest.approx(142.0)
    assert first.spot_calls == 1
    assert second.spot_calls == 1
    assert third.spot_calls == 0  # never reached


def test_chain_order_is_the_configured_order(conn):
    a = StubSource("jupiter", spot=1.0)
    b = StubSource("dexscreener", spot=2.0)
    assert chain_of(conn, a, b).price_now(MEME) == pytest.approx(1.0)
    assert chain_of(conn, b, a).price_now(MEME) == pytest.approx(2.0)


def test_chain_returns_none_when_every_source_is_silent(conn):
    chain = chain_of(conn, StubSource("jupiter"), StubSource("dexscreener"))
    assert chain.price_now(MEME) is None


def test_a_429_puts_the_source_in_cooldown_and_the_next_one_serves(conn):
    limited = StubSource("jupiter", raises=ApiError("rate limited", status=429))
    backup = StubSource("dexscreener", spot=140.0)
    chain = chain_of(conn, limited, backup, cooldown_seconds=300)

    assert chain.price_now(WSOL_MINT) == pytest.approx(140.0)
    assert chain.stats()["jupiter"]["in_cooldown"] is True

    # While cooling down it is not called again at all.
    assert chain.price_now(WSOL_MINT) == pytest.approx(140.0)
    assert limited.spot_calls == 1
    assert backup.spot_calls == 2


def test_cooldown_expires(conn):
    clock = {"now": NOW}
    limited = StubSource("jupiter", raises=ApiError("rate limited", status=429))
    chain = PriceChain(conn, [limited], cooldown_seconds=60, now_fn=lambda: clock["now"])

    chain.price_now(WSOL_MINT)
    assert chain.stats()["jupiter"]["in_cooldown"] is True
    clock["now"] += 61
    assert chain.stats()["jupiter"]["in_cooldown"] is False
    chain.price_now(WSOL_MINT)
    assert limited.spot_calls == 2


def test_other_failures_do_not_cool_a_source_down(conn):
    """An unknown mint is not a reason to stop using a whole source."""
    flaky = StubSource("jupiter", raises=ApiError("404 not found", status=404))
    backup = StubSource("dexscreener", spot=1.0)
    chain = chain_of(conn, flaky, backup)

    chain.price_now(MEME)
    chain.price_now(MEME)
    assert chain.stats()["jupiter"]["in_cooldown"] is False
    assert flaky.spot_calls == 2


def test_malformed_payloads_do_not_kill_the_run(conn):
    broken = StubSource("jupiter", raises=TypeError("bad payload"))
    chain = chain_of(conn, broken, StubSource("dexscreener", spot=5.0))
    assert chain.price_now(MEME) == pytest.approx(5.0)
    assert chain.stats()["jupiter"]["failures"] == 1


def test_history_is_cached_in_price_points_and_reused(conn):
    source = StubSource(
        "geckoterminal",
        supports_history=True,
        points=[PricePoint(WSOL_MINT, bucket_of(NOW - i * 3_600), 140.0 + i) for i in range(5)],
    )
    chain = chain_of(conn, source)
    old = NOW - 10 * 86_400

    # One upstream call fills many buckets...
    assert chain.price_at(WSOL_MINT, NOW) == pytest.approx(140.0)
    assert source.history_calls == 1
    stored = conn.execute("SELECT COUNT(*) AS c FROM price_points").fetchone()["c"]
    assert stored == 5

    # ...and neighbouring lookups are served from the cache, not the network.
    assert chain.price_at(WSOL_MINT, NOW - 3_600) == pytest.approx(141.0)
    assert source.history_calls == 1
    assert chain.cache_hits == 1

    # A bucket nobody fetched still costs a call.
    chain.price_at(WSOL_MINT, old)
    assert source.history_calls == 2


def test_cache_survives_a_new_chain(conn):
    source = StubSource(
        "geckoterminal", supports_history=True, points=[PricePoint(WSOL_MINT, bucket_of(NOW), 150.0)]
    )
    chain_of(conn, source).price_at(WSOL_MINT, NOW)

    fresh_source = StubSource("geckoterminal", supports_history=True, points=[])
    assert chain_of(conn, fresh_source).price_at(WSOL_MINT, NOW) == pytest.approx(150.0)
    assert fresh_source.history_calls == 0


def test_spot_only_sources_are_skipped_for_old_timestamps(conn):
    """Asking Jupiter for last month's price would silently return today's."""
    spot_only = StubSource("jupiter", spot=999.0)
    historical = StubSource(
        "geckoterminal",
        supports_history=True,
        points=[PricePoint(WSOL_MINT, bucket_of(NOW - 30 * 86_400), 22.0)],
    )
    chain = chain_of(conn, spot_only, historical)

    assert chain.price_at(WSOL_MINT, NOW - 30 * 86_400) == pytest.approx(22.0)
    assert spot_only.spot_calls == 0


def test_spot_sources_are_allowed_for_recent_timestamps(conn):
    spot_only = StubSource("jupiter", spot=141.0)
    chain = chain_of(conn, spot_only)
    assert chain.price_at(WSOL_MINT, NOW - 60) == pytest.approx(141.0)
    assert spot_only.spot_calls == 1


def test_stablecoins_short_circuit(conn):
    from whale_tracker.config import USDC_MINT

    source = StubSource("jupiter", spot=0.5)
    chain = chain_of(conn, source)
    assert chain.price_at(USDC_MINT, NOW) == 1.0
    assert chain.price_now(USDC_MINT) == 1.0
    assert source.spot_calls == 0


def test_nearest_cached_price_is_the_last_resort(conn):
    from whale_tracker.db import upsert_price_points

    upsert_price_points(
        conn, [{"mint": WSOL_MINT, "ts_bucket": bucket_of(NOW), "price_usd": 137.0}]
    )
    conn.commit()
    chain = chain_of(conn, StubSource("geckoterminal", supports_history=True, points=[]))
    assert chain.price_at(WSOL_MINT, NOW + 7_200) == pytest.approx(137.0)


def test_budget_exhaustion_is_never_swallowed(conn):
    """A budget stop must abort the run, not fall through to the next source."""
    from whale_tracker.budget import BudgetExceeded

    exhausted = StubSource("jupiter", raises=BudgetExceeded("jupiter", "requests/run", 1, 1, 1))
    backup = StubSource("dexscreener", spot=1.0)
    with pytest.raises(BudgetExceeded):
        chain_of(conn, exhausted, backup).price_now(MEME)
    assert backup.spot_calls == 0


# ---------------------------------------------------------------------------
# construction from configuration
# ---------------------------------------------------------------------------


def test_default_chain_is_keyless_and_in_order(settings, conn):
    chain = build_price_chain(settings, conn)
    assert [s.name for s in chain.sources] == ["jupiter", "dexscreener", "geckoterminal"]
    assert [s.supports_history for s in chain.sources] == [False, False, True]


def test_birdeye_is_off_unless_enabled(settings, conn, monkeypatch):
    monkeypatch.setenv("BIRDEYE_API_KEY", "birdeye-key")
    with_key = load_settings(override=True)
    assert "birdeye" not in [s.name for s in build_price_chain(with_key, conn).sources]

    monkeypatch.setenv("ENABLE_BIRDEYE", "true")
    enabled = load_settings(override=True)
    assert [s.name for s in build_price_chain(enabled, conn).sources][-1] == "birdeye"


def test_enabling_birdeye_without_a_key_is_a_no_op(settings, conn, monkeypatch):
    monkeypatch.setenv("ENABLE_BIRDEYE", "true")
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    assert "birdeye" not in [
        s.name for s in build_price_chain(load_settings(override=True), conn).sources
    ]


def test_unknown_source_names_are_rejected(settings, monkeypatch):
    from whale_tracker.config import ConfigError

    monkeypatch.setenv("PRICE_SOURCES", "jupiter,notarealsource")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(override=True).active_price_sources()
    assert "notarealsource" in str(excinfo.value)


@pytest.mark.parametrize(
    "factory", [JupiterPriceSource, DexScreenerPriceSource, GeckoTerminalPriceSource]
)
def test_each_real_source_surfaces_a_429_as_a_cooldown(factory, settings, conn, no_sleep):
    """Whichever source rate-limits us, the chain steps over it rather than retrying forever."""
    limited = wire(factory(settings, conn), FakeSession(default=FakeResponse(429)))
    backup = StubSource("backup", spot=140.0)
    chain = chain_of(conn, limited, backup)

    assert chain.price_now(WSOL_MINT) == pytest.approx(140.0)
    assert chain.stats()[limited.name]["in_cooldown"] is True


def test_price_calls_are_metered_against_the_budget(settings, conn):
    tracker = BudgetTracker(conn, {"jupiter": ProviderBudget("jupiter", max_requests_per_run=1)})
    source = wire(
        JupiterPriceSource(settings, conn, tracker),
        FakeSession(default=FakeResponse(200, {"data": {WSOL_MINT: {"price": "1"}}})),
    )
    source.spot(WSOL_MINT)
    assert tracker.run_usage["jupiter"].requests == 1
    assert tracker.run_usage["jupiter"].credits == 0  # keyless sources cost no credits
