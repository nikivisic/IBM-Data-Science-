"""Transaction-history strategies for Helius, cheapest first.

The Enhanced Transactions API is billed at 100 credits per call and Helius now
recommends migrating off it. This module puts the history fetch behind a small
strategy interface so the client can use a cheaper endpoint where one is
available, and fall back only when it genuinely is not:

1. **Parsed Events API** (open beta, cheapest) — pre-decoded swap events.
2. **Bulk address history** (`getTransactionsForAddress`-style, ~10 credits) —
   a paginated transaction fetch over the same signature range the Enhanced
   endpoint walks.
3. **Enhanced Transactions API** (100 credits) — the previous behaviour, kept
   as a last resort so a plan that only has this still works.

Selection is a *runtime* capability probe, not an assumption: the first page of
the preferred strategy either comes back or tells us the endpoint is missing or
not enabled on this plan (404, "unknown method", -32601, a feature-gated 403),
in which case the chain moves down. Rate limits and server errors are **not**
treated as unavailability — those are transient, the HTTP layer already retries
them, and falling back on one would silently double-spend.

Every payload is adapted into the shape `extract_legs` already consumes, so the
parser, the ingestion pipeline and the scoring behind it are untouched.

Endpoint paths, RPC method names, page sizes and per-endpoint credit costs are
all configurable: this code cannot see Helius's price list or its beta rollout,
so nothing about either is hardcoded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence

from ..config import Settings
from ..logging_setup import get_logger
from .base import ApiError

log = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

STRATEGY_PARSED_EVENTS = "parsed_events"
STRATEGY_BULK_HISTORY = "bulk_history"
STRATEGY_ENHANCED_TX = "enhanced_tx"

#: Cheapest first. `auto` walks this order.
STRATEGY_ORDER = (STRATEGY_PARSED_EVENTS, STRATEGY_BULK_HISTORY, STRATEGY_ENHANCED_TX)
KNOWN_STRATEGIES = frozenset({*STRATEGY_ORDER, "auto"})

#: JSON-RPC error code for an unknown method.
RPC_METHOD_NOT_FOUND = -32601

#: Phrases that mean "this endpoint is not available to you", as opposed to
#: "this request failed". Matched case-insensitively against the error body.
UNAVAILABLE_PATTERNS = re.compile(
    r"not\s+enabled|not\s+found|unknown\s+method|method\s+not\s+found|unsupported|"
    r"not\s+available|no\s+access|requires?\s+(a\s+)?(paid|upgraded)|beta\s+access|"
    r"invalid\s+method|does\s+not\s+exist",
    re.IGNORECASE,
)

#: Best-effort DEX labels for raw transactions, which carry no `source` field.
#: Cosmetic only — `dex` is displayed, never scored on.
PROGRAM_LABELS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "RAYDIUM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "RAYDIUM",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS": "RAYDIUM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "ORCA",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "ORCA",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "PUMP_FUN",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PUMP_FUN",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "METEORA",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "METEORA",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "JUPITER",
    "JUP4Fb2cqiRUcaTNdFAZ3cVJnvRhDqfKjWwRPa4vqJC": "JUPITER",
}


class StrategyUnavailable(RuntimeError):
    """This endpoint is missing or not enabled — try the next strategy.

    Deliberately distinct from `ApiError`: a rate limit or a 500 means "try
    again", not "this endpoint does not exist", and must never trigger a
    fallback that would pay for the same page twice.
    """


def _is_unavailable(exc: ApiError) -> bool:
    """Does this failure mean the endpoint isn't available on this plan?"""
    if exc.status in (404, 501):
        return True
    if exc.status in (400, 403, 405) and UNAVAILABLE_PATTERNS.search(str(exc) + exc.body):
        return True
    return False


def _rpc_error_is_unavailable(error: Any) -> bool:
    if not isinstance(error, dict):
        return bool(error) and bool(UNAVAILABLE_PATTERNS.search(str(error)))
    if int(error.get("code") or 0) == RPC_METHOD_NOT_FOUND:
        return True
    return bool(UNAVAILABLE_PATTERNS.search(str(error.get("message") or "")))


# ---------------------------------------------------------------------------
# payload adapters
# ---------------------------------------------------------------------------


def looks_enhanced(tx: Any) -> bool:
    """Is this already in the shape `extract_legs` expects?"""
    if not isinstance(tx, dict):
        return False
    if not tx.get("signature"):
        return False
    return any(key in tx for key in ("events", "tokenTransfers", "accountData", "nativeTransfers"))


