"""Transaction-history endpoints: adapters, strategy selection, and cost.

The parser (`extract_legs`) is deliberately untouched by the migration, so the
adapters are tested *through* it: a payload from any endpoint must produce the
same swap legs the Enhanced Transactions API produced.
"""

from __future__ import annotations

import pytest
from conftest import FakeResponse, FakeSession

from whale_tracker.budget import BudgetTracker, CreditCosts, ProviderBudget
from whale_tracker.clients.base import ApiError
from whale_tracker.clients.helius import HeliusClient, extract_legs
from whale_tracker.clients.helius_history import (
    STRATEGY_BULK_HISTORY,
    STRATEGY_ENHANCED_TX,
    STRATEGY_PARSED_EVENTS,
    HeliusHistory,
    StrategyUnavailable,
    adapt_parsed_event,
    adapt_raw_transaction,
    adapt_transaction,
    planned_strategies,
    unwrap_list,
)
from whale_tracker.config import WSOL_MINT, load_settings

WALLET = "5xKq8ZTdJ8YQ1Fh3Hs9M7Vp2Nn4Rr6Tt8Uu1Ww3Yy5Zz"
POOL = "PoolAccount11111111111111111111111111111111"
MEME = "MemeTokenMint1111111111111111111111111111111"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
TS = 1_700_000_000


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HELIUS_API_KEY", "test-key")
    for key in ("HELIUS_HISTORY_STRATEGY", "HELIUS_CREDITS_ENHANCED_TX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_MAX_RETRIES", "2")
    return load_settings(override=True)


# ---------------------------------------------------------------------------
# fixtures: documented payload shapes
# ---------------------------------------------------------------------------


def raw_rpc_transaction(*, signature="rawSig1", ts=TS, slot=250_000_000, failed=False):
    """A raw JSON-RPC transaction: 1 SOL -> 1,000,000 MEME.

    This is the shape `getTransactionsForAddress`-style bulk history returns
    when it does not pre-parse. Balances are the authority: the wallet is down
    1 SOL plus the 5,000 lamport fee, and up 1,000,000 MEME.
    """
    return {
        "slot": slot,
        "blockTime": ts,
        "transaction": {
            "signatures": [signature],
            "message": {"accountKeys": [WALLET, POOL, RAYDIUM]},
        },
        "meta": {
            "err": {"InstructionError": [0, "Custom"]} if failed else None,
            "fee": 5_000,
            "preBalances": [2_000_000_000, 5_000_000_000, 0],
            "postBalances": [999_995_000, 6_000_000_000, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 3,
                    "mint": MEME,
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 3,
                    "mint": MEME,
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "1000000000000", "decimals": 6},
                }
            ],
            "innerInstructions": [],
        },
    }


def parsed_event(*, signature="eventSig1", ts=TS, snake_case=False):
    """A flat Parsed Events record for the same swap."""
    if snake_case:
        return {
            "signature": signature,
            "block_time": ts,
            "slot": 250_000_001,
            "type": "SWAP",
            "source": "raydium",
            "fee_payer": WALLET,
            "fee": 5_000,
            "native_input": {"account": WALLET, "amount": "1000000000"},
            "token_outputs": [
                {
                    "userAccount": WALLET,
                    "mint": MEME,
                    "rawTokenAmount": {"tokenAmount": "1000000000000", "decimals": 6},
                }
            ],
        }
    return {
        "signature": signature,
        "timestamp": ts,
        "slot": 250_000_001,
        "type": "SWAP",
        "source": "RAYDIUM",
        "feePayer": WALLET,
        "fee": 5_000,
        "nativeInput": {"account": WALLET, "amount": "1000000000"},
        "tokenOutputs": [
            {
                "userAccount": WALLET,
                "mint": MEME,
                "rawTokenAmount": {"tokenAmount": "1000000000000", "decimals": 6},
            }
        ],
    }


def enhanced_transaction(signature="enhancedSig1", ts=TS):
    """The original Enhanced Transactions shape, unchanged."""
    return {
        "signature": signature,
        "timestamp": ts,
        "slot": 250_000_002,
        "type": "SWAP",
        "source": "RAYDIUM",
        "fee": 5_000,
        "feePayer": WALLET,
        "events": {
            "swap": {
                "nativeInput": {"account": WALLET, "amount": "1000000000"},
                "nativeOutput": None,
                "tokenInputs": [],
                "tokenOutputs": [
                    {
                        "userAccount": WALLET,
                        "mint": MEME,
                        "rawTokenAmount": {"tokenAmount": "1000000000000", "decimals": 6},
                    }
                ],
                "innerSwaps": [],
            }
        },
    }


