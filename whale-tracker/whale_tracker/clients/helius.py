"""Helius client: parsed (enhanced) transaction history.

Helius is the primary discovery source. Asking it for an address's parsed
transaction history — where the address is the *mint* — yields every swap that
touched that token, already decoded into balance changes, which is what we need
to attribute trades to wallets.

Docs: https://docs.helius.dev/api-reference/enhanced-transactions-api
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator, Optional, Sequence

from ..budget import BudgetTracker
from ..config import QUOTE_MINTS, Settings, WSOL_MINT
from ..logging_setup import get_logger
from ..models import SwapLeg
from .base import ApiError, HttpClient

log = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


class HeliusClient:
    """Thin wrapper over the Helius enhanced-transactions and RPC endpoints."""

    def __init__(
        self,
        settings: Settings,
        conn: Optional[sqlite3.Connection] = None,
        budget: Optional[BudgetTracker] = None,
    ):
        self.settings = settings
        self.api_key = settings.require_helius_key()
        self.http = HttpClient(
            settings.helius_base_url,
            provider="helius",
            rate_limit_rps=settings.helius_rate_limit_rps,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            conn=conn,
            cache_ttl_seconds=settings.http_cache_ttl_seconds,
            budget=budget,
            # Most traffic from this client is the Enhanced Transactions API;
            # RPC calls override this per call with their own method name.
            cost_kind="enhanced_tx",
        )

    # -- raw endpoints ----------------------------------------------------
    def address_transactions(
        self,
        address: str,
        *,
        before: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        tx_type: Optional[str] = "SWAP",
    ) -> list[dict[str, Any]]:
        """One page of parsed transactions involving `address`, newest first."""
        params: dict[str, Any] = {"api-key": self.api_key, "limit": min(limit, 100)}
        if before:
            params["before"] = before
        if until:
            params["until"] = until
        if tx_type:
            params["type"] = tx_type
        payload = self.http.get(
            f"/v0/addresses/{address}/transactions", params=params, cost_kind="enhanced_tx"
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise ApiError(f"helius error for {address}: {payload['error']}")
        return payload if isinstance(payload, list) else []

    def iter_address_transactions(
        self,
        address: str,
        *,
        max_txs: int = 5_000,
        tx_type: Optional[str] = "SWAP",
        since_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
    ) -> Iterator[dict[str, Any]]:
        """Page backwards through an address's history, newest transaction first.

        Stops at `max_txs`, or as soon as a page falls entirely before
        `since_ts` (history is returned newest-first, so that is the end).
        """
        seen = 0
        before: Optional[str] = None
        while seen < max_txs:
            page = self.address_transactions(
                address, before=before, limit=min(100, max_txs - seen), tx_type=tx_type
            )
            if not page:
                return
            for tx in page:
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
            before = page[-1].get("signature")
            if not before:
                return

    def transactions_by_signature(self, signatures: Sequence[str]) -> list[dict[str, Any]]:
        """Parse up to 100 signatures at a time."""
        out: list[dict[str, Any]] = []
        sigs = list(signatures)
        for i in range(0, len(sigs), 100):
            chunk = sigs[i : i + 100]
            payload = self.http.post(
                "/v0/transactions",
                params={"api-key": self.api_key},
                json_body={"transactions": chunk},
                cost_kind="enhanced_tx",
            )
            if isinstance(payload, list):
                out.extend(payload)
        return out

    def rpc(self, method: str, params: Any) -> Any:
        payload = self.http.post(
            f"{self.settings.helius_rpc_url}/",
            params={"api-key": self.api_key},
            json_body={"jsonrpc": "2.0", "id": "whale-tracker", "method": method, "params": params},
            # DAS methods and getProgramAccounts are billed above plain RPC;
            # the cost table resolves that from the method name.
            cost_kind="rpc",
            cost_method=method,
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise ApiError(f"helius rpc {method} failed: {payload['error']}")
        return (payload or {}).get("result")

    def token_metadata(self, mint: str) -> dict[str, Any]:
        """Symbol/name/decimals via the DAS `getAsset` method (best effort)."""
        try:
            asset = self.rpc("getAsset", {"id": mint}) or {}
        except ApiError as exc:
            log.warning("helius.metadata_failed", extra={"ctx": {"mint": mint, "error": str(exc)}})
            return {}
        content = asset.get("content") or {}
        meta = content.get("metadata") or {}
        token_info = asset.get("token_info") or {}
        return {
            "symbol": meta.get("symbol") or token_info.get("symbol") or "",
            "name": meta.get("name") or "",
            "decimals": int(token_info.get("decimals") or 0),
        }

    def first_signature(self, address: str) -> Optional[dict[str, Any]]:
        """Oldest signature for an address — used to date a token's launch."""
        sigs = self.rpc("getSignaturesForAddress", [address, {"limit": 1000}]) or []
        if not sigs:
            return None
        return sigs[-1]

    def stats(self) -> dict[str, int]:
        return self.http.stats()


# ---------------------------------------------------------------------------
# Parsing: enhanced transaction -> SwapLeg
# ---------------------------------------------------------------------------


def _raw_amount(entry: dict[str, Any]) -> float:
    raw = entry.get("rawTokenAmount") or {}
    try:
        amount = float(raw.get("tokenAmount", 0))
    except (TypeError, ValueError):
        return 0.0
    decimals = int(raw.get("decimals") or 0)
    return amount / (10**decimals) if decimals else amount


