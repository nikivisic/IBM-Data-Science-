"""Provider clients: response parsing, rate limiting, retries and caching."""

from __future__ import annotations

import time

import pytest

from whale_tracker.clients.base import ApiError, HttpClient, RateLimiter
from whale_tracker.clients.birdeye import normalise_trade, overview_to_token_row
from whale_tracker.clients.helius import candidate_wallets, extract_legs

from conftest import FakeResponse, FakeSession
from whale_tracker.config import WSOL_MINT

WALLET = "5xKq8ZTdJ8YQ1Fh3Hs9M7Vp2Nn4Rr6Tt8Uu1Ww3Yy5Zz"
MEME = "MemeTokenMint1111111111111111111111111111111"
POOL = "PoolAccount11111111111111111111111111111111"


def swap_tx(*, buy: bool = True, signature: str = "sig1", ts: int = 1_700_000_000, slot: int = 250_000_000):
    """A Helius enhanced SWAP transaction: 1 SOL in, 1,000,000 MEME out (or the reverse)."""
    lamports = "1000000000"          # 1 SOL
    raw_tokens = {"tokenAmount": "1000000000000", "decimals": 6}  # 1,000,000 MEME
    swap = {
        "nativeInput": {"account": WALLET, "amount": lamports} if buy else None,
        "nativeOutput": None if buy else {"account": WALLET, "amount": lamports},
        "tokenInputs": []
        if buy
        else [{"userAccount": WALLET, "mint": MEME, "rawTokenAmount": raw_tokens}],
        "tokenOutputs": [{"userAccount": WALLET, "mint": MEME, "rawTokenAmount": raw_tokens}]
        if buy
        else [],
        "innerSwaps": [],
    }
    return {
        "signature": signature,
        "timestamp": ts,
        "slot": slot,
        "type": "SWAP",
        "source": "RAYDIUM",
        "fee": 5_000,
        "feePayer": WALLET,
        "tokenTransfers": [
            {
                "fromUserAccount": POOL if buy else WALLET,
                "toUserAccount": WALLET if buy else POOL,
                "mint": MEME,
                "tokenAmount": 1_000_000,
            }
        ],
        "nativeTransfers": [],
        "accountData": [],
        "events": {"swap": swap},
    }


def test_extract_buy_leg():
    legs = extract_legs(swap_tx(buy=True))
    assert len(legs) == 1
    leg = legs[0]
    assert leg.wallet == WALLET
    assert leg.mint == MEME
    assert leg.side == "buy"
    assert leg.token_delta == pytest.approx(1_000_000)
    assert leg.quote_mint == WSOL_MINT
    assert leg.quote_delta == pytest.approx(-1.0)
    assert leg.slot == 250_000_000
    assert leg.source == "helius"


def test_extract_sell_leg():
    legs = extract_legs(swap_tx(buy=False))
    assert legs[0].side == "sell"
    assert legs[0].token_delta == pytest.approx(-1_000_000)
    assert legs[0].quote_delta == pytest.approx(1.0)


def test_extract_uses_inner_swaps_for_aggregator_routes():
    tx = swap_tx()
    inner = tx["events"]["swap"]
    tx["events"]["swap"] = {
        "nativeInput": None,
        "nativeOutput": None,
        "tokenInputs": [],
        "tokenOutputs": [],
        "innerSwaps": [inner],
    }
    legs = extract_legs(tx)
    assert len(legs) == 1
    assert legs[0].token_delta == pytest.approx(1_000_000)


def test_extract_falls_back_to_balance_changes():
    """No decoded swap event: reconstruct from account balance deltas."""
    tx = swap_tx()
    del tx["events"]
    tx["accountData"] = [
        {
            "account": WALLET,
            "nativeBalanceChange": -1_000_005_000,  # 1 SOL plus the 5,000 lamport fee
            "tokenBalanceChanges": [],
        },
        {
            "account": "TokenAccount111",
            "nativeBalanceChange": 0,
            "tokenBalanceChanges": [
                {
                    "userAccount": WALLET,
                    "mint": MEME,
                    "rawTokenAmount": {"tokenAmount": "1000000000000", "decimals": 6},
                }
            ],
        },
    ]
    leg = extract_legs(tx)[0]
    assert leg.side == "buy"
    assert leg.quote_delta == pytest.approx(-1.0)  # the fee is added back


def test_extract_ignores_memecoin_to_memecoin_swaps():
    """No SOL or stable leg means no unambiguous USD basis — skip it."""
    tx = swap_tx()
    tx["events"]["swap"] = {
        "nativeInput": None,
        "nativeOutput": None,
        "tokenInputs": [
            {"userAccount": WALLET, "mint": "OtherMeme111", "rawTokenAmount": {"tokenAmount": "5", "decimals": 0}}
        ],
        "tokenOutputs": [
            {"userAccount": WALLET, "mint": MEME, "rawTokenAmount": {"tokenAmount": "5", "decimals": 0}}
        ],
        "innerSwaps": [],
    }
    assert extract_legs(tx) == []


def test_extract_can_be_restricted_to_one_mint():
    assert extract_legs(swap_tx(), mints={"SomethingElse"}) == []
    assert len(extract_legs(swap_tx(), mints={MEME})) == 1


def test_extract_rejects_transactions_without_identity():
    assert extract_legs({"timestamp": 1, "events": {}}) == []
    assert extract_legs({"signature": "s", "events": {}}) == []


def test_candidate_wallets_puts_the_fee_payer_first():
    assert candidate_wallets(swap_tx())[0] == WALLET


