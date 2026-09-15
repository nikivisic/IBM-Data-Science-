"""Dry-run cost projection.

`--dry-run` answers one question before a single call is made: *what will this
cost me?* — in provider requests and, for Helius, in credits, sized against the
caps in `.env` and against a monthly plan (the free tier is 1,000,000 credits).

Everything here is an **upper bound**. The projection assumes each token and
each wallet runs into its cap; in practice a token whose history is shorter
than the cap stops early, so the real figure is at most this and usually less.
Bounding the wrong way round would defeat the purpose.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .budget import BudgetTracker, month_to_date
from .clients.helius_history import STRATEGY_ENHANCED_TX, planned_strategies
from .config import Settings

#: Helius returns at most 100 parsed transactions per page.
HELIUS_PAGE_SIZE = 100
#: GeckoTerminal returns at most 100 hourly candles per OHLCV call.
OHLCV_PAGE_HOURS = 100
#: Span assumed for SOL price history when the run is not time-bounded.
DEFAULT_PRICE_SPAN_DAYS = 90


@dataclass
class LineItem:
    label: str
    provider: str
    calls: int
    credits_each: int = 0
    note: str = ""

    @property
    def credits(self) -> int:
        return self.calls * self.credits_each

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "provider": self.provider,
            "calls": self.calls,
            "credits_each": self.credits_each,
            "credits": self.credits,
            "note": self.note,
        }


@dataclass
class Estimate:
    command: str
    items: list[LineItem] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Transaction cap this projection was built from, used for page maths.
    _cap: int = 0

    def add(self, item: LineItem) -> None:
        if item.calls > 0:
            self.items.append(item)

    def totals(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for item in self.items:
            entry = out.setdefault(item.provider, {"requests": 0, "credits": 0})
            entry["requests"] += item.calls
            entry["credits"] += item.credits
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "dry_run": True,
            "facts": dict(self.facts),
            "items": [item.as_dict() for item in self.items],
            "totals": self.totals(),
            "notes": self.notes,
        }


def _pages(total: int, page_size: int) -> int:
    return max(1, math.ceil(max(total, 1) / page_size))


def _history_plan(settings: Settings, strategy: Optional[str] = None) -> tuple[str, tuple[str, ...]]:
    """(strategy this run would use first, the rest of the fallback chain)."""
    plan = planned_strategies(settings)
    if strategy:
        plan = (strategy, *(name for name in plan if name != strategy))
    return plan[0], plan[1:]


def _history_lines(
    estimate: "Estimate",
    settings: Settings,
    *,
    label: str,
    units: int,
    strategy: Optional[str] = None,
) -> int:
    """Add the history line item and the fallback note. Returns pages per unit.

    `units` is tokens (for ingest) or wallets (for expand). The projection uses
    the *preferred* strategy, and a note carries the worst case, because which
    endpoint answers is only known once the run makes its first call.
    """
    costs = settings.credit_costs()
    primary, fallbacks = _history_plan(settings, strategy)
    page_size = settings.helius_history_page_size
    pages = _pages(_history_cap(estimate), page_size)

    estimate.add(
        LineItem(
            label,
            "helius",
            units * pages,
            costs.history_cost(primary),
            HISTORY_LABELS.get(primary, primary),
        )
    )
    if fallbacks:
        worst = max(costs.history_cost(name) for name in fallbacks)
        if worst > costs.history_cost(primary):
            worst_total = units * pages * worst
            estimate.notes.append(
                f"If {primary} is not enabled on this plan the run falls back to "
                f"{'/'.join(fallbacks)} — worst case {worst_total:,} credits "
                f"({worst:,} per page via {STRATEGY_ENHANCED_TX})."
            )
    return pages


HISTORY_LABELS = {
    "parsed_events": "Parsed Events API (beta)",
    "bulk_history": "bulk address history (RPC)",
    "enhanced_tx": "Enhanced Transactions API",
}


def _history_cap(estimate: "Estimate") -> int:
    return int(getattr(estimate, "_cap", HELIUS_PAGE_SIZE))


def _price_history_calls(settings: Settings, since_ts: Optional[int], now_ts: int) -> int:
    """OHLCV calls needed to cover the SOL price span, plus one pool lookup."""
    span_seconds = (now_ts - since_ts) if since_ts else DEFAULT_PRICE_SPAN_DAYS * 86_400
    hours = max(1, int(span_seconds // 3_600))
    return _pages(hours, OHLCV_PAGE_HOURS) + 1


def estimate_ingest(
    settings: Settings,
    mints: Sequence[str],
    *,
    max_txs: Optional[int] = None,
    provider: str = "helius",
    since_ts: Optional[int] = None,
    strategy: Optional[str] = None,
    now_ts: int,
) -> Estimate:
    """Project the cost of `ingest` for the given mints."""
    cap = max_txs if max_txs is not None else settings.max_txs_per_token
    page_size = settings.helius_history_page_size
    pages = _pages(cap, page_size)
    costs = settings.credit_costs()
    estimate = Estimate(command="ingest")
    estimate._cap = cap
    primary, _ = _history_plan(settings, strategy)

    estimate.facts = [
        ("tokens", f"{len(mints)}"),
        ("transactions per token (cap)", f"{cap:,}"),
        ("pages per token", f"{pages:,} × {page_size} per page"),
        ("history endpoint", f"{primary} ({costs.history_cost(primary):,} credits/page)"),
        ("provider", provider),
        ("price sources", ", ".join(settings.active_price_sources()) or "(none)"),
    ]

    if provider in ("helius", "both"):
        _history_lines(
            estimate, settings, label="transaction history pages",
            units=len(mints), strategy=strategy,
        )
    if provider in ("birdeye", "both"):
        # Birdeye's tape pages 50 items at a time.
        estimate.add(
            LineItem("trade tape pages", "birdeye", len(mints) * _pages(cap, 50), 0))

    # Token metadata: free first, Helius DAS only as a fallback.
    if settings.enable_birdeye and settings.birdeye_api_key:
        estimate.add(LineItem("token metadata", "birdeye", len(mints), 0))
    elif "dexscreener" in settings.active_price_sources():
        estimate.add(LineItem("token metadata", "dexscreener", len(mints), 0))
        estimate.add(
            LineItem(
                "token metadata fallback",
                "helius",
                len(mints),
                costs.das,
                "DAS getAsset, only if DexScreener has no listing",
            )
        )
    else:
        estimate.add(
            LineItem("token metadata", "helius", len(mints), costs.das, "DAS getAsset")
        )

    # Quote-leg pricing: SOL history, shared across every token in the run.
    sources = settings.active_price_sources()
    if "geckoterminal" in sources:
        estimate.add(
            LineItem(
                "SOL price history",
                "geckoterminal",
                _price_history_calls(settings, since_ts, now_ts),
                0,
                "hourly OHLCV, cached in price_points",
            )
        )
    elif "birdeye" in sources:
        estimate.add(LineItem("SOL price history", "birdeye", 30, 0))

    estimate.notes.append(
        "Upper bound: a token with fewer transactions than the cap stops early."
    )
    estimate.notes.append(
        "Cached responses cost nothing, so a repeated or resumed run is cheaper than this."
    )
    return estimate


def estimate_expand(
    settings: Settings,
    *,
    wallet_count: int,
    max_txs_per_wallet: Optional[int] = None,
    per_token_limit: Optional[int] = None,
    strategy: Optional[str] = None,
    now_ts: int,
) -> Estimate:
    """Project the cost of `expand` for a known number of candidate wallets."""
    cap = (
        max_txs_per_wallet
        if max_txs_per_wallet is not None
        else settings.max_txs_per_wallet
    )
    page_size = settings.helius_history_page_size
    pages = _pages(cap, page_size)
    costs = settings.credit_costs()
    estimate = Estimate(command="expand")
    estimate._cap = cap
    primary, _ = _history_plan(settings, strategy)

    estimate.facts = [
        ("candidate wallets", f"{wallet_count:,}"),
        ("wallets per seed token (cap)", f"{per_token_limit or settings.max_wallets_per_token:,}"),
        ("transactions per wallet (cap)", f"{cap:,}"),
        ("pages per wallet", f"{pages:,} × {page_size} per page"),
        ("history endpoint", f"{primary} ({costs.history_cost(primary):,} credits/page)"),
    ]
    _history_lines(
        estimate, settings, label="wallet history pages",
        units=wallet_count, strategy=strategy,
    )
    estimate.notes.append(
        "Upper bound: a wallet with a shorter history than the cap stops early."
    )
    estimate.notes.append(
        "Newly discovered mints are priced from cached SOL history, so no extra price calls."
    )
    return estimate


def budget_check(
    estimate: Estimate,
    tracker: BudgetTracker,
    settings: Settings,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Compare a projection against each cap. Returns one row per check."""
    rows: list[dict[str, Any]] = []
    totals = estimate.totals()
    mtd = month_to_date(conn) if conn is not None else {}

    for provider, projected in sorted(totals.items()):
        budget = tracker.budget_for(provider)
        day = tracker.day_usage(provider)
        run = tracker.run_usage.get(provider)
        run_requests = run.requests if run else 0
        run_credits = run.credits if run else 0

        for name, limit, used, needed in (
            ("requests/run", budget.max_requests_per_run, run_requests, projected["requests"]),
            ("requests/day", budget.max_requests_per_day, day.requests, projected["requests"]),
            ("credits/run", budget.max_credits_per_run, run_credits, projected["credits"]),
            ("credits/day", budget.max_credits_per_day, day.credits, projected["credits"]),
        ):
            if not needed:
                continue
            capped = bool(limit and limit > 0)
            rows.append(
                {
                    "provider": provider,
                    "limit_name": name,
                    "limit": int(limit) if capped else None,
                    "used": int(used),
                    "projected": int(needed),
                    "fits": (not capped) or (used + needed <= int(limit)),
                }
            )

        monthly = budget.monthly_credit_budget
        if monthly and projected["credits"]:
            used_month = mtd.get(provider).credits if provider in mtd else 0
            rows.append(
                {
                    "provider": provider,
                    "limit_name": "credits/month",
                    "limit": int(monthly),
                    "used": int(used_month),
                    "projected": int(projected["credits"]),
                    "fits": used_month + projected["credits"] <= int(monthly),
                }
            )
    return rows


