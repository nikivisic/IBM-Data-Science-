"""SQLite storage layer.

One file, WAL mode, plain SQL. The schema is intentionally denormalised around
the three questions the tool answers: what did a wallet trade, how did it do,
and should we trust it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Tokens we have ingested trade history for.
CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,
    symbol          TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    decimals        INTEGER DEFAULT 0,
    launch_ts       INTEGER,
    launch_slot     INTEGER,
    first_trade_ts  INTEGER,
    last_trade_ts   INTEGER,
    trade_count     INTEGER DEFAULT 0,
    wallet_count    INTEGER DEFAULT 0,
    ingested_at     INTEGER,
    meta_json       TEXT DEFAULT '{}'
);

-- Normalised swap legs. One row = one wallet moving in/out of one mint.
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signature     TEXT NOT NULL,
    wallet        TEXT NOT NULL,
    mint          TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
    token_amount  REAL NOT NULL,
    quote_mint    TEXT DEFAULT '',
    quote_amount  REAL DEFAULT 0,
    price_usd     REAL DEFAULT 0,
    value_usd     REAL DEFAULT 0,
    ts            INTEGER NOT NULL,
    slot          INTEGER DEFAULT 0,
    dex           TEXT DEFAULT '',
    source        TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_dedupe
    ON trades (signature, wallet, mint, side, token_amount);
CREATE INDEX IF NOT EXISTS ix_trades_mint_ts   ON trades (mint, ts);
CREATE INDEX IF NOT EXISTS ix_trades_wallet_ts ON trades (wallet, ts);
CREATE INDEX IF NOT EXISTS ix_trades_wallet_mint ON trades (wallet, mint, ts);

-- Realised P&L per (wallet, mint).
CREATE TABLE IF NOT EXISTS wallet_token_pnl (
    wallet              TEXT NOT NULL,
    mint                TEXT NOT NULL,
    buys                INTEGER DEFAULT 0,
    sells               INTEGER DEFAULT 0,
    tokens_bought       REAL DEFAULT 0,
    tokens_sold         REAL DEFAULT 0,
    cost_usd            REAL DEFAULT 0,
    proceeds_usd        REAL DEFAULT 0,
    total_buy_usd       REAL DEFAULT 0,
    total_sell_usd      REAL DEFAULT 0,
    realised_pnl_usd    REAL DEFAULT 0,
    roi                 REAL,
    avg_entry_price     REAL,
    avg_exit_price      REAL,
    first_buy_ts        INTEGER,
    first_buy_slot      INTEGER,
    last_sell_ts        INTEGER,
    hold_seconds        REAL,
    remaining_tokens    REAL DEFAULT 0,
    remaining_cost_usd  REAL DEFAULT 0,
    unrealised_pnl_usd  REAL,
    zero_cost_tokens    REAL DEFAULT 0,
    zero_cost_proceeds_usd REAL DEFAULT 0,
    is_closed           INTEGER DEFAULT 0,
    PRIMARY KEY (wallet, mint)
);
CREATE INDEX IF NOT EXISTS ix_pnl_wallet ON wallet_token_pnl (wallet);
CREATE INDEX IF NOT EXISTS ix_pnl_mint   ON wallet_token_pnl (mint);

-- Repeatability scorecards.
CREATE TABLE IF NOT EXISTS wallet_scores (
    wallet                TEXT PRIMARY KEY,
    tokens_traded         INTEGER DEFAULT 0,
    closed_positions      INTEGER DEFAULT 0,
    trades                INTEGER DEFAULT 0,
    wins                  INTEGER DEFAULT 0,
    losses                INTEGER DEFAULT 0,
    win_rate              REAL DEFAULT 0,
    win_rate_shrunk       REAL DEFAULT 0,
    median_roi            REAL DEFAULT 0,
    mean_roi              REAL DEFAULT 0,
    roi_stdev             REAL DEFAULT 0,
    gross_profit_usd      REAL DEFAULT 0,
    gross_loss_usd        REAL DEFAULT 0,
    realised_pnl_usd      REAL DEFAULT 0,
    windfall_pnl_usd      REAL DEFAULT 0,
    profit_factor         REAL,
    max_drawdown_usd      REAL DEFAULT 0,
    max_drawdown_pct      REAL DEFAULT 0,
    median_hold_seconds   REAL,
    first_trade_ts        INTEGER,
    last_trade_ts         INTEGER,
    days_since_last_trade REAL,
    top1_profit_share     REAL DEFAULT 0,
    top3_profit_share     REAL DEFAULT 0,
    profit_hhi            REAL DEFAULT 0,
    single_outlier        INTEGER DEFAULT 0,
    pnl_without_best_usd  REAL DEFAULT 0,
    consistency_score     REAL DEFAULT 0,
    raw_score             REAL DEFAULT 0,
    penalty               REAL DEFAULT 0,
    score                 REAL DEFAULT 0,
    components_json       TEXT DEFAULT '{}',
    scored_at             INTEGER
);
CREATE INDEX IF NOT EXISTS ix_scores_score ON wallet_scores (score DESC);

-- Bait / insider / sybil signals.
CREATE TABLE IF NOT EXISTS wallet_flags (
    wallet                     TEXT PRIMARY KEY,
    launch_block_buys          INTEGER DEFAULT 0,
    launch_window_buys         INTEGER DEFAULT 0,
    sniper_rate                REAL DEFAULT 0,
    median_entry_lag_seconds   REAL,
    insider_p_value            REAL,
    insider_suspicion          REAL DEFAULT 0,
    cluster_id                 INTEGER,
    cluster_size               INTEGER DEFAULT 1,
    lockstep_score             REAL DEFAULT 0,
    lockstep_peers             INTEGER DEFAULT 0,
    penalty                    REAL DEFAULT 0,
    reasons_json               TEXT DEFAULT '[]',
    analysed_at                INTEGER
);
CREATE INDEX IF NOT EXISTS ix_flags_cluster ON wallet_flags (cluster_id);

-- Historical USD price points (mostly SOL), bucketed to reduce API calls.
CREATE TABLE IF NOT EXISTS price_points (
    mint       TEXT NOT NULL,
    ts_bucket  INTEGER NOT NULL,
    price_usd  REAL NOT NULL,
    PRIMARY KEY (mint, ts_bucket)
);

-- Raw HTTP response cache so re-runs don't re-spend API credits.
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    payload    TEXT NOT NULL
);

-- Backtest runs and their simulated fills.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    label         TEXT DEFAULT '',
    params_json   TEXT NOT NULL,
    metrics_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    leader            TEXT NOT NULL,
    mint              TEXT NOT NULL,
    entry_ts          INTEGER,
    exit_ts           INTEGER,
    leader_entry_ts   INTEGER,
    leader_exit_ts    INTEGER,
    entry_price       REAL,
    exit_price        REAL,
    leader_entry_price REAL,
    leader_exit_price  REAL,
    tokens            REAL,
    cost_usd          REAL,
    proceeds_usd      REAL,
    fees_usd          REAL,
    pnl_usd           REAL,
    roi               REAL,
    hold_seconds      REAL,
    exit_reason       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_bt_trades_run ON backtest_trades (run_id);

-- Ingestion bookkeeping.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    mint        TEXT,
    provider    TEXT,
    txs_seen    INTEGER DEFAULT 0,
    trades_new  INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'running',
    detail      TEXT DEFAULT ''
);

-- Convenience view: the ranked candidate table.
CREATE VIEW IF NOT EXISTS v_ranked_wallets AS
SELECT
    s.wallet,
    s.score,
    s.raw_score,
    s.penalty,
    s.tokens_traded,
    s.closed_positions,
    s.trades,
    s.win_rate,
    s.median_roi,
    s.mean_roi,
    s.profit_factor,
    s.realised_pnl_usd,
    s.windfall_pnl_usd,
    s.max_drawdown_pct,
    s.top1_profit_share,
    s.single_outlier,
    s.consistency_score,
    s.days_since_last_trade,
    COALESCE(f.sniper_rate, 0)        AS sniper_rate,
    COALESCE(f.insider_suspicion, 0)  AS insider_suspicion,
    COALESCE(f.cluster_size, 1)       AS cluster_size,
    COALESCE(f.lockstep_score, 0)     AS lockstep_score,
    COALESCE(f.reasons_json, '[]')    AS reasons_json
FROM wallet_scores s
LEFT JOIN wallet_flags f ON f.wallet = s.wallet
ORDER BY s.score DESC;
"""


def connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open (and if needed create) the database with sane pragmas."""
    path = Path(db_path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent and return an open connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    log.debug("db.ready", extra={"ctx": {"path": str(db_path), "version": SCHEMA_VERSION}})
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on failure."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    conflict_cols: Sequence[str],
    *,
    ignore: bool = False,
) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    col_sql = ",".join(cols)
    if ignore:
        conflict = "ON CONFLICT DO NOTHING"
    else:
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in conflict_cols)
        target = ",".join(conflict_cols)
        conflict = (
            f"ON CONFLICT({target}) DO UPDATE SET {updates}" if updates else "ON CONFLICT DO NOTHING"
        )
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) {conflict}"
    before = conn.total_changes
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return conn.total_changes - before


def insert_trades(conn: sqlite3.Connection, trades: Iterable[Mapping[str, Any]]) -> int:
    """Insert trades, silently skipping duplicates. Returns rows actually added."""
    rows = list(trades)
    return _upsert(conn, "trades", rows, ["signature", "wallet", "mint", "side", "token_amount"], ignore=True)


def upsert_token(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    payload = dict(row)
    payload.setdefault("ingested_at", int(time.time()))
    if isinstance(payload.get("meta_json"), (dict, list)):
        payload["meta_json"] = json.dumps(payload["meta_json"])
    _upsert(conn, "tokens", [payload], ["mint"])


def upsert_pnl(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    return _upsert(conn, "wallet_token_pnl", rows, ["wallet", "mint"])


def upsert_scores(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    stamped = [{**r, "scored_at": int(time.time())} for r in rows]
    return _upsert(conn, "wallet_scores", stamped, ["wallet"])


def upsert_flags(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    stamped = [{**r, "analysed_at": int(time.time())} for r in rows]
    return _upsert(conn, "wallet_flags", stamped, ["wallet"])


def upsert_price_points(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    return _upsert(conn, "price_points", rows, ["mint", "ts_bucket"])


def cache_get(conn: sqlite3.Connection, key: str, ttl_seconds: int) -> Optional[Any]:
    if ttl_seconds <= 0:
        return None
    row = conn.execute(
        "SELECT fetched_at, payload FROM http_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    if int(time.time()) - int(row["fetched_at"]) > ttl_seconds:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:  # pragma: no cover - corrupt cache entry
        return None


def cache_put(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO http_cache(key, fetched_at, payload) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
        (key, int(time.time()), json.dumps(payload)),
    )
    conn.commit()


def start_ingest_run(conn: sqlite3.Connection, mint: str, provider: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_runs(started_at, mint, provider) VALUES (?,?,?)",
        (int(time.time()), mint, provider),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_ingest_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    txs_seen: int,
    trades_new: int,
    status: str = "ok",
    detail: str = "",
) -> None:
    conn.execute(
        "UPDATE ingest_runs SET finished_at=?, txs_seen=?, trades_new=?, status=?, detail=? WHERE id=?",
        (int(time.time()), txs_seen, trades_new, status, detail, run_id),
    )
    conn.commit()


def fetch_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for the main tables — used by `status` and tests."""
    names = [
        "tokens",
        "trades",
        "wallet_token_pnl",
        "wallet_scores",
        "wallet_flags",
        "backtest_runs",
        "backtest_trades",
    ]
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {name}").fetchone()["c"])
        except sqlite3.Error:  # pragma: no cover - table missing on old db
            out[name] = -1
    return out