def _first(source: dict[str, Any], *names: str, default: Any = None) -> Any:
    """First present key among `names` — providers differ on camel vs snake case."""
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return default


def _account_keys(raw: dict[str, Any]) -> list[str]:
    message = (raw.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or message.get("account_keys") or []
    out: list[str] = []
    for key in keys:
        if isinstance(key, str):
            out.append(key)
        elif isinstance(key, dict):
            out.append(str(key.get("pubkey") or key.get("publicKey") or ""))
    # Address-lookup-table accounts are appended after the static keys.
    meta = raw.get("meta") or {}
    loaded = meta.get("loadedAddresses") or meta.get("loaded_addresses") or {}
    for group in ("writable", "readonly"):
        for key in loaded.get(group) or []:
            out.append(str(key))
    return out


def _label_for(raw: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        if key in PROGRAM_LABELS:
            return PROGRAM_LABELS[key]
    for instruction in ((raw.get("meta") or {}).get("innerInstructions") or []):
        for inner in instruction.get("instructions") or []:
            program = inner.get("programId") or inner.get("program_id")
            if program in PROGRAM_LABELS:
                return PROGRAM_LABELS[program]
    return ""


def adapt_raw_transaction(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Convert a raw JSON-RPC transaction into the enhanced shape.

    Only the parts `extract_legs` reads are reconstructed: per-account native
    balance changes and per-(owner, mint) token balance changes, derived from
    `meta.preBalances`/`postBalances` and `meta.pre/postTokenBalances`. That is
    the same `accountData` fallback path the parser already supports for
    Enhanced payloads whose swap event failed to decode, so no parser change is
    needed — and balance deltas are strictly more reliable than a decoded event
    anyway.
    """
    if not isinstance(raw, dict):
        return None
    meta = raw.get("meta") or {}
    if meta.get("err"):
        return None  # a failed transaction moved no value

    transaction = raw.get("transaction") or {}
    signatures = transaction.get("signatures") or raw.get("signatures") or []
    signature = signatures[0] if signatures else _first(raw, "signature", "txHash", default="")
    if not signature:
        return None

    keys = _account_keys(raw)
    fee = int(meta.get("fee") or 0)
    fee_payer = keys[0] if keys else ""

    pre_lamports = meta.get("preBalances") or meta.get("pre_balances") or []
    post_lamports = meta.get("postBalances") or meta.get("post_balances") or []

    # Token balances are reported per token account; sum them per owner so the
    # result matches how Enhanced reports `userAccount`.
    token_deltas: dict[tuple[str, str], dict[str, Any]] = {}

    def fold(entries: Any, sign: int) -> None:
        for entry in entries or []:
            owner = _first(entry, "owner", default="")
            mint = _first(entry, "mint", default="")
            if not owner or not mint:
                continue
            amount_info = entry.get("uiTokenAmount") or entry.get("ui_token_amount") or {}
            try:
                amount = int(amount_info.get("amount"))
            except (TypeError, ValueError):
                continue
            decimals = int(amount_info.get("decimals") or 0)
            slot = token_deltas.setdefault(
                (owner, mint), {"amount": 0, "decimals": decimals}
            )
            slot["amount"] += sign * amount
            slot["decimals"] = decimals

    fold(meta.get("preTokenBalances") or meta.get("pre_token_balances"), -1)
    fold(meta.get("postTokenBalances") or meta.get("post_token_balances"), 1)

    by_owner: dict[str, list[dict[str, Any]]] = {}
    for (owner, mint), info in token_deltas.items():
        if info["amount"] == 0:
            continue
        by_owner.setdefault(owner, []).append(
            {
                "userAccount": owner,
                "mint": mint,
                "rawTokenAmount": {
                    "tokenAmount": str(info["amount"]),
                    "decimals": info["decimals"],
                },
            }
        )

    account_data: list[dict[str, Any]] = []
    for index, account in enumerate(keys):
        native_change = 0
        if index < len(pre_lamports) and index < len(post_lamports):
            native_change = int(post_lamports[index]) - int(pre_lamports[index])
        changes = by_owner.pop(account, [])
        if not native_change and not changes:
            continue
        account_data.append(
            {
                "account": account,
                "nativeBalanceChange": native_change,
                "tokenBalanceChanges": changes,
            }
        )
    # Owners that never appear in accountKeys (they signed nothing, but their
    # token account moved) still need an entry.
    for account, changes in by_owner.items():
        account_data.append(
            {"account": account, "nativeBalanceChange": 0, "tokenBalanceChanges": changes}
        )

    return {
        "signature": signature,
        "timestamp": int(_first(raw, "blockTime", "block_time", "timestamp", default=0) or 0),
        "slot": int(_first(raw, "slot", default=0) or 0),
        "fee": fee,
        "feePayer": fee_payer,
        "type": "SWAP",
        "source": _label_for(raw, keys),
        "accountData": account_data,
        "tokenTransfers": [],
        "nativeTransfers": [],
        "events": {},
    }


def adapt_parsed_event(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Convert a Parsed Events API record into the enhanced shape.

    The beta endpoint is not reachable from this build, so the adapter is
    written to be shape-tolerant rather than to one exact schema: a record that
    already carries an Enhanced-style `events.swap` or `tokenTransfers` is
    passed through, a raw-transaction envelope is converted, and a flat
    swap record is rebuilt into `events.swap`. Anything it cannot read returns
    None and is skipped rather than silently mis-parsed.
    """
    if not isinstance(event, dict):
        return None

    # Some envelopes wrap the payload.
    inner = event.get("transaction") if isinstance(event.get("transaction"), dict) else None
    if inner is not None and (inner.get("message") or inner.get("signatures")):
        adapted = adapt_raw_transaction(event)
        if adapted:
            return adapted

    if looks_enhanced(event):
        return event

    signature = _first(event, "signature", "txHash", "tx_hash", "transactionSignature", default="")
    timestamp = _first(event, "timestamp", "blockTime", "block_time", "blockUnixTime", default=0)
    slot = _first(event, "slot", "blockNumber", "block_number", default=0)
    if not signature or not timestamp:
        return None

    swap = event.get("swap") if isinstance(event.get("swap"), dict) else None
    if swap is None and isinstance(event.get("events"), dict):
        swap = event["events"].get("swap")
    if swap is None and str(_first(event, "type", "eventType", default="")).upper() in (
        "SWAP",
        "TOKEN_SWAP",
    ):
        swap = event

    if not isinstance(swap, dict):
        return None

    normalised_swap = {
        "nativeInput": _first(swap, "nativeInput", "native_input"),
        "nativeOutput": _first(swap, "nativeOutput", "native_output"),
        "tokenInputs": _first(swap, "tokenInputs", "token_inputs", default=[]) or [],
        "tokenOutputs": _first(swap, "tokenOutputs", "token_outputs", default=[]) or [],
        "innerSwaps": _first(swap, "innerSwaps", "inner_swaps", default=[]) or [],
    }
    if not any(normalised_swap.values()):
        return None

    return {
        "signature": str(signature),
        "timestamp": int(timestamp),
        "slot": int(slot or 0),
        "fee": int(_first(event, "fee", default=0) or 0),
        "feePayer": str(
            _first(event, "feePayer", "fee_payer", "user", "owner", "account", default="")
        ),
        "type": "SWAP",
        "source": str(_first(event, "source", "dex", "protocol", default="")).upper(),
        "accountData": event.get("accountData") or [],
        "tokenTransfers": event.get("tokenTransfers") or [],
        "nativeTransfers": event.get("nativeTransfers") or [],
        "events": {"swap": normalised_swap},
    }


def adapt_transaction(tx: Any) -> Optional[dict[str, Any]]:
    """Adapt whatever a bulk-history endpoint returned, whichever shape it used."""
    if not isinstance(tx, dict):
        return None
    if looks_enhanced(tx):
        return tx
    if tx.get("meta") or tx.get("transaction"):
        return adapt_raw_transaction(tx)
    return adapt_parsed_event(tx)


def unwrap_list(payload: Any, *keys: str) -> list[Any]:
    """Pull the record list out of whatever envelope a provider used."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in (*keys, "data", "items", "result", "events", "transactions", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("data", "items", "transactions", "events"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    return []


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------


@dataclass
class HistoryPage:
    transactions: list[dict[str, Any]] = field(default_factory=list)
    #: Signature to pass as `before` for the next page; None ends the walk.
    cursor: Optional[str] = None
    #: Records the endpoint returned before adaptation, for logging.
    raw_count: int = 0


class HistoryStrategy:
    """One way of fetching an address's transaction history."""

    name: str = "base"
    cost_kind: str = "enhanced_tx"

    def __init__(self, client: Any, settings: Settings):
        self.client = client
        self.settings = settings

    @property
    def page_size(self) -> int:
        return self.settings.helius_history_page_size

    def fetch(
        self,
        address: str,
        *,
        before: Optional[str],
        limit: int,
        tx_type: Optional[str],
    ) -> HistoryPage:  # pragma: no cover - interface
        raise NotImplementedError


class ParsedEventsStrategy(HistoryStrategy):
    """Helius Parsed Events API (open beta). The cheapest path when enabled."""

    name = STRATEGY_PARSED_EVENTS
    cost_kind = "parsed_events"

    def fetch(self, address, *, before, limit, tx_type):
        body: dict[str, Any] = {
            "address": address,
            "limit": min(limit, self.page_size),
        }
        if tx_type:
            body["types"] = [tx_type]
        if before:
            body["before"] = before

        try:
            payload = self.client.http.post(
                self.settings.helius_parsed_events_path,
                params={"api-key": self.client.api_key},
                json_body=body,
                cost_kind=self.cost_kind,
            )
        except ApiError as exc:
            if _is_unavailable(exc):
                raise StrategyUnavailable(str(exc)) from exc
            raise

        if isinstance(payload, dict) and payload.get("error"):
            if _rpc_error_is_unavailable(payload["error"]):
                raise StrategyUnavailable(str(payload["error"]))
            raise ApiError(f"helius parsed-events error for {address}: {payload['error']}")

        records = unwrap_list(payload)
        adapted = [tx for tx in (adapt_parsed_event(record) for record in records) if tx]
        if records and not adapted:
            # The endpoint answered but in a shape this adapter cannot read.
            # Treat that as unavailable rather than silently ingesting nothing.
            raise StrategyUnavailable(
                "parsed-events returned records this build could not adapt "
                f"(first keys: {sorted(list(records[0].keys()))[:8] if isinstance(records[0], dict) else type(records[0]).__name__})"
            )
        cursor = _page_cursor(records, adapted)
        return HistoryPage(adapted, cursor, len(records))


class BulkHistoryStrategy(HistoryStrategy):
    """Paginated bulk transaction history over RPC (~10 credits per page).

    Uses the configured bulk method (`getTransactionsForAddress` by default),
    which walks the same signature range the Enhanced endpoint does but returns
    transactions at the ordinary heavy-RPC rate instead of the parsed rate.
    """

    name = STRATEGY_BULK_HISTORY
    cost_kind = "bulk_history"

    def fetch(self, address, *, before, limit, tx_type):
        options: dict[str, Any] = {
            "limit": min(limit, self.page_size),
            "commitment": "confirmed",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
        }
        if before:
            options["before"] = before
        if tx_type and self.settings.helius_bulk_history_filter:
            # Only sent when the operator says the method supports it; an
            # unknown field is rejected by some RPC implementations.
            options[self.settings.helius_bulk_history_filter] = tx_type

        method = self.settings.helius_bulk_history_method
        try:
            result = self.client.rpc(method, [address, options], cost_kind=self.cost_kind)
        except ApiError as exc:
            if _is_unavailable(exc) or _rpc_error_is_unavailable(str(exc)):
                raise StrategyUnavailable(str(exc)) from exc
            raise

        records = unwrap_list(result)
        adapted = [tx for tx in (adapt_transaction(record) for record in records) if tx]
        cursor = _page_cursor(records, adapted)
        return HistoryPage(adapted, cursor, len(records))


class EnhancedTransactionsStrategy(HistoryStrategy):
    """The original 100-credit Enhanced Transactions API. Last resort."""

    name = STRATEGY_ENHANCED_TX
    cost_kind = "enhanced_tx"

    def fetch(self, address, *, before, limit, tx_type):
        page = self.client.address_transactions(
            address, before=before, limit=min(limit, self.page_size), tx_type=tx_type
        )
        cursor = page[-1].get("signature") if page else None
        return HistoryPage(list(page), cursor, len(page))


def _page_cursor(records: Sequence[Any], adapted: Sequence[dict[str, Any]]) -> Optional[str]:
    """Signature of the oldest record on the page, for `before` pagination.

    Taken from the raw records, not the adapted ones: a page whose last entries
    were skipped (failed transactions, unreadable records) must still advance
    the cursor past them, or the walk would loop on the same page forever.
    """
    for record in reversed(list(records)):
        if isinstance(record, dict):
            signature = _first(
                record, "signature", "txHash", "tx_hash", "transactionSignature", default=""
            )
            if not signature:
                signatures = (record.get("transaction") or {}).get("signatures") or []
                signature = signatures[0] if signatures else ""
            if signature:
                return str(signature)
    for tx in reversed(list(adapted)):
        if tx.get("signature"):
            return str(tx["signature"])
    return None


STRATEGY_CLASSES: dict[str, type[HistoryStrategy]] = {
    STRATEGY_PARSED_EVENTS: ParsedEventsStrategy,
    STRATEGY_BULK_HISTORY: BulkHistoryStrategy,
    STRATEGY_ENHANCED_TX: EnhancedTransactionsStrategy,
}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def planned_strategies(settings: Settings) -> tuple[str, ...]:
    """Strategies this configuration would try, in order."""
    configured = (settings.helius_history_strategy or "auto").lower()
    if configured == "auto":
        return STRATEGY_ORDER
    if configured not in STRATEGY_CLASSES:
        return STRATEGY_ORDER
    # An explicit choice still falls back if the endpoint is not enabled —
    # aborting a paid run because a beta endpoint went away helps nobody.
    rest = tuple(name for name in STRATEGY_ORDER if name != configured)
    return (configured, *rest)


class HeliusHistory:
    """Runs the strategy chain and remembers what actually worked."""

    def __init__(self, client: Any, settings: Settings):
        self.client = client
        self.settings = settings
        self.plan = planned_strategies(settings)
        self.resolved: Optional[str] = None
        self.unavailable: dict[str, str] = {}

    def _candidates(self) -> list[HistoryStrategy]:
        if self.resolved:
            names = [self.resolved]
        else:
            names = [name for name in self.plan if name not in self.unavailable]
        return [STRATEGY_CLASSES[name](self.client, self.settings) for name in names]

    def iter_transactions(
        self,
        address: str,
        *,
        max_txs: int,
        tx_type: Optional[str] = "SWAP",
        since_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield adapted transactions, newest first, across strategies."""
        candidates = self._candidates()
        if not candidates:
            log.error(
                "helius.history.no_strategy",
                extra={"ctx": {"address": address, "unavailable": ",".join(self.unavailable)}},
            )
            return

        for strategy in candidates:
            seen = 0
            before: Optional[str] = None
            started = True
            # Guards against an endpoint whose `before` cursor is inclusive, or
            # that repeats a page: the DB would dedupe, but re-yielding costs
            # parse time and muddies the ingest counters.
            seen_signatures: set[str] = set()
            try:
                while seen < max_txs:
                    page = strategy.fetch(
                        address,
                        before=before,
                        limit=max_txs - seen,
                        tx_type=tx_type,
                    )
                    if started:
                        # The first page proved the endpoint works; lock it in
                        # so a later failure cannot restart the walk elsewhere
                        # and pay for the same pages twice.
                        self._adopt(strategy)
                        started = False
                    if not page.transactions and not page.cursor:
                        return
                    for tx in page.transactions:
                        signature = str(tx.get("signature") or "")
                        if signature and signature in seen_signatures:
                            continue
                        if signature:
                            seen_signatures.add(signature)
                        ts = int(tx.get("timestamp") or 0)
                        if until_ts is not None and ts > until_ts:
                            continue
                        if since_ts is not None and ts and ts < since_ts:
                            log.debug(
                                "helius.window_exhausted",
                                extra={"ctx": {"address": address, "ts": ts, "since": since_ts}},
                            )
                            return
                        seen += 1
                        yield tx
                        if seen >= max_txs:
                            return
                    if not page.cursor or page.cursor == before:
                        return
                    before = page.cursor
                return
            except StrategyUnavailable as exc:
                if not started:
                    # Already committed to this strategy: a mid-walk failure is
                    # an error, not a reason to re-walk on another endpoint.
                    log.error(
                        "helius.history.failed_mid_walk",
                        extra={"ctx": {"strategy": strategy.name, "error": str(exc)[:200]}},
                    )
                    raise
                self.unavailable[strategy.name] = str(exc)[:200]
                log.warning(
                    "helius.history.unavailable",
                    extra={
                        "ctx": {
                            "strategy": strategy.name,
                            "cost_kind": strategy.cost_kind,
                            "reason": str(exc)[:200],
                            "next": next(
                                (
                                    name
                                    for name in self.plan
                                    if name not in self.unavailable
                                ),
                                "(none)",
                            ),
                        }
                    },
                )
                continue

    def _adopt(self, strategy: HistoryStrategy) -> None:
        if self.resolved == strategy.name:
            return
        self.resolved = strategy.name
        credits = self.settings.credit_costs().for_call(kind=strategy.cost_kind)
        log.info(
            "helius.history.strategy",
            extra={
                "ctx": {
                    "strategy": strategy.name,
                    "credits_per_page": credits,
                    "page_size": strategy.page_size,
                    "skipped": ",".join(self.unavailable) or "(none)",
                }
            },
        )