def _wallet_deltas(tx: dict[str, Any], wallet: str) -> dict[str, float]:
    """Net per-mint balance change for `wallet`, in whole tokens.

    Prefers the decoded `events.swap` block (which excludes fees and rent), and
    falls back to raw account balance changes, then to token transfers.
    """
    deltas: dict[str, float] = {}

    def bump(mint: str, amount: float) -> None:
        if not mint or not amount:
            return
        deltas[mint] = deltas.get(mint, 0.0) + amount

    swap = ((tx.get("events") or {}).get("swap")) or {}
    swap_blocks: list[dict[str, Any]] = []
    if swap:
        swap_blocks.append(swap)
        # Aggregator routes (Jupiter et al.) put the real legs in innerSwaps.
        swap_blocks.extend(swap.get("innerSwaps") or [])

    for block in swap_blocks:
        native_in = block.get("nativeInput")
        native_out = block.get("nativeOutput")
        for native, sign in ((native_in, -1), (native_out, 1)):
            if not native:
                continue
            if isinstance(native, dict):
                account = native.get("account")
                amount = native.get("amount")
            else:  # some payloads carry a bare lamport figure
                account, amount = wallet, native
            if account and account != wallet:
                continue
            try:
                bump(WSOL_MINT, sign * float(amount or 0) / LAMPORTS_PER_SOL)
            except (TypeError, ValueError):
                continue
        for entry in block.get("tokenInputs") or []:
            if entry.get("userAccount") == wallet:
                bump(entry.get("mint", ""), -_raw_amount(entry))
        for entry in block.get("tokenOutputs") or []:
            if entry.get("userAccount") == wallet:
                bump(entry.get("mint", ""), _raw_amount(entry))

    if deltas:
        return deltas

    for account in tx.get("accountData") or []:
        for change in account.get("tokenBalanceChanges") or []:
            if change.get("userAccount") != wallet:
                continue
            bump(change.get("mint", ""), _raw_amount(change))
        if account.get("account") == wallet:
            lamports = float(account.get("nativeBalanceChange") or 0)
            fee = float(tx.get("fee") or 0) if tx.get("feePayer") == wallet else 0.0
            # Add the fee back: it is a cost of transacting, not part of the swap.
            bump(WSOL_MINT, (lamports + fee) / LAMPORTS_PER_SOL)

    if deltas:
        return deltas

    for transfer in tx.get("tokenTransfers") or []:
        amount = float(transfer.get("tokenAmount") or 0)
        if transfer.get("toUserAccount") == wallet:
            bump(transfer.get("mint", ""), amount)
        elif transfer.get("fromUserAccount") == wallet:
            bump(transfer.get("mint", ""), -amount)
    for transfer in tx.get("nativeTransfers") or []:
        amount = float(transfer.get("amount") or 0) / LAMPORTS_PER_SOL
        if transfer.get("toUserAccount") == wallet:
            bump(WSOL_MINT, amount)
        elif transfer.get("fromUserAccount") == wallet:
            bump(WSOL_MINT, -amount)

    return deltas


def candidate_wallets(tx: dict[str, Any]) -> list[str]:
    """Wallets that could be the trader in this transaction, fee payer first."""
    wallets: list[str] = []
    fee_payer = tx.get("feePayer")
    if fee_payer:
        wallets.append(fee_payer)
    for transfer in tx.get("tokenTransfers") or []:
        for key in ("fromUserAccount", "toUserAccount"):
            account = transfer.get(key)
            if account and account not in wallets:
                wallets.append(account)
    return wallets


def extract_legs(
    tx: dict[str, Any],
    *,
    mints: Optional[set[str]] = None,
    min_token_amount: float = 0.0,
) -> list[SwapLeg]:
    """Turn one parsed Helius transaction into zero or more swap legs.

    Only swaps where the counter-asset is SOL or a stablecoin are kept: a
    memecoin-for-memecoin rotation has no unambiguous USD basis, and pretending
    otherwise would corrupt the P&L.
    """
    signature = tx.get("signature") or ""
    ts = int(tx.get("timestamp") or 0)
    slot = int(tx.get("slot") or 0)
    dex = (tx.get("source") or "").upper()
    if not signature or not ts:
        return []

    legs: list[SwapLeg] = []
    for wallet in candidate_wallets(tx):
        deltas = _wallet_deltas(tx, wallet)
        if not deltas:
            continue

        quote_candidates = {m: d for m, d in deltas.items() if m in QUOTE_MINTS and abs(d) > 0}
        token_candidates = {
            m: d
            for m, d in deltas.items()
            if m not in QUOTE_MINTS and abs(d) > min_token_amount
        }
        if mints:
            token_candidates = {m: d for m, d in token_candidates.items() if m in mints}
        if not quote_candidates or not token_candidates:
            continue

        # The quote leg is the largest-magnitude quote-currency movement.
        quote_mint, quote_delta = max(quote_candidates.items(), key=lambda kv: abs(kv[1]))
        for mint, token_delta in token_candidates.items():
            if token_delta * quote_delta >= 0:
                # Both sides moved the same way: a deposit/withdrawal or an
                # LP action, not a trade.
                continue
            legs.append(
                SwapLeg(
                    signature=signature,
                    wallet=wallet,
                    mint=mint,
                    token_delta=token_delta,
                    quote_mint=quote_mint,
                    quote_delta=quote_delta,
                    ts=ts,
                    slot=slot,
                    dex=dex,
                    source="helius",
                )
            )
        if legs:
            # The fee payer (or first matching wallet) is the trader; other
            # accounts in the tx are pools and routers.
            break
    return legs
