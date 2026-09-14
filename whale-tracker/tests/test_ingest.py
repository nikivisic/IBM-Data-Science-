"""Ingestion: pricing the quote leg and turning provider payloads into trades."""

from __future__ import annotations

import pytest

from whale_tracker.config import USDC_MINT, WSOL_MINT, load_settings
from whale_tracker.db import upsert_price_points
from whale_tracker.ingest import (
    PriceOracle,
    candidate_wallets,
    expand_wallet,
    ingest_token,
    leg_to_trade,
)
from whale_tracker.models import SwapLeg

MEME = "MemeMint1111111111111111111111111111111111"
WALLET = "Wallet111111111111111111111111111111111111"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIN_TRADE_USD", "10")
    return load_settings()


def leg(**kwargs):
    base = dict(
        signature="sig1",
        wallet=WALLET,
        mint=MEME,
        token_delta=1_000_000.0,
        quote_mint=WSOL_MINT,
        quote_delta=-1.0,
        ts=1_700_000_000,
        slot=250_000_000,
        dex="RAYDIUM",
        source="helius",
    )
    base.update(kwargs)
    return SwapLeg(**base)


def test_oracle_prices_stablecoin_legs_at_par(conn):
    oracle = PriceOracle(conn)
    assert oracle.quote_usd(USDC_MINT, -250.0, 1_700_000_000) == pytest.approx(250.0)


def test_oracle_uses_cached_sol_prices(conn):
    upsert_price_points(conn, [{"mint": WSOL_MINT, "ts_bucket": 1_699_999_200, "price_usd": 140.0}])
    conn.commit()
    oracle = PriceOracle(conn)
    assert oracle.quote_usd(WSOL_MINT, -2.0, 1_700_000_000) == pytest.approx(280.0)


def test_oracle_falls_back_to_a_static_price_then_gives_up(conn):
    assert PriceOracle(conn, static_sol_price=150.0).quote_usd(WSOL_MINT, -1.5, 1) == pytest.approx(225.0)
    oracle = PriceOracle(conn)
    assert oracle.quote_usd(WSOL_MINT, -1.0, 1) is None
    assert oracle.quote_usd("SomeOtherMint", -1.0, 1) is None
    assert oracle.misses == 1


def test_leg_to_trade_computes_usd_price(conn):
    oracle = PriceOracle(conn, static_sol_price=150.0)
    trade = leg_to_trade(leg(), oracle)
    assert trade.side == "buy"
    assert trade.value_usd == pytest.approx(150.0)
    assert trade.price_usd == pytest.approx(150.0 / 1_000_000)
    assert trade.token_amount == pytest.approx(1_000_000)
    assert trade.quote_amount == pytest.approx(1.0)


def test_leg_to_trade_falls_back_to_the_provider_price_hint(conn):
    oracle = PriceOracle(conn)  # cannot price SOL
    trade = leg_to_trade(leg(price_usd_hint=0.0002), oracle)
    assert trade.value_usd == pytest.approx(200.0)


def test_unpriceable_legs_are_dropped(conn):
    assert leg_to_trade(leg(), PriceOracle(conn)) is None
    assert leg_to_trade(leg(token_delta=0.0), PriceOracle(conn, static_sol_price=150.0)) is None


class FakeHelius:
    """Stands in for the Helius client with a canned transaction list."""

    def __init__(self, transactions):
        self.transactions = transactions
        self.requests = []

    def iter_address_transactions(self, address, *, max_txs=5_000, tx_type=None, since_ts=None):
        self.requests.append({"address": address, "max_txs": max_txs, "since_ts": since_ts})
        for tx in self.transactions[:max_txs]:
            if since_ts is not None and int(tx.get("timestamp", 0)) < since_ts:
                return
            yield tx

    def token_metadata(self, mint):
        return {"symbol": "MEME", "name": "Meme Token", "decimals": 6}


def swap(wallet, *, buy=True, signature="s1", ts=1_700_000_000, slot=250_000_000, sol="1000000000",
         tokens="1000000000000", mint=MEME):
    raw = {"tokenAmount": tokens, "decimals": 6}
    return {
        "signature": signature,
        "timestamp": ts,
        "slot": slot,
        "type": "SWAP",
        "source": "RAYDIUM",
        "fee": 5_000,
        "feePayer": wallet,
        "events": {
            "swap": {
                "nativeInput": {"account": wallet, "amount": sol} if buy else None,
                "nativeOutput": None if buy else {"account": wallet, "amount": sol},
                "tokenInputs": [] if buy else [{"userAccount": wallet, "mint": mint, "rawTokenAmount": raw}],
                "tokenOutputs": [{"userAccount": wallet, "mint": mint, "rawTokenAmount": raw}] if buy else [],
                "innerSwaps": [],
            }
        },
    }


