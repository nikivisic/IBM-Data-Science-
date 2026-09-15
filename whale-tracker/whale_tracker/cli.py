"""Command line interface.

    whale-tracker init-db
    whale-tracker ingest --token <mint> [--token <mint> ...]
    whale-tracker expand --limit 50
    whale-tracker analyse
    whale-tracker rank --limit 25
    whale-tracker wallet <address> --trades
    whale-tracker backtest --from-rank 10 --start 2025-06-01 --end 2025-09-01

Data goes to stdout, logs to stderr, so `... --json | jq` works.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import __version__
from .backtest import BacktestConfig, run_backtest
from .budget import BudgetExceeded, BudgetTracker, month_to_date
from .clients.base import ApiError
from .clients.birdeye import BirdeyeClient
from .clients.helius import HeliusClient
from .clients.helius_history import planned_strategies
from .clients.prices import DexScreenerPriceSource, PriceChain, build_price_chain
from .config import ConfigError, Settings, WSOL_MINT, load_settings
from .db import connect, init_db, table_counts
from .estimate import budget_check, estimate_expand, estimate_ingest, render
from .ingest import PriceOracle, candidate_wallets, expand_wallet, ingest_token
from .logging_setup import configure_logging, get_logger
from .patterns import PatternConfig, analyse_patterns
from .pnl import rebuild_positions
from .scoring import ScoringConfig, ranked_wallets, score_all

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def parse_time(value: str) -> int:
    """Accept `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, a unix timestamp, or `30d` ago."""
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("empty timestamp")
    if value.endswith("d") and value[:-1].isdigit():
        return int(time.time()) - int(value[:-1]) * 86_400
    if value.isdigit() and len(value) >= 9:
        return int(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognised time: {value!r} (try 2025-06-01, 30d, or a unix ts)")


def parse_range(value: str, *, scale: float = 1.0) -> tuple[float, float]:
    """`15-30` -> (15.0, 30.0); a bare `20` -> (20.0, 20.0)."""
    parts = [p for p in value.replace(",", "-").split("-") if p.strip()]
    try:
        numbers = [float(p) * scale for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number or range like 15-30, got {value!r}") from exc
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    if len(numbers) == 2:
        return min(numbers), max(numbers)
    raise argparse.ArgumentTypeError(f"expected a range like 15-30, got {value!r}")


def ts_str(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def dur_str(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5_400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172_800:
        return f"{seconds / 3_600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


def usd(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def num(value: Optional[float], places: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:,.{places}f}"


Column = tuple[str, str, Callable[[Any], str]]


def print_table(rows: Sequence[Any], columns: Sequence[Column], *, stream=None) -> None:
    """Render rows as a fixed-width table.

    `stream` is resolved at call time, not at import time, so redirection
    (and test capture) works.
    """
    stream = stream or sys.stdout
    if not rows:
        print("(no rows)", file=stream)
        return
    table = [[header for header, _, _ in columns]]
    for row in rows:
        mapping = dict(row) if isinstance(row, sqlite3.Row) else row
        table.append([formatter(mapping.get(key)) for _, key, formatter in columns])
    widths = [max(len(r[i]) for r in table) for i in range(len(columns))]
    for index, line in enumerate(table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip(), file=stream)
        if index == 0:
            print("  ".join("-" * widths[i] for i in range(len(columns))), file=stream)


def print_json(payload: Any, *, stream=None) -> None:
    stream = stream or sys.stdout
    json.dump(payload, stream, indent=2, default=str)
    stream.write("\n")


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def write_csv(rows: Sequence[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not rows:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# client wiring
# ---------------------------------------------------------------------------


@dataclass
class Runtime:
    """Everything a network-touching command needs, wired to one budget."""

    budget: BudgetTracker
    chain: PriceChain
    helius: Optional[HeliusClient] = None
    birdeye: Optional[BirdeyeClient] = None
    free_metadata: Optional[DexScreenerPriceSource] = None

    def oracle(self, conn: sqlite3.Connection, static_sol_price: Optional[float] = None) -> PriceOracle:
        return PriceOracle(conn, self.chain, static_sol_price=static_sol_price)

    def report(self) -> None:
        """Log what the run consumed, per provider and per price source."""
        self.budget.log_run_summary()
        for name, stats in self.chain.stats().items():
            if stats["served"] or stats["failures"]:
                log.info("price.source_summary", extra={"ctx": {"source": name, **stats}})


def make_budget(
    settings: Settings, conn: sqlite3.Connection, args: argparse.Namespace
) -> BudgetTracker:
    """Budget tracker with per-run overrides from the command line applied."""
    budgets = settings.provider_budgets()
    override_requests = getattr(args, "max_requests", None)
    override_credits = getattr(args, "max_credits", None)
    for budget in budgets.values():
        if override_requests:
            budget.max_requests_per_run = int(override_requests)
        if override_credits and budget.provider == "helius":
            budget.max_credits_per_run = int(override_credits)
    return BudgetTracker(
        conn,
        budgets,
        enforce=settings.budget_enforce,
        costs=settings.credit_costs(),
    )


def build_runtime(
    settings: Settings,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    provider: str = "helius",
    *,
    need_helius: bool = True,
) -> Runtime:
    """Construct clients, the price chain and the shared budget tracker."""
    budget = make_budget(settings, conn, args)
    chain = build_price_chain(settings, conn, budget)

    helius = None
    if need_helius and provider in ("helius", "both"):
        helius = HeliusClient(settings, conn, budget)

    birdeye = None
    if provider in ("birdeye", "both"):
        birdeye = BirdeyeClient(settings, conn, budget)
    elif settings.enable_birdeye and settings.birdeye_api_key:
        try:
            birdeye = BirdeyeClient(settings, conn, budget)
        except ConfigError:
            birdeye = None

    free_metadata = None
    if "dexscreener" in settings.active_price_sources():
        free_metadata = DexScreenerPriceSource(settings, conn, budget)

    return Runtime(
        budget=budget, chain=chain, helius=helius, birdeye=birdeye, free_metadata=free_metadata
    )


def apply_source_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    """Let --price-sources / --enable-birdeye override the .env configuration."""
    changes: dict[str, Any] = {}
    if getattr(args, "price_sources", None):
        changes["price_sources"] = tuple(
            part.strip().lower() for part in args.price_sources.split(",") if part.strip()
        )
    if getattr(args, "enable_birdeye", False):
        changes["enable_birdeye"] = True
    if getattr(args, "history_strategy", None):
        changes["helius_history_strategy"] = args.history_strategy
    if not changes:
        return settings
    return replace(settings, **changes)


def read_token_list(args: argparse.Namespace) -> list[str]:
    mints: list[str] = list(args.token or [])
    if args.tokens_file:
        path = Path(args.tokens_file)
        if not path.exists():
            raise SystemExit(f"tokens file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                mints.append(line)
    seen: set[str] = set()
    ordered = []
    for mint in mints:
        if mint not in seen:
            seen.add(mint)
            ordered.append(mint)
    return ordered


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    conn = init_db(settings.db_path)
    print(f"database ready at {settings.db_path}")
    print_table(
        [{"table": name, "rows": count} for name, count in table_counts(conn).items()],
        [("TABLE", "table", str), ("ROWS", "rows", str)],
    )
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    conn = init_db(settings.db_path)
    counts = table_counts(conn)
    window = conn.execute("SELECT MIN(ts) AS a, MAX(ts) AS b FROM trades").fetchone()
    tracker = BudgetTracker(conn, settings.provider_budgets(), enforce=settings.budget_enforce,
                            costs=settings.credit_costs())
    mtd = month_to_date(conn)
    month = datetime.now(tz=timezone.utc).strftime("%Y-%m")

    providers = sorted(set(mtd) | set(settings.provider_budgets()))
    usage_rows = []
    for provider in providers:
        budget = tracker.budget_for(provider)
        month_usage = mtd.get(provider)
        today = tracker.day_usage(provider)
        monthly_cap = budget.monthly_credit_budget
        month_credits = month_usage.credits if month_usage else 0
        usage_rows.append(
            {
                "provider": provider,
                "mtd_requests": month_usage.requests if month_usage else 0,
                "mtd_credits": month_credits,
                "mtd_cache_hits": month_usage.cache_hits if month_usage else 0,
                "today_requests": today.requests,
                "today_credits": today.credits,
                "monthly_budget": monthly_cap or 0,
                "monthly_pct": (month_credits / monthly_cap) if monthly_cap else None,
            }
        )

    payload = {
        "db_path": str(settings.db_path),
        "counts": counts,
        "trade_window": {"from": ts_str(window["a"]), "to": ts_str(window["b"])},
        "helius_key": bool(settings.helius_api_key),
        "birdeye_key": bool(settings.birdeye_api_key),
        "price_sources": list(settings.active_price_sources()),
        "history_strategy": settings.helius_history_strategy,
        "history_plan": list(planned_strategies(settings)),
        "budget_enforce": settings.budget_enforce,
        "month": month,
        "usage": usage_rows,
        "version": __version__,
    }
    if args.json:
        print_json(payload)
        return 0

    print(f"whale-tracker {__version__}   db={settings.db_path}")
    print(f"trade window: {payload['trade_window']['from']} .. {payload['trade_window']['to']}")
    print(f"keys: helius={'set' if settings.helius_api_key else 'MISSING'} "
          f"birdeye={'set' if settings.birdeye_api_key else 'MISSING'}")
    print(f"price sources: {', '.join(settings.active_price_sources()) or '(none)'}")
    costs = settings.credit_costs()
    plan = " -> ".join(
        f"{name}({costs.history_cost(name)})" for name in planned_strategies(settings)
    )
    print(f"history endpoints (credits/page): {plan}")
    print(f"budget caps: {'enforced' if settings.budget_enforce else 'DISABLED'}")
    print_table(
        [{"table": k, "rows": v} for k, v in counts.items()],
        [("TABLE", "table", str), ("ROWS", "rows", str)],
    )

    print(f"\nAPI usage — month to date ({month}, UTC)")
    print_table(
        usage_rows,
        [
            ("PROVIDER", "provider", str),
            ("REQUESTS", "mtd_requests", lambda v: f"{v:,}"),
            ("CREDITS", "mtd_credits", lambda v: f"{v:,}"),
            ("CACHE HITS", "mtd_cache_hits", lambda v: f"{v:,}"),
            ("TODAY REQ", "today_requests", lambda v: f"{v:,}"),
            ("TODAY CR", "today_credits", lambda v: f"{v:,}"),
            ("MONTHLY CAP", "monthly_budget", lambda v: f"{v:,}" if v else "-"),
            ("USED", "monthly_pct", lambda v: f"{v * 100:.1f}%" if v is not None else "-"),
        ],
    )
    return 0


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    mints = read_token_list(args)
    if not mints:
        raise SystemExit("no tokens given: use --token <mint> (repeatable) or --tokens-file")
    settings = apply_source_overrides(settings, args)
    conn = init_db(settings.db_path)

    if args.dry_run:
        estimate = estimate_ingest(
            settings,
            mints,
            max_txs=args.max_txs,
            provider=args.provider,
            since_ts=args.since,
            now_ts=int(time.time()),
        )
        tracker = make_budget(settings, conn, args)
        checks = budget_check(estimate, tracker, settings, conn)
        if args.json:
            print_json({**estimate.as_dict(), "budget_check": checks})
        else:
            for line in render(estimate, checks):
                print(line)
        return 0 if all(check["fits"] for check in checks) else 1

    runtime = build_runtime(settings, conn, args, args.provider)
    oracle = runtime.oracle(conn, args.sol_price)

    results = []
    failures = 0
    for mint in mints:
        try:
            result = ingest_token(
                conn,
                mint,
                settings=settings,
                helius=runtime.helius,
                birdeye=runtime.birdeye,
                oracle=oracle,
                free_metadata=runtime.free_metadata,
                max_txs=args.max_txs,
                since_ts=args.since,
                provider=args.provider,
            )
            results.append(result)
        except BudgetExceeded as exc:
            runtime.report()
            print(f"\nbudget stop: {exc}", file=sys.stderr)
            print(
                f"ingested {len(results)} of {len(mints)} token(s) before stopping.",
                file=sys.stderr,
            )
            return 4
        except ApiError as exc:
            failures += 1
            log.error("ingest.api_error", extra={"ctx": {"mint": mint, "error": str(exc)}})
            print(f"! {mint}: {exc}", file=sys.stderr)
    rows = [
        {
            "mint": r.mint,
            "txs": r.txs_seen,
            "trades": r.trades_written,
            "wallets": r.wallets,
            "unpriced": r.unpriced,
        }
        for r in results
    ]
    print_table(
        rows,
        [
            ("MINT", "mint", str),
            ("TXS", "txs", str),
            ("NEW TRADES", "trades", str),
            ("WALLETS", "wallets", str),
            ("UNPRICED", "unpriced", str),
        ],
    )
    runtime.report()
    print_history_endpoint(runtime)
    print_usage(runtime.budget)
    if failures and not results:
        print(f"\nall {failures} token(s) failed — nothing ingested", file=sys.stderr)
        return 3
    if failures:
        print(f"\n{failures} of {len(mints)} token(s) failed; see the log", file=sys.stderr)
    if args.analyse:
        return cmd_analyse(args, settings)
    print("\nnext: whale-tracker expand   (pull each candidate's wider history)")
    return 0


def print_history_endpoint(runtime: "Runtime") -> None:
    """Say which history endpoint actually served the run, and what it cost."""
    if runtime.helius is None:
        return
    history = runtime.helius.history
    if history.resolved is None:
        return
    credits = runtime.helius.settings.credit_costs().history_cost(history.resolved)
    print(f"\nhistory endpoint: {history.resolved} ({credits:,} credits/page)")
    for name, reason in history.unavailable.items():
        print(f"  skipped {name}: {reason}")


def print_usage(tracker: BudgetTracker) -> None:
    """What this run actually consumed, per provider."""
    summary = tracker.run_summary()
    rows = [
        {"provider": provider, **usage}
        for provider, usage in summary.items()
        if usage["requests"] or usage["cache_hits"]
    ]
    if not rows:
        return
    print("\nAPI usage this run")
    print_table(
        rows,
        [
            ("PROVIDER", "provider", str),
            ("REQUESTS", "requests", lambda v: f"{v:,}"),
            ("CREDITS", "credits", lambda v: f"{v:,}"),
            ("CACHE HITS", "cache_hits", lambda v: f"{v:,}"),
        ],
    )


def cmd_expand(args: argparse.Namespace, settings: Settings) -> int:
    settings = apply_source_overrides(settings, args)
    conn = init_db(settings.db_path)
    per_token = (
        args.max_wallets_per_token
        if args.max_wallets_per_token is not None
        else settings.max_wallets_per_token
    )
    max_txs = (
        args.max_txs_per_wallet
        if args.max_txs_per_wallet is not None
        else settings.max_txs_per_wallet
    )

    if args.wallet:
        wallets = list(args.wallet)
    else:
        wallets = candidate_wallets(
            conn,
            min_tokens=args.min_tokens,
            min_trades=args.min_trades,
            min_volume_usd=args.min_volume,
            limit=args.limit,
            per_token_limit=per_token,
        )
    if not wallets:
        print("no candidate wallets matched; ingest more tokens or lower --min-tokens")
        return 0

    if args.dry_run:
        estimate = estimate_expand(
            settings,
            wallet_count=len(wallets),
            max_txs_per_wallet=max_txs,
            per_token_limit=per_token,
            now_ts=int(time.time()),
        )
        tracker = make_budget(settings, conn, args)
        checks = budget_check(estimate, tracker, settings, conn)
        if args.json:
            print_json({**estimate.as_dict(), "budget_check": checks})
        else:
            for line in render(estimate, checks):
                print(line)
        return 0 if all(check["fits"] for check in checks) else 1

    runtime = build_runtime(settings, conn, args, "helius")
    if runtime.helius is None:
        raise SystemExit("expand needs a Helius key (wallet history comes from Helius)")
    oracle = runtime.oracle(conn, args.sol_price)

    total_new = 0
    done = 0
    for index, wallet in enumerate(wallets, start=1):
        try:
            result = expand_wallet(
                conn,
                wallet,
                settings=settings,
                helius=runtime.helius,
                oracle=oracle,
                max_txs=max_txs,
                since_ts=args.since,
            )
            total_new += result.trades_written
            done += 1
            print(f"[{index}/{len(wallets)}] {wallet}  +{result.trades_written} trades "
                  f"({result.txs_seen} txs)")
        except BudgetExceeded as exc:
            runtime.report()
            print(f"\nbudget stop: {exc}", file=sys.stderr)
            print(f"expanded {done} of {len(wallets)} wallet(s) before stopping.", file=sys.stderr)
            return 4
        except ApiError as exc:
            print(f"! {wallet}: {exc}", file=sys.stderr)
    print(f"\n{total_new} new trades across {done} wallets")
    runtime.report()
    print_history_endpoint(runtime)
    print_usage(runtime.budget)
    if args.analyse:
        return cmd_analyse(args, settings)
    print("next: whale-tracker analyse")
    return 0


def cmd_analyse(args: argparse.Namespace, settings: Settings) -> int:
    conn = init_db(settings.db_path)
    scoring_cfg = ScoringConfig(
        min_tokens=getattr(args, "min_tokens_score", 3),
        min_position_usd=getattr(args, "min_position_usd", 25.0),
    )
    pattern_cfg = PatternConfig(
        launch_window_seconds=getattr(args, "launch_window", 30),
        lockstep_window_seconds=getattr(args, "lockstep_window", 30),
        min_shared_tokens=getattr(args, "min_shared_tokens", 3),
    )
    positions = rebuild_positions(conn, min_trade_usd=settings.min_trade_usd)
    flags = analyse_patterns(conn, cfg=pattern_cfg)
    scored = score_all(conn, cfg=scoring_cfg)
    penalised = sum(1 for f in flags.values() if f.penalty > 0)
    print(f"positions rebuilt : {positions}")
    print(f"wallets scored    : {scored}")
    print(f"wallets flagged   : {penalised}")
    print("\nnext: whale-tracker rank")
    return 0


RANK_COLUMNS: list[Column] = [
    ("WALLET", "wallet", str),
    ("SCORE", "score", lambda v: num(v, 1)),
    ("RAW", "raw_score", lambda v: num(v, 1)),
    ("PEN", "penalty", lambda v: num(v, 2)),
    ("TOKENS", "tokens_traded", str),
    ("CLOSED", "closed_positions", str),
    ("WIN%", "win_rate", pct),
    ("MED ROI", "median_roi", pct),
    ("MEAN ROI", "mean_roi", pct),
    ("PF", "profit_factor", lambda v: num(v, 2) if v is not None else "inf"),
    ("PNL USD", "realised_pnl_usd", usd),
    ("MAXDD", "max_drawdown_pct", pct),
    ("TOP1", "top1_profit_share", pct),
    ("1HIT", "single_outlier", lambda v: "yes" if v else ""),
    ("SNIPE", "sniper_rate", pct),
    ("SYBIL", "cluster_size", lambda v: str(v) if v and int(v) > 1 else ""),
    ("AGE(d)", "days_since_last_trade", lambda v: num(v, 1)),
]


def cmd_rank(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    rows = ranked_wallets(
        conn,
        limit=args.limit,
        min_tokens=args.min_tokens,
        min_closed=args.min_closed,
        max_penalty=args.max_penalty,
        include_outliers=not args.no_outliers,
        min_pnl_usd=args.min_pnl,
    )
    dicts = rows_to_dicts(rows)
    if args.csv:
        write_csv(dicts, Path(args.csv))
        print(f"wrote {len(dicts)} rows to {args.csv}")
        return 0
    if args.json:
        print_json(dicts)
        return 0
    print_table(dicts, RANK_COLUMNS)
    if args.reasons:
        print()
        for row in dicts:
            reasons = json.loads(row.get("reasons_json") or "[]")
            if reasons:
                print(f"{row['wallet']}:")
                for reason in reasons:
                    print(f"    - {reason}")
    return 0


def cmd_wallet(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    wallet = args.address
    score = conn.execute("SELECT * FROM wallet_scores WHERE wallet = ?", (wallet,)).fetchone()
    flags = conn.execute("SELECT * FROM wallet_flags WHERE wallet = ?", (wallet,)).fetchone()
    positions = conn.execute(
        "SELECT * FROM wallet_token_pnl WHERE wallet = ? ORDER BY realised_pnl_usd DESC", (wallet,)
    ).fetchall()
    trades = conn.execute(
        "SELECT * FROM trades WHERE wallet = ? ORDER BY ts", (wallet,)
    ).fetchall()

    if not positions and not trades:
        print(f"no data for {wallet}. Has it been ingested?")
        return 1

    if args.json:
        print_json(
            {
                "wallet": wallet,
                "score": dict(score) if score else None,
                "flags": dict(flags) if flags else None,
                "positions": rows_to_dicts(positions),
                "trades": rows_to_dicts(trades) if args.trades else None,
            }
        )
        return 0

    print(f"wallet {wallet}")
    if score:
        components = json.loads(score["components_json"] or "{}")
        print(
            f"  score {score['score']:.1f}  (raw {score['raw_score']:.1f}, "
            f"penalty {score['penalty']:.2f})"
        )
        print(
            f"  tokens {score['tokens_traded']}  closed {score['closed_positions']}  "
            f"trades {score['trades']}  win {pct(score['win_rate'])}  "
            f"median ROI {pct(score['median_roi'])}  mean ROI {pct(score['mean_roi'])}"
        )
        print(
            f"  realised P&L ${usd(score['realised_pnl_usd'])}  "
            f"profit factor {num(score['profit_factor']) if score['profit_factor'] is not None else 'inf (no losing position)'}  "
            f"maxDD {pct(score['max_drawdown_pct'])}  "
            f"median hold {dur_str(score['median_hold_seconds'])}"
        )
        print(
            f"  top-1 share {pct(score['top1_profit_share'])}  "
            f"{'ONE-HIT WONDER' if score['single_outlier'] else 'profit spread across trades'}"
        )
        if score["windfall_pnl_usd"]:
            print(
                f"  unattributable proceeds (tokens never bought): ${usd(score['windfall_pnl_usd'])}"
                " — excluded from ranking"
            )
        if components.get("components"):
            parts = "  ".join(f"{k}={v:.2f}" for k, v in components["components"].items())
            print(f"  components: {parts}")
    if flags:
        reasons = json.loads(flags["reasons_json"] or "[]")
        if reasons:
            print("  flags:")
            for reason in reasons:
                print(f"    - {reason}")
        else:
            print("  flags: none")

    print("\npositions")
    print_table(
        rows_to_dicts(positions),
        [
            ("MINT", "mint", str),
            ("BUYS", "buys", str),
            ("SELLS", "sells", str),
            ("COST USD", "cost_usd", usd),
            ("PROCEEDS", "proceeds_usd", usd),
            ("PNL USD", "realised_pnl_usd", usd),
            ("ROI", "roi", pct),
            ("AVG IN", "avg_entry_price", lambda v: num(v, 9)),
            ("AVG OUT", "avg_exit_price", lambda v: num(v, 9)),
            ("HOLD", "hold_seconds", dur_str),
            ("FIRST BUY", "first_buy_ts", ts_str),
            ("OPEN", "remaining_tokens", lambda v: "yes" if v and float(v) > 0 else ""),
        ],
    )

    if args.trades:
        print(f"\ntrades ({len(trades)})")
        print_table(
            rows_to_dicts(trades),
            [
                ("TIME", "ts", ts_str),
                ("SIDE", "side", str),
                ("MINT", "mint", lambda v: str(v)[:12]),
                ("TOKENS", "token_amount", lambda v: num(v, 4)),
                ("PRICE", "price_usd", lambda v: num(v, 9)),
                ("USD", "value_usd", usd),
                ("DEX", "dex", str),
                ("SLOT", "slot", str),
                ("SIG", "signature", lambda v: str(v)[:16]),
            ],
        )
    return 0


def cmd_token(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    token = conn.execute("SELECT * FROM tokens WHERE mint = ?", (args.mint,)).fetchone()
    top = conn.execute(
        "SELECT * FROM wallet_token_pnl WHERE mint = ? ORDER BY realised_pnl_usd DESC LIMIT ?",
        (args.mint, args.limit),
    ).fetchall()
    if token is None and not top:
        print(f"no data for {args.mint}")
        return 1
    if args.json:
        print_json({"token": dict(token) if token else None, "top_wallets": rows_to_dicts(top)})
        return 0
    if token:
        print(f"{token['symbol'] or '?'}  {token['mint']}")
        print(f"  launch {ts_str(token['launch_ts'])} (slot {token['launch_slot']})  "
              f"trades {token['trade_count']}  wallets {token['wallet_count']}")
    print("\ntop wallets by realised P&L")
    print_table(
        rows_to_dicts(top),
        [
            ("WALLET", "wallet", str),
            ("PNL USD", "realised_pnl_usd", usd),
            ("ROI", "roi", pct),
            ("COST", "cost_usd", usd),
            ("HOLD", "hold_seconds", dur_str),
            ("FIRST BUY", "first_buy_ts", ts_str),
        ],
    )
    return 0


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> int:
    conn = init_db(settings.db_path)
    wallets = list(args.wallet or [])
    if args.wallets_file:
        path = Path(args.wallets_file)
        if not path.exists():
            raise SystemExit(f"wallets file not found: {path}")
        wallets.extend(
            line.split("#", 1)[0].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()
        )
    if args.from_rank:
        rows = ranked_wallets(
            conn,
            limit=args.from_rank,
            min_tokens=args.min_tokens,
            min_closed=args.min_closed,
            max_penalty=args.max_penalty,
            include_outliers=not args.no_outliers,
        )
        wallets.extend(row["wallet"] for row in rows)
    wallets = list(dict.fromkeys(w for w in wallets if w))
    if not wallets:
        raise SystemExit("no wallets to follow: use --wallet, --wallets-file or --from-rank")

    if args.start is None or args.end is None:
        window = conn.execute("SELECT MIN(ts) AS a, MAX(ts) AS b FROM trades").fetchone()
        start = args.start if args.start is not None else int(window["a"] or 0)
        end = args.end if args.end is not None else int(window["b"] or 0)
    else:
        start, end = args.start, args.end
    if not start or not end:
        raise SystemExit("no trade history in the database — ingest first")

    cfg = BacktestConfig(
        start_ts=start,
        end_ts=end,
        latency_seconds=args.latency,
        slippage=args.slippage,
        position_size_usd=args.size,
        starting_capital_usd=args.capital,
        max_open_positions=args.max_open,
        allow_duplicate_mints=args.allow_duplicate_mints,
        fee_bps=args.fee_bps,
        network_fee_usd=args.network_fee,
        max_hold_seconds=args.max_hold,
        min_leader_trade_usd=args.min_leader_trade_usd,
        seed=args.seed,
        label=args.label or "",
    )
    result = run_backtest(conn, wallets, cfg, persist=not args.no_save)

    if args.json:
        print_json(
            {
                "run_id": result.run_id,
                "config": json.loads(cfg.as_json()),
                "metrics": result.metrics,
                "trades": [t.as_row() for t in result.trades] if args.trades else None,
            }
        )
        return 0

    print(f"backtest {ts_str(start)} .. {ts_str(end)}   following {len(wallets)} wallet(s)")
    print(
        f"latency {cfg.latency_seconds[0]:.0f}-{cfg.latency_seconds[1]:.0f}s   "
        f"slippage {cfg.slippage[0] * 100:.1f}-{cfg.slippage[1] * 100:.1f}%   "
        f"size ${cfg.position_size_usd:,.0f}   capital ${cfg.starting_capital_usd:,.0f}   "
        f"fees {cfg.fee_bps:.0f}bps + ${cfg.network_fee_usd:.3f}/swap"
    )
    print()
    for line in result.summary_lines():
        print(f"  {line}")
    skipped = result.metrics.get("skipped", {})
    if any(skipped.values()):
        print("\n  skipped entries: " + ", ".join(f"{k}={v}" for k, v in skipped.items() if v))
    if result.metrics.get("unmatched_leader_sells"):
        print(f"  leader sells with no copied position: {result.metrics['unmatched_leader_sells']}")

    per_leader = result.metrics.get("per_leader", {})
    if per_leader:
        print("\nper-leader attribution")
        print_table(
            [
                {"wallet": w, **v, "win_rate": (v["wins"] / v["trades"]) if v["trades"] else 0.0}
                for w, v in sorted(per_leader.items(), key=lambda kv: -kv[1]["pnl_usd"])
            ],
            [
                ("WALLET", "wallet", str),
                ("TRADES", "trades", str),
                ("WINS", "wins", str),
                ("WIN%", "win_rate", pct),
                ("PNL USD", "pnl_usd", usd),
            ],
        )
    if args.trades:
        print("\nsimulated trades")
        print_table(
            [t.as_row() for t in result.trades],
            [
                ("LEADER", "leader", lambda v: str(v)[:12]),
                ("MINT", "mint", lambda v: str(v)[:12]),
                ("ENTRY", "entry_ts", ts_str),
                ("HOLD", "hold_seconds", dur_str),
                ("COST", "cost_usd", usd),
                ("PROCEEDS", "proceeds_usd", usd),
                ("FEES", "fees_usd", usd),
                ("PNL", "pnl_usd", usd),
                ("ROI", "roi", pct),
                ("EXIT", "exit_reason", str),
            ],
        )
    if result.run_id:
        print(f"\nsaved as backtest run #{result.run_id}")
    verdict = "GO" if result.metrics.get("net_pnl_usd", 0) > 0 else "NO-GO"
    print(f"\n==> {verdict}: ${result.metrics.get('net_pnl_usd', 0):,.2f} net on "
          f"${cfg.starting_capital_usd:,.0f} capital "
          f"({pct(result.metrics.get('roi_on_capital'))})")
    return 0


def cmd_backtests(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    if args.run_id:
        row = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (args.run_id,)).fetchone()
        if row is None:
            print(f"no backtest run #{args.run_id}")
            return 1
        payload = {
            "id": row["id"],
            "created_at": ts_str(row["created_at"]),
            "label": row["label"],
            "params": json.loads(row["params_json"]),
            "metrics": json.loads(row["metrics_json"]),
        }
        if args.json:
            print_json(payload)
            return 0
        print_json(payload)
        return 0

    rows = conn.execute(
        "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    table = []
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        table.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "label": row["label"],
                "trades": metrics.get("trades"),
                "win_rate": metrics.get("win_rate"),
                "net_pnl_usd": metrics.get("net_pnl_usd"),
                "roi_on_capital": metrics.get("roi_on_capital"),
                "drag_usd": metrics.get("drag_usd"),
            }
        )
    if args.json:
        print_json(table)
        return 0
    print_table(
        table,
        [
            ("ID", "id", str),
            ("WHEN", "created_at", ts_str),
            ("LABEL", "label", str),
            ("TRADES", "trades", str),
            ("WIN%", "win_rate", pct),
            ("NET PNL", "net_pnl_usd", usd),
            ("ROI", "roi_on_capital", pct),
            ("DRAG", "drag_usd", usd),
        ],
    )
    return 0


def cmd_price_check(args: argparse.Namespace, settings: Settings) -> int:
    """Probe each configured price source in isolation.

    Worth running once before a real ingest: it proves the keyless chain can
    actually reach its upstreams from your network, and shows which source
    answers for history (only some can).
    """
    settings = apply_source_overrides(settings, args)
    # A connectivity probe should fail fast: the point is to find out whether a
    # source answers, not to sit through a full retry ladder for each one.
    settings = replace(settings, http_max_retries=max(1, args.retries),
                       http_timeout_seconds=min(settings.http_timeout_seconds, args.timeout))
    conn = init_db(settings.db_path)
    budget = make_budget(settings, conn, args)
    mint = args.mint or WSOL_MINT
    ts = args.ts or (int(time.time()) - 7 * 86_400)

    rows = []
    for name in settings.active_price_sources():
        chain = build_price_chain(settings, conn, budget, sources=[name])
        source = chain.sources[0] if chain.sources else None
        if source is None:
            continue
        row = {"source": name, "history": "yes" if source.supports_history else "no"}

        started = time.time()
        try:
            row["spot"] = source.spot(mint)
            row["spot_error"] = ""
        except Exception as exc:
            row["spot"] = None
            row["spot_error"] = f"{type(exc).__name__}: {exc}"[:90]
        row["spot_ms"] = int((time.time() - started) * 1000)

        if source.supports_history:
            started = time.time()
            try:
                points = source.history(mint, ts)
                row["points"] = len(points)
                row["history_error"] = ""
            except Exception as exc:
                row["points"] = 0
                row["history_error"] = f"{type(exc).__name__}: {exc}"[:90]
            row["history_ms"] = int((time.time() - started) * 1000)
        else:
            row["points"] = "-"
            row["history_ms"] = "-"
            row["history_error"] = ""
        rows.append(row)

    if args.json:
        print_json({"mint": mint, "ts": ts, "sources": rows})
        return 0

    print(f"price sources for {mint}   (history probe at {ts_str(ts)})")
    print_table(
        rows,
        [
            ("SOURCE", "source", str),
            ("HISTORY?", "history", str),
            ("SPOT USD", "spot", lambda v: num(v, 9) if v else "-"),
            ("SPOT ms", "spot_ms", str),
            ("POINTS", "points", str),
            ("HIST ms", "history_ms", str),
            ("ERROR", "spot_error", lambda v: str(v)[:44]),
            ("HIST ERROR", "history_error", lambda v: str(v)[:44]),
        ],
    )
    working = [r for r in rows if r["spot"] or (isinstance(r["points"], int) and r["points"])]
    if not working:
        print("\nNo source answered. Check network egress, then try --price-sources one at a time.")
        return 1
    history_ok = [r for r in rows if isinstance(r["points"], int) and r["points"]]
    if not history_ok:
        print(
            "\nNo source returned historical points: old trades will fall back to the nearest "
            "cached price, or to --sol-price. Enable Birdeye (--enable-birdeye) for full history."
        )
    print_usage(budget)
    return 0


def cmd_demo_seed(args: argparse.Namespace, settings: Settings) -> int:
    from .demo import seed_demo

    conn = init_db(settings.db_path)
    summary = seed_demo(conn, tokens=args.tokens, seed=args.seed, days=args.days)
    print(f"seeded {summary['tokens']} synthetic tokens / {summary['trades']} trades")
    for archetype, wallets in summary["archetypes"].items():
        for wallet in wallets:
            print(f"  {archetype:11} {wallet}")
    print("\nnext: whale-tracker analyse && whale-tracker rank")
    return 0


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    allowed = {
        "ranked": "SELECT * FROM v_ranked_wallets",
        "scores": "SELECT * FROM wallet_scores ORDER BY score DESC",
        "flags": "SELECT * FROM wallet_flags",
        "positions": "SELECT * FROM wallet_token_pnl",
        "trades": "SELECT * FROM trades ORDER BY ts",
        "tokens": "SELECT * FROM tokens",
    }
    if args.table not in allowed:
        raise SystemExit(f"--table must be one of: {', '.join(sorted(allowed))}")
    rows = rows_to_dicts(conn.execute(allowed[args.table]).fetchall())
    write_csv(rows, Path(args.out))
    print(f"wrote {len(rows)} rows from {args.table} to {args.out}")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def add_budget_flags(parser: argparse.ArgumentParser) -> None:
    """Per-run budget overrides, available on every network-touching command."""
    parser.add_argument("--max-requests", type=int,
                        help="override the per-run request cap for every provider")
    parser.add_argument("--max-credits", type=int,
                        help="override the per-run Helius credit cap")


def add_history_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--history-strategy",
        choices=["auto", "parsed_events", "bulk_history", "enhanced_tx"],
        help="transaction-history endpoint to prefer (default: auto, cheapest first)",
    )


def add_price_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--price-sources",
                        help="comma-separated keyless sources in priority order "
                             "(default: jupiter,dexscreener,geckoterminal)")
    parser.add_argument("--enable-birdeye", action="store_true",
                        help="also use Birdeye for prices (needs BIRDEYE_API_KEY)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whale-tracker",
        description="Discover and score Solana memecoin wallets worth copying (read-only).",
    )
    parser.add_argument("--version", action="version", version=f"whale-tracker {__version__}")
    parser.add_argument("--env-file", help="path to a .env file (default: nearest .env)")
    parser.add_argument("--db", help="override the SQLite path from the environment")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-format", choices=["json", "console"])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the SQLite schema").set_defaults(func=cmd_init_db)

    status = sub.add_parser("status", help="show database and key status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    ingest = sub.add_parser("ingest", help="pull every wallet that traded the given tokens")
    ingest.add_argument("--token", action="append", help="token mint (repeatable)")
    ingest.add_argument("--tokens-file", help="file with one mint per line")
    ingest.add_argument("--provider", choices=["helius", "birdeye", "both"], default="helius")
    ingest.add_argument("--max-txs", type=int, help="cap transactions per token")
    ingest.add_argument("--since", type=parse_time, help="only trades at/after this time")
    ingest.add_argument("--sol-price", type=float,
                        help="fallback SOL/USD when no price history is available")
    ingest.add_argument("--analyse", action="store_true", help="run analyse when done")
    ingest.add_argument("--dry-run", action="store_true",
                        help="project API calls and Helius credits without calling anything")
    ingest.add_argument("--json", action="store_true", help="machine-readable dry-run output")
    add_budget_flags(ingest)
    add_price_flags(ingest)
    add_history_flags(ingest)
    ingest.set_defaults(func=cmd_ingest)

    expand = sub.add_parser(
        "expand", help="pull the wider trade history of candidate wallets (Helius)"
    )
    expand.add_argument("--wallet", action="append", help="specific wallet (repeatable)")
    expand.add_argument("--limit", type=int, default=50, help="max candidates to expand")
    expand.add_argument("--min-tokens", type=int, default=2,
                        help="candidate must appear in this many ingested tokens")
    expand.add_argument("--min-trades", type=int, default=2)
    expand.add_argument("--min-volume", type=float, default=0.0)
    expand.add_argument("--max-txs-per-wallet", type=int,
                        help="tx cap per wallet (default: MAX_TXS_PER_WALLET, 200)")
    expand.add_argument("--max-wallets-per-token", type=int,
                        help="candidate wallets taken from each seed token "
                             "(default: MAX_WALLETS_PER_TOKEN, 50)")
    expand.add_argument("--since", type=parse_time)
    expand.add_argument("--sol-price", type=float)
    expand.add_argument("--analyse", action="store_true")
    expand.add_argument("--dry-run", action="store_true",
                        help="project API calls and Helius credits without calling anything")
    expand.add_argument("--json", action="store_true", help="machine-readable dry-run output")
    add_budget_flags(expand)
    add_price_flags(expand)
    add_history_flags(expand)
    expand.set_defaults(func=cmd_expand)

    analyse = sub.add_parser("analyse", help="rebuild P&L, detect patterns and score wallets")
    analyse.add_argument("--min-tokens-score", type=int, default=3,
                         help="closed positions needed for a full-confidence score")
    analyse.add_argument("--min-position-usd", type=float, default=25.0)
    analyse.add_argument("--launch-window", type=int, default=30,
                         help="seconds after launch that count as a sniper entry")
    analyse.add_argument("--lockstep-window", type=int, default=30,
                         help="seconds within which two wallets count as co-entering")
    analyse.add_argument("--min-shared-tokens", type=int, default=3)
    analyse.set_defaults(func=cmd_analyse)

    rank = sub.add_parser("rank", help="the ranked candidate table")
    rank.add_argument("--limit", type=int, default=25)
    rank.add_argument("--min-tokens", type=int, default=3)
    rank.add_argument("--min-closed", type=int, default=3)
    rank.add_argument("--max-penalty", type=float, default=1.0)
    rank.add_argument("--min-pnl", type=float, help="minimum realised P&L in USD")
    rank.add_argument("--no-outliers", action="store_true",
                      help="hide wallets whose profit is one big win")
    rank.add_argument("--reasons", action="store_true", help="print why wallets were penalised")
    rank.add_argument("--json", action="store_true")
    rank.add_argument("--csv", help="write to this CSV path instead of stdout")
    rank.set_defaults(func=cmd_rank)

    wallet = sub.add_parser("wallet", help="inspect one wallet")
    wallet.add_argument("address")
    wallet.add_argument("--trades", action="store_true", help="include the full trade history")
    wallet.add_argument("--json", action="store_true")
    wallet.set_defaults(func=cmd_wallet)

    token = sub.add_parser("token", help="inspect one token and its best wallets")
    token.add_argument("mint")
    token.add_argument("--limit", type=int, default=20)
    token.add_argument("--json", action="store_true")
    token.set_defaults(func=cmd_token)

    backtest = sub.add_parser(
        "backtest", help="simulate copying a wallet set with latency and slippage"
    )
    backtest.add_argument("--wallet", action="append", help="leader wallet (repeatable)")
    backtest.add_argument("--wallets-file", help="file with one wallet per line")
    backtest.add_argument("--from-rank", type=int, help="follow the top N ranked wallets")
    backtest.add_argument("--start", type=parse_time, help="window start (default: first trade)")
    backtest.add_argument("--end", type=parse_time, help="window end (default: last trade)")
    backtest.add_argument("--latency", type=lambda v: parse_range(v), default=(15.0, 30.0),
                          help="reaction delay in seconds, e.g. 15-30")
    backtest.add_argument("--slippage", type=lambda v: parse_range(v, scale=0.01),
                          default=(0.01, 0.03), help="slippage in percent, e.g. 1-3")
    backtest.add_argument("--size", type=float, default=100.0, help="USD per copied entry")
    backtest.add_argument("--capital", type=float, default=1_000.0, help="starting capital in USD")
    backtest.add_argument("--max-open", type=int, default=10, help="max concurrent positions")
    backtest.add_argument("--allow-duplicate-mints", action="store_true",
                          help="copy several leaders into the same token")
    backtest.add_argument("--fee-bps", type=float, default=30.0, help="round-trip fee, basis points")
    backtest.add_argument("--network-fee", type=float, default=0.05,
                          help="fixed USD cost per swap (priority fee)")
    backtest.add_argument("--max-hold", type=int, default=0,
                          help="force an exit after N seconds (0 = follow the leader)")
    backtest.add_argument("--min-leader-trade-usd", type=float, default=0.0)
    backtest.add_argument("--min-tokens", type=int, default=3, help="filter for --from-rank")
    backtest.add_argument("--min-closed", type=int, default=3, help="filter for --from-rank")
    backtest.add_argument("--max-penalty", type=float, default=1.0, help="filter for --from-rank")
    backtest.add_argument("--no-outliers", action="store_true", help="filter for --from-rank")
    backtest.add_argument("--seed", type=int, default=1337)
    backtest.add_argument("--label", help="name this run")
    backtest.add_argument("--trades", action="store_true", help="print every simulated trade")
    backtest.add_argument("--no-save", action="store_true", help="do not store the run")
    backtest.add_argument("--json", action="store_true")
    backtest.set_defaults(func=cmd_backtest)

    runs = sub.add_parser("backtests", help="list or show stored backtest runs")
    runs.add_argument("--run-id", type=int)
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--json", action="store_true")
    runs.set_defaults(func=cmd_backtests)

    price_check = sub.add_parser(
        "price-check", help="probe each price source and report which ones answer"
    )
    price_check.add_argument("--mint", help="mint to price (default: wrapped SOL)")
    price_check.add_argument("--ts", type=parse_time,
                             help="timestamp for the history probe (default: 7 days ago)")
    price_check.add_argument("--retries", type=int, default=1,
                             help="attempts per source before giving up (default 1)")
    price_check.add_argument("--timeout", type=float, default=10.0,
                             help="per-request timeout in seconds (default 10)")
    price_check.add_argument("--json", action="store_true")
    add_budget_flags(price_check)
    add_price_flags(price_check)
    price_check.set_defaults(func=cmd_price_check)

    demo = sub.add_parser("demo-seed", help="fill the database with a synthetic universe")
    demo.add_argument("--tokens", type=int, default=10)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--days", type=int, default=45)
    demo.set_defaults(func=cmd_demo_seed)

    export = sub.add_parser("export", help="dump a table to CSV")
    export.add_argument("--table", required=True,
                        help="ranked | scores | flags | positions | trades | tokens")
    export.add_argument("--out", required=True)
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.db:
        settings = Settings(**{**settings.__dict__, "db_path": Path(args.db)})
    configure_logging(
        args.log_level or settings.log_level,
        args.log_format or settings.log_format,
        settings.log_file,
    )

    try:
        return int(args.func(args, settings) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except BudgetExceeded as exc:
        print(f"budget stop: {exc}", file=sys.stderr)
        return 4
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