def assert_is_the_expected_buy(legs):
    assert len(legs) == 1
    leg = legs[0]
    assert leg.wallet == WALLET
    assert leg.mint == MEME
    assert leg.side == "buy"
    assert leg.token_delta == pytest.approx(1_000_000)
    assert leg.quote_mint == WSOL_MINT
    assert leg.quote_delta == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# adapters, verified through the untouched parser
# ---------------------------------------------------------------------------


def test_raw_rpc_transaction_parses_identically_to_enhanced():
    """Same swap, three endpoints, one set of legs."""
    from_raw = extract_legs(adapt_raw_transaction(raw_rpc_transaction()))
    from_event = extract_legs(adapt_parsed_event(parsed_event()))
    from_enhanced = extract_legs(enhanced_transaction())

    for legs in (from_raw, from_event, from_enhanced):
        assert_is_the_expected_buy(legs)

    assert from_raw[0].token_delta == from_enhanced[0].token_delta
    assert from_raw[0].quote_delta == pytest.approx(from_enhanced[0].quote_delta)


def test_raw_adapter_adds_the_fee_back_to_the_sol_leg():
    """The 5,000 lamport fee is a cost of transacting, not part of the swap."""
    adapted = adapt_raw_transaction(raw_rpc_transaction())
    wallet_entry = next(a for a in adapted["accountData"] if a["account"] == WALLET)
    assert wallet_entry["nativeBalanceChange"] == -1_000_005_000
    assert extract_legs(adapted)[0].quote_delta == pytest.approx(-1.0)


def test_raw_adapter_keeps_metadata():
    adapted = adapt_raw_transaction(raw_rpc_transaction())
    assert adapted["signature"] == "rawSig1"
    assert adapted["timestamp"] == TS
    assert adapted["slot"] == 250_000_000
    assert adapted["feePayer"] == WALLET
    assert adapted["source"] == "RAYDIUM"   # inferred from the program id


def test_raw_adapter_skips_failed_transactions():
    """A reverted transaction moved no value and must not become a trade."""
    assert adapt_raw_transaction(raw_rpc_transaction(failed=True)) is None


def test_raw_adapter_handles_versioned_lookup_tables():
    raw = raw_rpc_transaction()
    raw["transaction"]["message"]["accountKeys"] = [
        {"pubkey": WALLET}, {"pubkey": POOL}
    ]
    raw["meta"]["loadedAddresses"] = {"writable": [RAYDIUM], "readonly": []}
    adapted = adapt_raw_transaction(raw)
    assert adapted["feePayer"] == WALLET
    assert adapted["source"] == "RAYDIUM"
    assert_is_the_expected_buy(extract_legs(adapted))


def test_raw_adapter_keeps_owners_absent_from_account_keys():
    raw = raw_rpc_transaction()
    raw["meta"]["postTokenBalances"][0]["owner"] = "OtherOwner111"
    raw["meta"]["preTokenBalances"][0]["owner"] = "OtherOwner111"
    adapted = adapt_raw_transaction(raw)
    assert any(a["account"] == "OtherOwner111" for a in adapted["accountData"])


def test_raw_adapter_ignores_balances_that_did_not_move():
    raw = raw_rpc_transaction()
    raw["meta"]["postTokenBalances"] = raw["meta"]["preTokenBalances"]
    adapted = adapt_raw_transaction(raw)
    assert all(not a["tokenBalanceChanges"] for a in adapted["accountData"])
    assert extract_legs(adapted) == []


def test_parsed_event_snake_case_variant():
    assert_is_the_expected_buy(extract_legs(adapt_parsed_event(parsed_event(snake_case=True))))


def test_parsed_event_nested_swap_variant():
    event = {
        "signature": "nested",
        "timestamp": TS,
        "slot": 1,
        "feePayer": WALLET,
        "swap": parsed_event(),
    }
    assert_is_the_expected_buy(extract_legs(adapt_parsed_event(event)))


def test_parsed_event_passes_enhanced_payloads_through():
    enhanced = enhanced_transaction()
    assert adapt_parsed_event(enhanced) is enhanced


def test_parsed_event_rejects_what_it_cannot_read():
    assert adapt_parsed_event({"signature": "x"}) is None          # no timestamp
    assert adapt_parsed_event({"timestamp": TS}) is None           # no signature
    assert adapt_parsed_event({"signature": "x", "timestamp": TS, "type": "TRANSFER"}) is None
    assert adapt_parsed_event("not a dict") is None


