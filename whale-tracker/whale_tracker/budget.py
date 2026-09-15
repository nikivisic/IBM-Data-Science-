"""API budget tracking and hard caps.

Every external call is metered before it leaves the process. Two things are
counted per provider:

* **requests** — one per network call (cache hits are free and counted
  separately);
* **credits** — the provider's own billing unit. Helius charges different
  amounts per method, so the cost of a call is looked up in `CreditCosts`.

Caps exist per run and per (UTC) day, and are enforced *before* the call is
made. Hitting one raises `BudgetExceeded`, which the CLI turns into a clean
abort naming the provider and the limit. There is deliberately no "soft" mode
that keeps going with degraded data: a half-finished ingest that silently
stopped calling an API is worse than one that stopped and said so.

Daily totals are persisted in `api_usage`, so month-to-date consumption
survives restarts and `whale-tracker status` can show it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .logging_setup import get_logger

log = get_logger(__name__)

#: Helius DAS (Digital Asset Standard) methods. Priced as heavy calls.
DAS_METHODS = frozenset(
    {
        "getasset",
        "getassetproof",
        "getassetbatch",
        "getassetproofbatch",
        "getassetsbyowner",
        "getassetsbyauthority",
        "getassetsbycreator",
        "getassetsbygroup",
        "searchassets",
        "gettokenaccounts",
        "getnfteditions",
        "getsignaturesforasset",
    }
)

#: Other methods Helius bills above the standard rate.
HEAVY_RPC_METHODS = frozenset({"getprogramaccounts"})


class BudgetExceeded(RuntimeError):
    """A provider cap would be breached by the call that was about to be made."""

    def __init__(self, provider: str, limit_name: str, limit: float, used: float, needed: float):
        self.provider = provider
        self.limit_name = limit_name
        self.limit = limit
        self.used = used
        self.needed = needed
        super().__init__(
            f"{provider}: {limit_name} budget exhausted "
            f"({used:,.0f} used of {limit:,.0f}; this call needs {needed:,.0f}). "
            f"Raise the cap in .env or with the matching CLI flag, or wait "
            f"{'until tomorrow (UTC)' if 'day' in limit_name else 'for the next run'}."
        )


@dataclass(frozen=True)
class CreditCosts:
    """What one call costs, in the provider's own billing unit.

    Defaults follow Helius's published pricing: ordinary RPC is 1 credit,
    `getProgramAccounts` and the DAS methods are 10, and the Enhanced
    Transactions API — which is what ingestion actually uses — is billed well
    above a plain RPC call. All three are configurable because pricing is the
    provider's to change, and plans differ; see `HELIUS_CREDITS_*` in .env.
    """

    rpc: int = 1
    heavy_rpc: int = 10
    das: int = 10
    enhanced_tx: int = 100
    default: int = 1

    def for_call(self, *, kind: str = "rpc", method: str = "") -> int:
        """Credits for one call. `kind` is the endpoint family, `method` the RPC method."""
        if kind == "enhanced_tx":
            return self.enhanced_tx
        if kind in ("free", "none"):
            return 0
        name = (method or "").lower()
        if name in DAS_METHODS:
            return self.das
        if name in HEAVY_RPC_METHODS:
            return self.heavy_rpc
        if kind == "das":
            return self.das
        if kind == "heavy_rpc":
            return self.heavy_rpc
        if kind == "rpc":
            return self.rpc
        return self.default


@dataclass
class ProviderBudget:
    """Caps for one provider. `None` or a non-positive value means unlimited."""

    provider: str
    max_requests_per_run: Optional[int] = None
    max_requests_per_day: Optional[int] = None
    max_credits_per_run: Optional[int] = None
    max_credits_per_day: Optional[int] = None
    #: Informational only — used by `status` and the dry-run estimate to show
    #: how a run sizes against a monthly plan (e.g. the 1M Helius free tier).
    monthly_credit_budget: Optional[int] = None

    def limits(self) -> dict[str, Optional[int]]:
        return {
            "requests/run": self.max_requests_per_run,
            "requests/day": self.max_requests_per_day,
            "credits/run": self.max_credits_per_run,
            "credits/day": self.max_credits_per_day,
        }


@dataclass
class Usage:
    requests: int = 0
    credits: int = 0
    cache_hits: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"requests": self.requests, "credits": self.credits, "cache_hits": self.cache_hits}


def utc_day(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


class BudgetTracker:
    """Meters provider usage and refuses calls that would breach a cap.

    Thread-safe, because the HTTP clients may eventually be used concurrently.
    Writes are flushed to SQLite as they happen so a killed process still
    leaves an accurate daily total behind.
    """

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        budgets: Optional[Mapping[str, ProviderBudget]] = None,
        *,
        enforce: bool = True,
        costs: Optional[CreditCosts] = None,
        log_every: int = 25,
    ):
        self.conn = conn
        self.budgets: dict[str, ProviderBudget] = dict(budgets or {})
        self.enforce = enforce
        self.costs = costs or CreditCosts()
        self.log_every = max(1, log_every)
        self.run_usage: dict[str, Usage] = {}
        self._day = utc_day()
        self._day_seed: dict[str, Usage] = {}
        self._lock = threading.RLock()

    # -- persistence ------------------------------------------------------
    def _load_day(self, provider: str) -> Usage:
        """Usage already recorded for `provider` today, before this run started."""
        if provider in self._day_seed:
            return self._day_seed[provider]
        usage = Usage()
        if self.conn is not None:
            row = self.conn.execute(
                "SELECT requests, credits, cache_hits FROM api_usage WHERE provider = ? AND day = ?",
                (provider, self._day),
            ).fetchone()
            if row is not None:
                usage = Usage(
                    requests=int(row["requests"] or 0),
                    credits=int(row["credits"] or 0),
                    cache_hits=int(row["cache_hits"] or 0),
                )
        # Subtract what this run has already contributed, so the seed stays a
        # pure "before this run" figure even after we start writing.
        run = self.run_usage.get(provider)
        if run is not None:
            usage = Usage(
                requests=max(0, usage.requests - run.requests),
                credits=max(0, usage.credits - run.credits),
                cache_hits=max(0, usage.cache_hits - run.cache_hits),
            )
        self._day_seed[provider] = usage
        return usage

    def _persist(self, provider: str, requests: int, credits: int, cache_hits: int) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT INTO api_usage(provider, day, requests, credits, cache_hits) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(provider, day) DO UPDATE SET "
            "requests = requests + excluded.requests, "
            "credits = credits + excluded.credits, "
            "cache_hits = cache_hits + excluded.cache_hits",
            (provider, self._day, requests, credits, cache_hits),
        )
        self.conn.commit()

    # -- accounting -------------------------------------------------------
    def budget_for(self, provider: str) -> ProviderBudget:
        return self.budgets.get(provider) or ProviderBudget(provider)

    def day_usage(self, provider: str) -> Usage:
        """Total recorded today, including this run."""
        seed = self._load_day(provider)
        run = self.run_usage.get(provider, Usage())
        return Usage(
            requests=seed.requests + run.requests,
            credits=seed.credits + run.credits,
            cache_hits=seed.cache_hits + run.cache_hits,
        )

    def remaining(self, provider: str) -> dict[str, Optional[int]]:
        """How much headroom is left under each cap (None = uncapped)."""
        budget = self.budget_for(provider)
        run = self.run_usage.get(provider, Usage())
        day = self.day_usage(provider)
        out: dict[str, Optional[int]] = {}
        for name, limit, used in (
            ("requests/run", budget.max_requests_per_run, run.requests),
            ("requests/day", budget.max_requests_per_day, day.requests),
            ("credits/run", budget.max_credits_per_run, run.credits),
            ("credits/day", budget.max_credits_per_day, day.credits),
        ):
            out[name] = None if not limit or limit <= 0 else max(0, int(limit) - int(used))
        return out

    def check(self, provider: str, *, requests: int = 1, credits: int = 0) -> None:
        """Raise `BudgetExceeded` if this much usage would breach a cap."""
        if not self.enforce:
            return
        budget = self.budget_for(provider)
        run = self.run_usage.get(provider, Usage())
        day = self.day_usage(provider)
        checks = (
            ("requests/run", budget.max_requests_per_run, run.requests, requests),
            ("requests/day", budget.max_requests_per_day, day.requests, requests),
            ("credits/run", budget.max_credits_per_run, run.credits, credits),
            ("credits/day", budget.max_credits_per_day, day.credits, credits),
        )
        for name, limit, used, needed in checks:
            if limit and limit > 0 and needed and used + needed > limit:
                raise BudgetExceeded(provider, name, float(limit), float(used), float(needed))

    def consume(
        self,
        provider: str,
        *,
        kind: str = "rpc",
        method: str = "",
        requests: int = 1,
        credits: Optional[int] = None,
    ) -> int:
        """Check the caps, then record the usage. Returns the credits charged.

        Called immediately before the network request, so a refusal costs
        nothing and a recorded call really was made.
        """
        cost = self.costs.for_call(kind=kind, method=method) if credits is None else int(credits)
        with self._lock:
            self._roll_day_if_needed()
            self.check(provider, requests=requests, credits=cost)
            usage = self.run_usage.setdefault(provider, Usage())
            usage.requests += requests
            usage.credits += cost
            self._persist(provider, requests, cost, 0)
            if usage.requests % self.log_every == 0:
                day = self.day_usage(provider)
                log.info(
                    "budget.usage",
                    extra={
                        "ctx": {
                            "provider": provider,
                            "run_requests": usage.requests,
                            "run_credits": usage.credits,
                            "day_requests": day.requests,
                            "day_credits": day.credits,
                        }
                    },
                )
        return cost

    def record_cache_hit(self, provider: str) -> None:
        with self._lock:
            self.run_usage.setdefault(provider, Usage()).cache_hits += 1
            self._persist(provider, 0, 0, 1)

    def _roll_day_if_needed(self) -> None:
        today = utc_day()
        if today != self._day:
            log.info("budget.day_rollover", extra={"ctx": {"from": self._day, "to": today}})
            self._day = today
            self._day_seed.clear()

    # -- reporting --------------------------------------------------------
    def run_summary(self) -> dict[str, dict[str, int]]:
        return {provider: usage.as_dict() for provider, usage in sorted(self.run_usage.items())}

    def log_run_summary(self) -> None:
        for provider, usage in sorted(self.run_usage.items()):
            if not (usage.requests or usage.cache_hits):
                continue
            log.info(
                "budget.run_total",
                extra={
                    "ctx": {
                        "provider": provider,
                        "requests": usage.requests,
                        "credits": usage.credits,
                        "cache_hits": usage.cache_hits,
                    }
                },
            )


def month_to_date(conn: sqlite3.Connection, *, month: Optional[str] = None) -> dict[str, Usage]:
    """Usage per provider for the given `YYYY-MM` (default: the current UTC month)."""
    month = month or datetime.now(tz=timezone.utc).strftime("%Y-%m")
    rows = conn.execute(
        "SELECT provider, SUM(requests) AS requests, SUM(credits) AS credits, "
        "SUM(cache_hits) AS cache_hits FROM api_usage WHERE day LIKE ? GROUP BY provider",
        (f"{month}-%",),
    ).fetchall()
    return {
        row["provider"]: Usage(
            requests=int(row["requests"] or 0),
            credits=int(row["credits"] or 0),
            cache_hits=int(row["cache_hits"] or 0),
        )
        for row in rows
    }


def usage_history(conn: sqlite3.Connection, *, days: int = 14) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT day, provider, requests, credits, cache_hits FROM api_usage "
        "ORDER BY day DESC, provider LIMIT ?",
        (days * 8,),
    ).fetchall()
    return [dict(row) for row in rows]
