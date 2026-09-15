"""Dry-run projections: the numbers you size a free tier against."""

from __future__ import annotations

import pytest

from whale_tracker.budget import BudgetTracker, ProviderBudget, utc_day
from whale_tracker.config import load_settings
from whale_tracker.estimate import (
    HELIUS_PAGE_SIZE,
    budget_check,
    estimate_expand,
    estimate_ingest,
    render,
)

NOW = 1_800_000_000


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("HELIUS_API_KEY", "BIRDEYE_API_KEY", "ENABLE_BIRDEYE", "PRICE_SOURCES"):
        monkeypatch.delenv(key, raising=False)
    return load_settings()


def by_provider(estimate):
    return estimate.totals()


def history_item(estimate):
    return next(i for i in estimate.items if "history pages" in i.label)


def test_ingest_projection_is_pages_times_cost(settings):
    """The default projection uses the cheapest history endpoint, not Enhanced."""
    estimate = estimate_ingest(settings, ["m1", "m2"], max_txs=1_000, now_ts=NOW)
    pages_per_token = 1_000 // HELIUS_PAGE_SIZE
    tape = history_item(estimate)

    assert tape.calls == 2 * pages_per_token
    assert tape.credits_each == settings.helius_credits_parsed_events
    assert tape.credits == 2 * pages_per_token * 10


def test_default_projection_is_ten_times_cheaper_than_enhanced(settings):
    """The whole point of the migration, asserted as a number."""
    cheap = estimate_ingest(settings, ["m1"], max_txs=5_000, now_ts=NOW)
    enhanced = estimate_ingest(
        settings, ["m1"], max_txs=5_000, strategy="enhanced_tx", now_ts=NOW
    )
    assert history_item(enhanced).credits == 10 * history_item(cheap).credits
    assert enhanced.totals()["helius"]["requests"] == cheap.totals()["helius"]["requests"]


def test_projection_follows_an_explicit_strategy(settings):
    for strategy, expected in (
        ("parsed_events", 10),
        ("bulk_history", 10),
        ("enhanced_tx", 100),
    ):
        estimate = estimate_ingest(settings, ["m1"], max_txs=100, strategy=strategy, now_ts=NOW)
        assert history_item(estimate).credits_each == expected
        assert dict(estimate.facts)["history endpoint"].startswith(strategy)


def test_projection_warns_about_the_fallback_cost(settings):
    estimate = estimate_ingest(settings, ["m1"], max_txs=1_000, now_ts=NOW)
    note = " ".join(estimate.notes)
    assert "falls back" in note
    assert "worst case" in note
    # 10 pages x 100 credits if only the Enhanced endpoint is available.
    assert "1,000 credits" in note


def test_no_fallback_note_when_already_on_the_dearest_endpoint(settings):
    estimate = estimate_ingest(settings, ["m1"], strategy="enhanced_tx", now_ts=NOW)
    assert not any("worst case" in note for note in estimate.notes)