def render(
    estimate: Estimate,
    checks: Sequence[dict[str, Any]],
) -> list[str]:
    """Human-readable dry-run report."""
    lines: list[str] = ["dry run — no API calls were made", ""]

    if estimate.facts:
        lines.append("WHAT IT WOULD DO")
        width = max(len(label) for label, _ in estimate.facts)
        for label, value in estimate.facts:
            lines.append(f"  {label.ljust(width)}   {value}")
        lines.append("")

    lines.append("PROJECTED CONSUMPTION (upper bound)")
    if not estimate.items:
        lines.append("  (nothing to do)")
    else:
        header = ("WHAT", "PROVIDER", "CALLS", "CREDITS EACH", "CREDITS")
        rows = [
            (
                item.label,
                item.provider,
                f"{item.calls:,}",
                f"{item.credits_each:,}" if item.credits_each else "0",
                f"{item.credits:,}" if item.credits else "0",
            )
            for item in estimate.items
        ]
        widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(5)]
        lines.append("  " + "  ".join(header[i].ljust(widths[i]) for i in range(5)).rstrip())
        lines.append("  " + "  ".join("-" * widths[i] for i in range(5)))
        for row in rows:
            lines.append("  " + "  ".join(str(row[i]).ljust(widths[i]) for i in range(5)).rstrip())
        for item in estimate.items:
            if item.note:
                lines.append(f"    · {item.label}: {item.note}")
        lines.append("")
        lines.append("  totals")
        for provider, totals in sorted(estimate.totals().items()):
            lines.append(
                f"    {provider:<14} {totals['requests']:>8,} requests   "
                f"{totals['credits']:>10,} credits"
            )

    if checks:
        lines.append("")
        lines.append("BUDGET CHECK")
        for row in checks:
            limit = row["limit"]
            after = row["used"] + row["projected"]
            if limit is None:
                status, detail = "uncapped", f"{after:,} after this run"
            else:
                status = "ok" if row["fits"] else "OVER"
                share = (after / limit * 100) if limit else 0.0
                detail = f"{after:,} of {limit:,} after this run ({share:.1f}%)"
            lines.append(
                f"  {row['provider']:<14} {row['limit_name']:<14} {status:<8} {detail}"
            )

    over = [row for row in checks if not row["fits"]]
    if over:
        lines.append("")
        lines.append("This run would breach:")
        for row in over:
            lines.append(
                f"  - {row['provider']} {row['limit_name']}: "
                f"{row['used']:,} used + {row['projected']:,} projected > {row['limit']:,}"
            )
        lines.append("")
        lines.append(
            "Reduce the work (--max-txs, --limit, fewer tokens) or raise the cap "
            "in .env before running for real."
        )

    if estimate.notes:
        lines.append("")
        for note in estimate.notes:
            lines.append(f"note: {note}")
    return lines