def birdeye_item(side="buy"):
    return {
        "txHash": "birdeyeSig",
        "blockUnixTime": 1_700_000_100,
        "blockNumber": 250_000_100,
        "source": "raydium",
        "owner": WALLET,
        "side": side,
        "base": {
            "address": MEME,
            "decimals": 6,
            "uiChangeAmount": 500_000 if side == "buy" else -500_000,
            "price": 0.00025,
        },
        "quote": {
            "address": WSOL_MINT,
            "decimals": 9,
            "uiChangeAmount": -0.83 if side == "buy" else 0.83,
            "price": 150.0,
        },
    }


def test_birdeye_buy_normalisation():
    leg = normalise_trade(birdeye_item("buy"), MEME)
    assert leg.side == "buy"
    assert leg.token_delta == pytest.approx(500_000)
    assert leg.quote_delta == pytest.approx(-0.83)
    assert leg.price_usd_hint == pytest.approx(0.00025)
    assert leg.source == "birdeye"


def test_birdeye_sell_normalisation():
    leg = normalise_trade(birdeye_item("sell"), MEME)
    assert leg.side == "sell"
    assert leg.token_delta == pytest.approx(-500_000)


def test_birdeye_handles_reversed_base_and_quote():
    item = birdeye_item("buy")
    item["base"], item["quote"] = item["quote"], item["base"]
    leg = normalise_trade(item, MEME)
    assert leg.mint == MEME
    assert leg.token_delta == pytest.approx(500_000)


def test_birdeye_infers_sign_from_side_when_amounts_are_unsigned():
    item = birdeye_item("sell")
    item["base"]["uiChangeAmount"] = 500_000
    item["quote"]["uiChangeAmount"] = 0.83
    leg = normalise_trade(item, MEME)
    assert leg.side == "sell"


def test_birdeye_skips_unquotable_and_malformed_items():
    item = birdeye_item()
    item["quote"]["address"] = "SomeRandomMeme111"
    assert normalise_trade(item, MEME) is None
    assert normalise_trade(birdeye_item(), "NotInThisTrade") is None
    headless = birdeye_item()
    headless["owner"] = ""
    assert normalise_trade(headless, MEME) is None


def test_overview_maps_onto_token_columns():
    row = overview_to_token_row(MEME, {"symbol": "MEME", "name": "Meme", "decimals": 6, "mc": 1e6})
    assert row["mint"] == MEME and row["symbol"] == "MEME" and row["decimals"] == 6
    assert row["meta_json"]["mc"] == 1e6


def test_rate_limiter_paces_calls():
    limiter = RateLimiter(rps=50)
    started = time.monotonic()
    for _ in range(11):
        limiter.acquire()
    assert time.monotonic() - started >= 0.15


def make_client(session, conn=None, **kwargs):
    return HttpClient(
        "https://api.test",
        provider="test",
        rate_limit_rps=1_000,
        max_retries=3,
        session=session,
        conn=conn,
        **kwargs,
    )


def test_http_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("whale_tracker.clients.base.time.sleep", lambda _s: None)
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"ok": True})])
    assert make_client(session).get("/x") == {"ok": True}
    assert len(session.calls) == 2


def test_http_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("whale_tracker.clients.base.time.sleep", lambda _s: None)
    session = FakeSession([FakeResponse(503) for _ in range(3)])
    with pytest.raises(ApiError) as excinfo:
        make_client(session).get("/x")
    assert "after 3 attempts" in str(excinfo.value)


def test_http_reports_a_bad_key_immediately():
    session = FakeSession([FakeResponse(401, text="bad key")])
    with pytest.raises(ApiError) as excinfo:
        make_client(session).get("/x")
    assert "API key" in str(excinfo.value)
    assert len(session.calls) == 1


def test_http_cache_avoids_a_second_call(conn):
    session = FakeSession([FakeResponse(200, {"n": 1})])
    client = make_client(session, conn=conn, cache_ttl_seconds=600)
    assert client.get("/x", params={"api-key": "secret"}) == {"n": 1}
    assert client.get("/x", params={"api-key": "secret"}) == {"n": 1}
    assert len(session.calls) == 1
    assert client.stats() == {"calls": 1, "cache_hits": 1}
    # The key must never be stored in the clear.
    cached = conn.execute("SELECT key, payload FROM http_cache").fetchone()
    assert "secret" not in cached["key"] and "secret" not in cached["payload"]


def test_http_rejects_non_json():
    session = FakeSession([FakeResponse(200, None, text="<html>")])
    with pytest.raises(ApiError):
        make_client(session).get("/x")


def test_errors_never_leak_the_api_key(monkeypatch):
    """`requests` puts the full URL in its exception text — scrub it."""
    import requests as requests_module

    monkeypatch.setattr("whale_tracker.clients.base.time.sleep", lambda _s: None)

    class ExplodingSession(FakeSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            raise requests_module.ConnectionError(
                f"failed to reach {url}?api-key=super-secret-key-1234"
            )

    client = make_client(ExplodingSession([]))
    with pytest.raises(ApiError) as excinfo:
        client.get("/x", params={"api-key": "super-secret-key-1234"})
    assert "super-secret-key-1234" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_header_keys_are_scrubbed_from_error_bodies():
    session = FakeSession([FakeResponse(500, text="rejected key header-secret-value")])
    client = HttpClient(
        "https://api.test",
        provider="test",
        rate_limit_rps=1_000,
        max_retries=1,
        session=session,
        default_headers={"X-API-KEY": "header-secret-value"},
    )
    with pytest.raises(ApiError) as excinfo:
        client.get("/x")
    assert "header-secret-value" not in excinfo.value.body