def test_ingest_token_writes_trades_and_token_stats(conn, settings):
    helius = FakeHelius(
        [
            swap("walletA", buy=True, signature="a1", ts=1_700_000_100, slot=250_000_010),
            swap("walletA", buy=False, signature="a2", ts=1_700_003_000, slot=250_007_000),
            swap("walletB", buy=True, signature="b1", ts=1_700_000_000, slot=250_000_000),
        ]
    )
    oracle = PriceOracle(conn, static_sol_price=150.0)
    result = ingest_token(conn, MEME, settings=settings, helius=helius, oracle=oracle)

    assert result.txs_seen == 3
    assert result.trades_written == 3
    assert result.wallets == 2
    assert result.unpriced == 0

    token = conn.execute("SELECT * FROM tokens WHERE mint = ?", (MEME,)).fetchone()
    assert token["symbol"] == "MEME"
    assert token["launch_ts"] == 1_700_000_000
    assert token["launch_slot"] == 250_000_000
    assert token["wallet_count"] == 2

    run = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "ok" and run["trades_new"] == 3


def test_ingest_is_idempotent(conn, settings):
    helius = FakeHelius([swap("walletA", signature="dup")])
    oracle = PriceOracle(conn, static_sol_price=150.0)
    first = ingest_token(conn, MEME, settings=settings, helius=helius, oracle=oracle)
    second = ingest_token(conn, MEME, settings=settings, helius=helius, oracle=oracle)
    assert first.trades_written == 1
    assert second.trades_written == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"] == 1


def test_ingest_drops_dust_below_the_floor(conn, settings):
    tiny = swap("walletA", sol="10000000")  # 0.01 SOL ≈ $1.50, under MIN_TRADE_USD
    oracle = PriceOracle(conn, static_sol_price=150.0)
    result = ingest_token(conn, MEME, settings=settings, helius=FakeHelius([tiny]), oracle=oracle)
    assert result.trades_written == 0


def test_ingest_records_failures_against_the_run(conn, settings):
    class Broken(FakeHelius):
        def iter_address_transactions(self, *args, **kwargs):
            raise RuntimeError("upstream exploded")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        ingest_token(conn, MEME, settings=settings, helius=Broken([]),
                     oracle=PriceOracle(conn, static_sol_price=150.0))
    run = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "error" and "upstream exploded" in run["detail"]


def test_ingest_needs_a_provider(conn, settings):
    with pytest.raises(ValueError):
        ingest_token(conn, MEME, settings=settings)


def test_expand_wallet_pulls_other_tokens(conn, settings):
    other = "OtherMint11111111111111111111111111111111"
    helius = FakeHelius(
        [
            swap(WALLET, buy=True, signature="x1", mint=MEME),
            swap(WALLET, buy=True, signature="x2", mint=other, ts=1_700_010_000),
            swap("SomebodyElse", buy=True, signature="x3", mint=other, ts=1_700_020_000),
        ]
    )
    result = expand_wallet(
        conn, WALLET, settings=settings, helius=helius,
        oracle=PriceOracle(conn, static_sol_price=150.0),
    )
    assert result.trades_written == 2  # the third transaction belongs to another wallet
    mints = {r["mint"] for r in conn.execute("SELECT DISTINCT mint FROM trades")}
    assert mints == {MEME, other}


def test_candidate_wallets_filters_on_breadth_and_size(conn, settings, trade_factory):
    from whale_tracker.db import insert_trades

    rows = []
    for i in range(3):
        rows.append(trade_factory("broad", f"m{i}", "buy", 1_000, 500.0, 1_000 + i))
    rows.append(trade_factory("narrow", "m0", "buy", 1_000, 5_000.0, 1_000))
    insert_trades(conn, rows)
    conn.commit()

    assert candidate_wallets(conn, min_tokens=2, min_trades=2) == ["broad"]
    assert set(candidate_wallets(conn, min_tokens=1, min_trades=1)) == {"broad", "narrow"}
    assert candidate_wallets(conn, min_tokens=1, min_trades=1, min_volume_usd=2_000) == ["narrow"]
