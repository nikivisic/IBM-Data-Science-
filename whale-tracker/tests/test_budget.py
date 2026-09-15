"""API budget metering and hard caps."""

from __future__ import annotations

import pytest
from conftest import FakeResponse, FakeSession

from whale_tracker.budget import (
    BudgetExceeded,
    BudgetTracker,
    CreditCosts,
    ProviderBudget,
    month_to_date,
    utc_day,
)
from whale_tracker.clients.base import HttpClient


# --- credit costs ---------------------------------------------------------


def test_credit_costs_follow_the_published_rates():
    costs = CreditCosts()
    assert costs.for_call(kind="rpc", method="getBalance") == 1
    assert costs.for_call(kind="rpc", method="getProgramAccounts") == 10
    assert costs.for_call(kind="rpc", method="getAsset") == 10
    assert costs.for_call(kind="rpc", method="searchAssets") == 10
    assert costs.for_call(kind="enhanced_tx") == 100
    assert costs.for_call(kind="free") == 0


def test_credit_costs_are_case_insensitive_and_configurable():
    assert CreditCosts().for_call(kind="rpc", method="GETPROGRAMACCOUNTS") == 10
    cheap = CreditCosts(rpc=1, heavy_rpc=2, das=3, enhanced_tx=4)
    assert cheap.for_call(kind="rpc", method="getAsset") == 3
    assert cheap.for_call(kind="enhanced_tx") == 4


# --- tracking -------------------------------------------------------------


def tracker_for(conn, **limits):
    budget = ProviderBudget("helius", **limits)
    return BudgetTracker(conn, {"helius": budget})


def test_consume_accumulates_and_persists(conn):
    tracker = tracker_for(conn)
    tracker.consume("helius", kind="enhanced_tx")
    tracker.consume("helius", kind="rpc", method="getAsset")

    assert tracker.run_usage["helius"].requests == 2
    assert tracker.run_usage["helius"].credits == 110

    row = conn.execute(
        "SELECT * FROM api_usage WHERE provider = 'helius' AND day = ?", (utc_day(),)
    ).fetchone()
    assert row["requests"] == 2
    assert row["credits"] == 110


def test_request_cap_per_run_aborts_before_the_call(conn):
    tracker = tracker_for(conn, max_requests_per_run=2)
    tracker.consume("helius", kind="rpc")
    tracker.consume("helius", kind="rpc")
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.consume("helius", kind="rpc")
    assert excinfo.value.provider == "helius"
    assert excinfo.value.limit_name == "requests/run"
    # The refused call was not recorded.
    assert tracker.run_usage["helius"].requests == 2


def test_credit_cap_per_run(conn):
    tracker = tracker_for(conn, max_credits_per_run=250)
    tracker.consume("helius", kind="enhanced_tx")  # 100
    tracker.consume("helius", kind="enhanced_tx")  # 200
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.consume("helius", kind="enhanced_tx")  # would be 300
    assert excinfo.value.limit_name == "credits/run"
    assert "credits/run budget exhausted" in str(excinfo.value)


def test_daily_cap_counts_usage_from_earlier_runs(conn):
    """A fresh process must not get a fresh daily allowance."""
    conn.execute(
        "INSERT INTO api_usage(provider, day, requests, credits) VALUES ('helius', ?, 900, 90000)",
        (utc_day(),),
    )
    conn.commit()

    tracker = tracker_for(conn, max_requests_per_day=1_000, max_credits_per_day=100_000)
    assert tracker.day_usage("helius").requests == 900
    for _ in range(100):
        tracker.consume("helius", kind="rpc")
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.consume("helius", kind="rpc")
    assert excinfo.value.limit_name == "requests/day"


def test_remaining_headroom_reported(conn):
    tracker = tracker_for(conn, max_requests_per_run=10, max_credits_per_run=1_000)
    tracker.consume("helius", kind="enhanced_tx")
    remaining = tracker.remaining("helius")
    assert remaining["requests/run"] == 9
    assert remaining["credits/run"] == 900
    assert remaining["requests/day"] is None  # uncapped