def test_adapt_transaction_dispatches_on_shape():
    assert adapt_transaction(enhanced_transaction())["signature"] == "enhancedSig1"
    assert adapt_transaction(raw_rpc_transaction())["signature"] == "rawSig1"
    assert adapt_transaction(parsed_event())["signature"] == "eventSig1"
    assert adapt_transaction(None) is None


def test_unwrap_list_handles_common_envelopes():
    assert unwrap_list([1, 2]) == [1, 2]
    assert unwrap_list({"data": [1]}) == [1]
    assert unwrap_list({"result": {"transactions": [1, 2]}}) == [1, 2]
    assert unwrap_list({"events": [3]}) == [3]
    assert unwrap_list({"nothing": 1}) == []


# ---------------------------------------------------------------------------
# strategy selection
# ---------------------------------------------------------------------------


def client_with(settings, session, conn=None, budget=None):
    client = HeliusClient(settings, conn, budget)
    client.http.session = session
    return client


def parsed_events_response(records):
    return FakeResponse(200, {"data": records})


def rpc_response(result):
    return FakeResponse(200, {"jsonrpc": "2.0", "id": "whale-tracker", "result": result})


def test_plan_is_cheapest_first(settings):
    assert planned_strategies(settings) == (
        STRATEGY_PARSED_EVENTS,
        STRATEGY_BULK_HISTORY,
        STRATEGY_ENHANCED_TX,
    )


def test_parsed_events_is_used_when_available(settings, conn):
    session = FakeSession(
        routes={"parsed-events": [parsed_events_response([parsed_event()]),
                                  parsed_events_response([])]}
    )
    client = client_with(settings, session, conn)
    txs = list(client.iter_address_transactions("addr", max_txs=10))

    assert len(txs) == 1
    assert client.history_strategy == STRATEGY_PARSED_EVENTS
    assert all("parsed-events" in url for url in session.urls())
    assert_is_the_expected_buy(extract_legs(txs[0]))


def test_falls_back_to_bulk_history_when_parsed_events_404s(settings, conn):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404, text="not found"),
            "helius-rpc.com": [rpc_response([raw_rpc_transaction()]), rpc_response([])],
        }
    )
    client = client_with(settings, session, conn)
    txs = list(client.iter_address_transactions("addr", max_txs=10))

    assert len(txs) == 1
    assert client.history_strategy == STRATEGY_BULK_HISTORY
    assert STRATEGY_PARSED_EVENTS in client.history.unavailable
    assert_is_the_expected_buy(extract_legs(txs[0]))


def test_falls_back_to_enhanced_when_the_rpc_method_is_unknown(settings, conn):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404),
            "helius-rpc.com": FakeResponse(
                200, {"error": {"code": -32601, "message": "Method not found"}}
            ),
            "/v0/addresses/": [
                FakeResponse(200, [enhanced_transaction()]),
                FakeResponse(200, []),
            ],
        }
    )
    client = client_with(settings, session, conn)
    txs = list(client.iter_address_transactions("addr", max_txs=10))

    assert len(txs) == 1
    assert client.history_strategy == STRATEGY_ENHANCED_TX
    assert set(client.history.unavailable) == {STRATEGY_PARSED_EVENTS, STRATEGY_BULK_HISTORY}


def test_feature_gated_403_is_treated_as_unavailable(settings, conn):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(403, text="beta access required for this endpoint"),
            "helius-rpc.com": [rpc_response([raw_rpc_transaction()]), rpc_response([])],
        }
    )
    client = client_with(settings, session, conn)
    assert len(list(client.iter_address_transactions("addr", max_txs=10))) == 1
    assert client.history_strategy == STRATEGY_BULK_HISTORY


def test_a_rate_limit_is_never_treated_as_unavailable(settings, conn, no_sleep):
    """429 means try again, not "this endpoint does not exist".

    Falling back here would pay for the same page twice on the dearer endpoint.
    """
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(429),
            "helius-rpc.com": rpc_response([raw_rpc_transaction()]),
            "/v0/addresses/": FakeResponse(200, [enhanced_transaction()]),
        }
    )
    client = client_with(settings, session, conn)
    with pytest.raises(ApiError) as excinfo:
        list(client.iter_address_transactions("addr", max_txs=10))

    assert excinfo.value.status == 429
    assert all("parsed-events" in url for url in session.urls())
    assert client.history.unavailable == {}


def test_server_errors_do_not_trigger_a_fallback(settings, conn, no_sleep):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(503),
            "helius-rpc.com": rpc_response([raw_rpc_transaction()]),
        }
    )
    client = client_with(settings, session, conn)
    with pytest.raises(ApiError):
        list(client.iter_address_transactions("addr", max_txs=10))
    assert client.history.unavailable == {}


