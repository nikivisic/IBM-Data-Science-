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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import __version__
from .backtest import BacktestConfig, run_backtest
from .clients.base import ApiError
from .clients.birdeye import BirdeyeClient
from .clients.helius import HeliusClient
from .config import ConfigError, Settings, load_settings
from .db import connect, init_db, table_counts
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


def make_clients(
    settings: Settings, conn: sqlite3.Connection, provider: str
) -> tuple[Optional[HeliusClient], Optional[BirdeyeClient]]:
    helius = birdeye = None
    if provider in ("helius", "both"):
        helius = HeliusClient(settings, conn)
    if provider in ("birdeye", "both"):
        birdeye = BirdeyeClient(settings, conn)
    if provider == "helius" and settings.birdeye_api_key:
        # Still useful: Birdeye prices the SOL leg of every historical trade.
        try:
            birdeye = BirdeyeClient(settings, conn)
        except ConfigError:
            birdeye = None
    return helius, birdeye


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
    payload = {
        "db_path": str(settings.db_path),
        "counts": counts,
        "trade_window": {"from": ts_str(window["a"]), "to": ts_str(window["b"])},
        "helius_key": bool(settings.helius_api_key),
        "birdeye_key": bool(settings.birdeye_api_key),
        "version": __version__,
    }
    if args.json:
        print_json(payload)
        return 0
    print(f"whale-tracker {__version__}   db={settings.db_path}")
    print(f"trade window: {payload['trade_window']['from']} .. {payload['trade_window']['to']}")
    print(f"keys: helius={'set' if settings.helius_api_key else 'MISSING'} "
          f"birdeye={'set' if settings.birdeye_api_key else 'MISSING'}")
    print_table(
        [{"table": k, "rows": v} for k, v in counts.items()],
        [("TABLE", "table", str), ("ROWS", "rows", str)],
    )
    return 0


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    mints = read_token_list(args)
    if not mints:
        raise SystemExit("no tokens given: use --token <mint> (repeatable) or --tokens-file")
    conn = init_db(settings.db_path)
    helius, birdeye = make_clients(settings, conn, args.provider)
    oracle = PriceOracle(conn, birdeye, static_sol_price=args.sol_price)

    results = []
    failures = 0
    for mint in mints:
        try:
            result = ingest_token(
                conn,
                mint,
                settings=settings,
                helius=helius,
                birdeye=birdeye,
                oracle=oracle,
                max_txs=args.max_txs,
                since_ts=args.since,
                provider=args.provider,
            )
            results.append(result)
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
    if failures and not results:
        print(f"\nall {failures} token(s) failed — nothing ingested", file=sys.stderr)
        return 3
    if failures:
        print(f"\n{failures} of {len(mints)} token(s) failed; see the log", file=sys.stderr)
    if args.analyse:
        return cmd_analyse(args, settings)
    print("\nnext: whale-tracker expand   (pull each candidate's wider history)")
    return 0


def cmd_expand(args: argparse.Namespace, settings: Settings) -> int:
    conn = init_db(settings.db_path)
    if args.wallet:
        wallets = list(args.wallet)
    else:
        wallets = candidate_wallets(
            conn,
            min_tokens=args.min_tokens,
            min_trades=args.min_trades,
            min_volume_usd=args.min_volume,
            limit=args.limit,
        )
    if not wallets:
        print("no candidate wallets matched; ingest more tokens or lower --min-tokens")
        return 0

    helius, birdeye = make_clients(settings, conn, "helius")
    if helius is None:
        raise SystemExit("expand needs a Helius key (wallet history comes from Helius)")
    oracle = PriceOracle(conn, birdeye, static_sol_price=args.sol_price)

    total_new = 0
    for index, wallet in enumerate(wallets, start=1):
        try:
            result = expand_wallet(
                conn,
                wallet,
                settings=settings,
                helius=helius,
                oracle=oracle,
                max_txs=args.max_txs,
                since_ts=args.since,
            )
            total_new += result.trades_written
            print(f"[{index}/{len(wallets)}] {wallet}  +{result.trades_written} trades "
                  f"({result.txs_seen} txs)")
        except ApiError as exc:
            print(f"! {wallet}: {exc}", file=sys.stderr)
    print(f"\n{total_new} new trades across {len(wallets)} wallets")
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
    expand.add_argument("--max-txs", type=int, default=1_000, help="tx cap per wallet")
    expand.add_argument("--since", type=parse_time)
    expand.add_argument("--sol-price", type=float)
    expand.add_argument("--analyse", action="store_true")
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
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