def test_enforcement_can_be_switched_off(conn):
    tracker = BudgetTracker(
        conn, {"helius": ProviderBudget("helius", max_requests_per_run=1)}, enforce=False
    )
    for _ in range(5):
        tracker.consume("helius", kind="rpc")
    assert tracker.run_usage["helius"].requests == 5  # still counted, just not capped


def test_zero_or_missing_limits_mean_unlimited(conn):
    tracker = tracker_for(conn, max_requests_per_run=0)
    for _ in range(50):
        tracker.consume("helius", kind="rpc")
    assert tracker.run_usage["helius"].requests == 50


def test_month_to_date_sums_days(conn):
    day = utc_day()
    month = day[:7]
    conn.executemany(
        "INSERT INTO api_usage(provider, day, requests, credits) VALUES (?,?,?,?)",
        [
            ("helius", f"{month}-01", 10, 1_000),
            ("helius", f"{month}-02", 5, 500),
            ("jupiter", f"{month}-02", 7, 0),
        ],
    )
    conn.commit()
    totals = month_to_date(conn, month=month)
    assert totals["helius"].requests == 15
    assert totals["helius"].credits == 1_500
    assert totals["jupiter"].requests == 7


# --- integration with the HTTP client ------------------------------------


def make_client(conn, tracker, session, **kwargs):
    return HttpClient(
        "https://api.test",
        provider="helius",
        rate_limit_rps=1_000,
        max_retries=1,
        session=session,
        conn=conn,
        budget=tracker,
        **kwargs,
    )


def test_http_client_charges_the_right_cost_per_call(conn):
    tracker = tracker_for(conn)
    session = FakeSession(default=FakeResponse(200, {"ok": True}))
    client = make_client(conn, tracker, session, cost_kind="enhanced_tx")

    client.get("/v0/addresses/x/transactions", use_cache=False)
    client.get("/rpc", cost_kind="rpc", cost_method="getAsset", use_cache=False)

    assert tracker.run_usage["helius"].credits == 110
    assert tracker.run_usage["helius"].requests == 2


def test_http_client_refuses_the_call_when_the_cap_is_hit(conn):
    tracker = tracker_for(conn, max_credits_per_run=100)
    session = FakeSession(default=FakeResponse(200, {"ok": True}))
    client = make_client(conn, tracker, session, cost_kind="enhanced_tx")

    client.get("/first", use_cache=False)
    with pytest.raises(BudgetExceeded):
        client.get("/second", use_cache=False)
    # Crucially, the refused call never reached the network.
    assert len(session.calls) == 1


def test_retries_are_charged_because_they_reach_the_provider(conn, no_sleep):
    tracker = tracker_for(conn)
    session = FakeSession(
        [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"ok": True})]
    )
    client = HttpClient(
        "https://api.test",
        provider="helius",
        rate_limit_rps=1_000,
        max_retries=3,
        session=session,
        conn=conn,
        budget=tracker,
        cost_kind="rpc",
    )
    client.get("/x", use_cache=False)
    assert tracker.run_usage["helius"].requests == 2


def test_cache_hits_are_free_but_counted(conn):
    tracker = tracker_for(conn)
    session = FakeSession([FakeResponse(200, {"n": 1})])
    client = make_client(conn, tracker, session, cache_ttl_seconds=600, cost_kind="enhanced_tx")

    client.get("/x")
    client.get("/x")

    usage = tracker.run_usage["helius"]
    assert usage.requests == 1
    assert usage.credits == 100
    assert usage.cache_hits == 1
    assert len(session.calls) == 1