def test_an_unreadable_response_shape_counts_as_unavailable(settings, conn):
    """Answering in a shape we cannot parse must fall back, not ingest nothing."""
    session = FakeSession(
        routes={
            "parsed-events": parsed_events_response([{"totally": "unexpected"}]),
            "helius-rpc.com": [rpc_response([raw_rpc_transaction()]), rpc_response([])],
        }
    )
    client = client_with(settings, session, conn)
    assert len(list(client.iter_address_transactions("addr", max_txs=10))) == 1
    assert client.history_strategy == STRATEGY_BULK_HISTORY
    assert "could not adapt" in client.history.unavailable[STRATEGY_PARSED_EVENTS]


def test_an_explicit_strategy_is_honoured(settings, conn, monkeypatch):
    monkeypatch.setenv("HELIUS_HISTORY_STRATEGY", "enhanced_tx")
    pinned = load_settings(override=True)
    session = FakeSession(
        routes={"/v0/addresses/": [FakeResponse(200, [enhanced_transaction()]),
                                   FakeResponse(200, [])]}
    )
    client = client_with(pinned, session, conn)
    assert len(list(client.iter_address_transactions("addr", max_txs=10))) == 1
    assert client.history_strategy == STRATEGY_ENHANCED_TX
    assert all("/v0/addresses/" in url for url in session.urls())


def test_an_explicit_strategy_still_falls_back_if_it_is_not_enabled(settings, conn, monkeypatch):
    monkeypatch.setenv("HELIUS_HISTORY_STRATEGY", "parsed_events")
    pinned = load_settings(override=True)
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404),
            "helius-rpc.com": [rpc_response([raw_rpc_transaction()]), rpc_response([])],
        }
    )
    client = client_with(pinned, session, conn)
    assert len(list(client.iter_address_transactions("addr", max_txs=10))) == 1
    assert client.history_strategy == STRATEGY_BULK_HISTORY


def test_the_resolved_strategy_is_sticky_across_addresses(settings, conn):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404),
            "helius-rpc.com": [
                rpc_response([raw_rpc_transaction(signature="a")]),
                rpc_response([]),
                rpc_response([raw_rpc_transaction(signature="b")]),
                rpc_response([]),
            ],
        }
    )
    client = client_with(settings, session, conn)
    list(client.iter_address_transactions("addr1", max_txs=10))
    before = len([u for u in session.urls() if "parsed-events" in u])
    list(client.iter_address_transactions("addr2", max_txs=10))
    after = len([u for u in session.urls() if "parsed-events" in u])

    assert before == after == 1  # probed once, never again


def test_a_failure_after_adoption_is_raised_not_re_walked(settings, conn):
    """Switching endpoints mid-walk would re-pay for pages already fetched."""
    session = FakeSession(
        routes={
            "parsed-events": [
                parsed_events_response([parsed_event(signature=f"s{i}") for i in range(2)]),
                FakeResponse(404),
            ],
            "helius-rpc.com": rpc_response([raw_rpc_transaction()]),
        }
    )
    client = client_with(settings, session, conn)
    with pytest.raises(StrategyUnavailable):
        list(client.iter_address_transactions("addr", max_txs=500))
    assert client.history_strategy == STRATEGY_PARSED_EVENTS


def test_pagination_advances_past_records_the_adapter_skipped(settings, conn):
    """A page of failed transactions must move the cursor, not loop forever."""
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404),
            "helius-rpc.com": [
                rpc_response([raw_rpc_transaction(signature="skipped", failed=True)]),
                rpc_response([raw_rpc_transaction(signature="good")]),
                rpc_response([]),
            ],
        }
    )
    client = client_with(settings, session, conn)
    txs = list(client.iter_address_transactions("addr", max_txs=500))
    assert [tx["signature"] for tx in txs] == ["good"]

    cursors = [call["json"]["params"][1].get("before")
               for call in session.calls if "helius-rpc" in call["url"]]
    assert cursors == [None, "skipped", "good"]


def test_a_repeated_cursor_terminates_the_walk(settings, conn):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404),
            "helius-rpc.com": rpc_response([raw_rpc_transaction(signature="same")]),
        }
    )
    client = client_with(settings, session, conn)
    txs = list(client.iter_address_transactions("addr", max_txs=500))
    # The repeated page is not yielded twice, and the repeated cursor ends the walk.
    assert [tx["signature"] for tx in txs] == ["same"]


def test_the_since_window_stops_the_walk(settings, conn):
    old = raw_rpc_transaction(signature="old", ts=TS - 100_000)
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404),
            "helius-rpc.com": rpc_response([raw_rpc_transaction(signature="new"), old]),
        }
    )
    client = client_with(settings, session, conn)
    txs = list(client.iter_address_transactions("addr", max_txs=500, since_ts=TS - 10))
    assert [tx["signature"] for tx in txs] == ["new"]