def test_ingest_projection_respects_the_configured_cost_table(monkeypatch, tmp_path):
    """Costs are configuration, not constants: a plan with different rates re-prices."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HELIUS_CREDITS_ENHANCED_TX", "1")
    settings = load_settings(override=True)
    estimate = estimate_ingest(settings, ["m1"], max_txs=500, strategy="enhanced_tx", now_ts=NOW)
    tape = history_item(estimate)
    assert tape.credits_each == 1
    assert tape.credits == 5


def test_metadata_is_projected_as_free_first_with_a_das_fallback(settings):
    estimate = estimate_ingest(settings, ["m1", "m2", "m3"], now_ts=NOW)
    labels = {item.label: item for item in estimate.items}
    assert labels["token metadata"].provider == "dexscreener"
    assert labels["token metadata"].credits == 0
    fallback = labels["token metadata fallback"]
    assert fallback.provider == "helius"
    assert fallback.credits_each == 10  # DAS getAsset
    assert fallback.calls == 3


def test_metadata_uses_birdeye_when_it_is_enabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIRDEYE_API_KEY", "k")
    monkeypatch.setenv("ENABLE_BIRDEYE", "true")
    settings = load_settings(override=True)
    estimate = estimate_ingest(settings, ["m1"], now_ts=NOW)
    labels = {item.label: item for item in estimate.items}
    assert labels["token metadata"].provider == "birdeye"
    assert "token metadata fallback" not in labels


def test_a_time_bounded_run_needs_less_price_history(settings):
    wide = estimate_ingest(settings, ["m1"], now_ts=NOW)
    narrow = estimate_ingest(settings, ["m1"], since_ts=NOW - 2 * 86_400, now_ts=NOW)

    def price_calls(estimate):
        return next(i.calls for i in estimate.items if i.label == "SOL price history")

    assert price_calls(narrow) < price_calls(wide)


def test_expand_projection(settings):
    estimate = estimate_expand(settings, wallet_count=40, max_txs_per_wallet=200, now_ts=NOW)
    item = estimate.items[0]
    assert item.provider == "helius"
    assert item.calls == 40 * 2          # 200 txs = 2 pages of 100
    assert item.credits == 40 * 2 * 10   # cheap history endpoint
    assert by_provider(estimate)["helius"]["requests"] == 80

    dear = estimate_expand(
        settings, wallet_count=40, max_txs_per_wallet=200, strategy="enhanced_tx", now_ts=NOW
    )
    assert dear.items[0].credits == 40 * 2 * 100


def test_expand_defaults_to_the_low_fan_out_caps(settings):
    estimate = estimate_expand(settings, wallet_count=10, now_ts=NOW)
    facts = dict(estimate.facts)
    assert facts["transactions per wallet (cap)"] == "200"
    assert facts["wallets per seed token (cap)"] == "50"


def test_budget_check_passes_within_the_caps(settings, conn):
    tracker = BudgetTracker(conn, settings.provider_budgets(), costs=settings.credit_costs())
    estimate = estimate_ingest(settings, ["m1"], max_txs=200, now_ts=NOW)
    checks = budget_check(estimate, tracker, settings, conn)
    assert checks and all(check["fits"] for check in checks)


def test_budget_check_flags_a_run_that_would_breach_a_cap(settings, conn):
    budgets = {"helius": ProviderBudget("helius", max_credits_per_run=1_000)}
    tracker = BudgetTracker(conn, budgets, costs=settings.credit_costs())
    estimate = estimate_ingest(settings, ["m1", "m2"], max_txs=5_000, now_ts=NOW)
    checks = budget_check(estimate, tracker, settings, conn)

    over = [c for c in checks if not c["fits"]]
    assert over
    assert over[0]["provider"] == "helius"
    assert over[0]["limit_name"] == "credits/run"


def test_budget_check_counts_what_was_already_spent_today(settings, conn):
    conn.execute(
        "INSERT INTO api_usage(provider, day, requests, credits) VALUES ('helius', ?, 0, 990000)",
        (utc_day(),),
    )
    conn.commit()
    tracker = BudgetTracker(conn, settings.provider_budgets(), costs=settings.credit_costs())
    # 3 tokens x 50 pages x 100 credits = 15,000 on the Enhanced endpoint,
    # which no longer fits in the 1,000,000 monthly free tier once 990,000 is
    # already spent.
    estimate = estimate_ingest(
        settings, ["m1", "m2", "m3"], max_txs=5_000, strategy="enhanced_tx", now_ts=NOW
    )
    checks = budget_check(estimate, tracker, settings, conn)

    monthly = next(c for c in checks if c["limit_name"] == "credits/month")
    assert monthly["used"] == 990_000
    assert monthly["projected"] == 15_030
    assert monthly["fits"] is False

    # The same run on the default endpoint fits comfortably.
    cheap = estimate_ingest(settings, ["m1", "m2", "m3"], max_txs=5_000, now_ts=NOW)
    cheap_monthly = next(
        c for c in budget_check(cheap, tracker, settings, conn)
        if c["limit_name"] == "credits/month"
    )
    assert cheap_monthly["fits"] is True


def test_render_reports_the_numbers_and_the_verdict(settings, conn):
    budgets = {"helius": ProviderBudget("helius", max_credits_per_run=1_000)}
    tracker = BudgetTracker(conn, budgets, costs=settings.credit_costs())
    estimate = estimate_ingest(
        settings, ["m1", "m2"], max_txs=5_000, strategy="enhanced_tx", now_ts=NOW
    )
    text = "\n".join(render(estimate, budget_check(estimate, tracker, settings, conn)))

    assert "dry run — no API calls were made" in text
    assert "PROJECTED CONSUMPTION" in text
    assert "BUDGET CHECK" in text
    assert "This run would breach" in text
    assert "Enhanced Transactions API" in text
    assert "upper bound" in text.lower()


def test_render_names_the_history_endpoint(settings, conn):
    tracker = BudgetTracker(conn, settings.provider_budgets(), costs=settings.credit_costs())
    estimate = estimate_ingest(settings, ["m1"], max_txs=1_000, now_ts=NOW)
    text = "\n".join(render(estimate, budget_check(estimate, tracker, settings, conn)))
    assert "history endpoint" in text
    assert "Parsed Events API (beta)" in text


def test_estimate_serialises_for_json_output(settings):
    payload = estimate_ingest(settings, ["m1"], now_ts=NOW).as_dict()
    assert payload["dry_run"] is True
    assert payload["command"] == "ingest"
    assert payload["totals"]["helius"]["credits"] > 0
    assert all({"label", "provider", "calls", "credits"} <= set(i) for i in payload["items"])