def test_exhausted_retries_report_the_status_for_cooldowns(conn, no_sleep):
    from whale_tracker.clients.base import ApiError

    session = FakeSession([FakeResponse(429) for _ in range(3)])
    client = HttpClient(
        "https://api.test", provider="x", rate_limit_rps=1_000, max_retries=3, session=session
    )
    with pytest.raises(ApiError) as excinfo:
        client.get("/x")
    assert excinfo.value.status == 429


# --- end to end: a real ingest hitting its cap ----------------------------


def parsed_swap(wallet, signature, ts, mint):
    """A minimal Helius enhanced SWAP payload."""
    raw = {"tokenAmount": "1000000000000", "decimals": 6}
    return {
        "signature": signature,
        "timestamp": ts,
        "slot": ts * 2,
        "type": "SWAP",
        "source": "RAYDIUM",
        "fee": 5_000,
        "feePayer": wallet,
        "events": {
            "swap": {
                "nativeInput": {"account": wallet, "amount": "1000000000"},
                "nativeOutput": None,
                "tokenInputs": [],
                "tokenOutputs": [{"userAccount": wallet, "mint": mint, "rawTokenAmount": raw}],
                "innerSwaps": [],
            }
        },
    }


def test_ingest_stops_cleanly_at_the_cap_and_keeps_what_it_pulled(conn, tmp_path, monkeypatch):
    """The whole point of the cap: stop, say why, and do not lose the pages already fetched."""
    from whale_tracker.clients.helius import HeliusClient
    from whale_tracker.config import load_settings
    from whale_tracker.ingest import PriceOracle, ingest_token

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HELIUS_API_KEY", "test-key")
    monkeypatch.setenv("MIN_TRADE_USD", "0")
    # Pinned to the 100-credit endpoint so the arithmetic below is about the
    # cap, not about which endpoint served the pages.
    monkeypatch.setenv("HELIUS_HISTORY_STRATEGY", "enhanced_tx")
    settings = load_settings(override=True)

    mint = "MemeMint1111111111111111111111111111111111"
    # Distinct pages, so pagination really advances (identical pages would be
    # served from the HTTP cache and cost nothing, which is its own feature).
    pages = [
        [
            parsed_swap(
                f"wallet{page_no}{i:03d}",
                f"sig{page_no}{i:05d}",
                1_700_000_000 + page_no * 1_000 + i,
                mint,
            )
            for i in range(100)
        ]
        for page_no in range(10)
    ]
    session = FakeSession(
        [FakeResponse(200, page) for page in pages],
        # Token metadata goes to the RPC endpoint (DAS getAsset, 10 credits)
        # before any page is pulled; keep it off the pagination queue.
        routes={
            "helius-rpc.com": FakeResponse(
                200,
                {
                    "result": {
                        "content": {"metadata": {"symbol": "MEME", "name": "Meme"}},
                        "token_info": {"decimals": 6},
                    }
                },
            )
        },
    )

    # 250 credits allows two Enhanced Transactions pages (100 each); the third
    # would take us to 300 and must be refused before it is sent.
    tracker = BudgetTracker(
        conn,
        {"helius": ProviderBudget("helius", max_credits_per_run=250)},
        costs=settings.credit_costs(),
    )
    helius = HeliusClient(settings, conn, tracker)
    helius.http.session = session

    with pytest.raises(BudgetExceeded) as excinfo:
        ingest_token(
            conn,
            mint,
            settings=settings,
            helius=helius,
            oracle=PriceOracle(conn, static_sol_price=150.0),
            max_txs=1_000,
        )

    assert excinfo.value.provider == "helius"
    assert excinfo.value.limit_name == "credits/run"
    # One DAS metadata call (10) plus two Enhanced Transactions pages (100
    # each) = 210; the next page would reach 310 and never went out.
    assert len(session.calls) == 3
    assert tracker.run_usage["helius"].credits == 210

    # Everything fetched before the stop was kept.
    stored = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
    assert stored == 200  # two full pages of parsed swaps
    run_row = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run_row["status"] == "error"
    assert "credits/run" in run_row["detail"]