# ---------------------------------------------------------------------------
# what it costs
# ---------------------------------------------------------------------------


def tracker_for(conn, costs=None):
    return BudgetTracker(conn, {"helius": ProviderBudget("helius")}, costs=costs or CreditCosts())


def test_each_endpoint_is_billed_at_its_own_rate(settings, conn):
    cases = {
        STRATEGY_PARSED_EVENTS: (
            {"parsed-events": [parsed_events_response([parsed_event()]),
                               parsed_events_response([])]},
            10,
        ),
        STRATEGY_BULK_HISTORY: (
            {"helius-rpc.com": [rpc_response([raw_rpc_transaction()]), rpc_response([])]},
            10,
        ),
        STRATEGY_ENHANCED_TX: (
            {"/v0/addresses/": [FakeResponse(200, [enhanced_transaction()]),
                                FakeResponse(200, [])]},
            100,
        ),
    }
    for strategy, (routes, cost_per_page) in cases.items():
        tracker = tracker_for(conn, settings.credit_costs())
        pinned = type(settings)(**{**settings.__dict__, "helius_history_strategy": strategy})
        client = client_with(pinned, FakeSession(routes=routes), conn, tracker)
        list(client.iter_address_transactions(f"addr-{strategy}", max_txs=100))

        usage = tracker.run_usage["helius"]
        assert usage.credits == cost_per_page * usage.requests, strategy


def test_the_cheap_path_costs_a_tenth_of_enhanced(settings, conn):
    """The migration, measured: same pages, one tenth the credits."""

    def credits_for(strategy, routes):
        tracker = tracker_for(conn, settings.credit_costs())
        pinned = type(settings)(**{**settings.__dict__, "helius_history_strategy": strategy})
        client = client_with(pinned, FakeSession(routes=routes), conn, tracker)
        list(client.iter_address_transactions(f"addr-{strategy}-cmp", max_txs=100))
        return tracker.run_usage["helius"].credits, tracker.run_usage["helius"].requests

    cheap_credits, cheap_calls = credits_for(
        STRATEGY_PARSED_EVENTS,
        {"parsed-events": [parsed_events_response([parsed_event()]), parsed_events_response([])]},
    )
    dear_credits, dear_calls = credits_for(
        STRATEGY_ENHANCED_TX,
        {"/v0/addresses/": [FakeResponse(200, [enhanced_transaction()]), FakeResponse(200, [])]},
    )
    assert cheap_calls == dear_calls
    assert dear_credits == 10 * cheap_credits


def test_history_reports_what_it_settled_on(settings, conn):
    session = FakeSession(
        routes={
            "parsed-events": FakeResponse(404, text="not found"),
            "helius-rpc.com": [rpc_response([raw_rpc_transaction()]), rpc_response([])],
        }
    )
    client = client_with(settings, session, conn)
    list(client.iter_address_transactions("addr", max_txs=10))

    assert isinstance(client.history, HeliusHistory)
    assert client.history.resolved == STRATEGY_BULK_HISTORY
    assert "404" in client.history.unavailable[STRATEGY_PARSED_EVENTS]


# ---------------------------------------------------------------------------
# end to end through ingestion
# ---------------------------------------------------------------------------


def test_ingest_works_end_to_end_on_the_cheap_endpoint(settings, conn, monkeypatch):
    """Nothing downstream of the parser knows the endpoint changed."""
    from whale_tracker.ingest import PriceOracle, ingest_token

    monkeypatch.setenv("MIN_TRADE_USD", "0")
    live = load_settings(override=True)
    session = FakeSession(
        routes={
            "parsed-events": [
                parsed_events_response(
                    [parsed_event(signature=f"sig{i}", ts=TS + i) for i in range(3)]
                ),
                parsed_events_response([]),
            ],
            "helius-rpc.com": rpc_response({"content": {"metadata": {"symbol": "MEME"}}}),
        }
    )
    client = client_with(live, session, conn)
    result = ingest_token(
        conn,
        MEME,
        settings=live,
        helius=client,
        oracle=PriceOracle(conn, static_sol_price=150.0),
        max_txs=100,
    )

    assert result.trades_written == 3
    assert client.history_strategy == STRATEGY_PARSED_EVENTS
    row = conn.execute("SELECT * FROM trades LIMIT 1").fetchone()
    assert row["side"] == "buy"
    assert row["value_usd"] == pytest.approx(150.0)
    assert row["token_amount"] == pytest.approx(1_000_000)
