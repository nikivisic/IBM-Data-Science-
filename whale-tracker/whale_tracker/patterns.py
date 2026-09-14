"""Bait, insider and sybil detection.

Three families of wallet look excellent on a P&L table and are useless — or
actively dangerous — to copy:

* **Launch-block snipers.** A wallet that is in the same block as the pool
  creation, token after token, either wrote the bot that did it or was told
  when to run. Even where that is entirely legitimate alpha, it is not
  *copyable*: by the time a copier sees the buy, 15-30 seconds of candles have
  already printed. Down-ranked on repeat offence, not on a single hit.
* **Abnormal win rates.** Memecoins are close to a coin flip. A wallet that is
  19-for-20 is either a bot with information, or the exit liquidity is you.
  Scored as a binomial tail probability against the population's own win rate,
  so the bar adapts to the dataset instead of to a guess.
* **Lockstep clusters.** Sets of wallets that enter the same tokens within
  seconds of each other are one actor split across many addresses (sybil
  farming, wash volume, or a bait set built to be followed). Copying any member
  means trading against the other members.

All three are *heuristics*, deliberately expressed as a continuous penalty
rather than a ban, and every contribution is recorded in `reasons` so a human
can audit why a wallet was pushed down the table.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .logging_setup import get_logger
from .models import WalletFlags

log = get_logger(__name__)


@dataclass
class PatternConfig:
    #: A buy within this many seconds of the token's first observed trade
    #: counts as a launch-window entry.
    launch_window_seconds: int = 30
    #: ...or within this many slots (~400ms each).
    launch_slot_window: int = 2
    #: Sniper rate below this is ignored; bots get penalised above it.
    sniper_rate_floor: float = 0.3
    #: Two wallets entering the same token within this many seconds are "together".
    lockstep_window_seconds: int = 30
    #: Minimum tokens two wallets must share before their timing means anything.
    min_shared_tokens: int = 3
    #: Fraction of shared tokens that must be co-timed to call it lockstep.
    lockstep_min_rate: float = 0.6
    #: Minimum closed positions before a win rate can be called abnormal.
    min_positions_for_insider: int = 5
    #: Population win rate for the binomial test; None = derive from the data.
    baseline_win_rate: Optional[float] = None
    #: Tokens with more participants than this are skipped for pair building
    #: (a mega-cap tape would otherwise dominate the O(n²) step).
    max_wallets_per_token: int = 5_000
    #: Penalty weights and overall cap.
    weight_sniper: float = 0.55
    weight_insider: float = 0.45
    weight_sybil: float = 0.55
    max_penalty: float = 0.90
    weights: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------


def binomial_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact; n here is at most a few hundred."""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    p = min(max(p, 1e-9), 1 - 1e-9)
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (p**i) * ((1 - p) ** (n - i))
    return min(1.0, max(0.0, total))


def _suspicion_from_p(p_value: float) -> float:
    """Map a p-value onto [0, 1]: 1e-3 -> 0.5, 1e-6 -> 1.0."""
    return max(0.0, min(1.0, -math.log10(max(p_value, 1e-12)) / 6.0))


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------


def detect_launch_snipers(
    positions: Iterable[Mapping[str, Any]],
    launches: Mapping[str, Mapping[str, Any]],
    cfg: PatternConfig,
) -> dict[str, dict[str, Any]]:
    """Per-wallet launch-entry statistics.

    `launches` maps mint -> {"launch_ts": int, "launch_slot": int}, normally the
    first trade we ever observed for that token.
    """
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tokens": 0, "block": 0, "window": 0, "lags": []}
    )
    for row in positions:
        first_ts = row.get("first_buy_ts")
        if not first_ts:
            continue
        launch = launches.get(row["mint"]) or {}
        launch_ts = launch.get("launch_ts")
        if not launch_ts:
            continue
        entry = stats[row["wallet"]]
        entry["tokens"] += 1
        lag_seconds = int(first_ts) - int(launch_ts)
        entry["lags"].append(lag_seconds)

        launch_slot = launch.get("launch_slot") or 0
        first_slot = int(row.get("first_buy_slot") or 0)
        slot_lag = first_slot - int(launch_slot) if first_slot and launch_slot else None

        if slot_lag == 0:
            entry["block"] += 1
            entry["window"] += 1
        elif (slot_lag is not None and 0 <= slot_lag <= cfg.launch_slot_window) or (
            0 <= lag_seconds <= cfg.launch_window_seconds
        ):
            entry["window"] += 1

    out: dict[str, dict[str, Any]] = {}
    for wallet, entry in stats.items():
        tokens = entry["tokens"] or 1
        out[wallet] = {
            "launch_block_buys": entry["block"],
            "launch_window_buys": entry["window"],
            "sniper_rate": entry["window"] / tokens,
            "median_entry_lag_seconds": statistics.median(entry["lags"]) if entry["lags"] else None,
            "tokens": entry["tokens"],
        }
    return out


def detect_insiders(
    wallet_outcomes: Mapping[str, tuple[int, int]],
    cfg: PatternConfig,
    baseline: Optional[float] = None,
) -> dict[str, dict[str, Any]]:
    """Flag win rates too good to be a coin flip.

    `wallet_outcomes` maps wallet -> (wins, closed_positions).
    """
    rates = [w / n for w, n in wallet_outcomes.values() if n >= cfg.min_positions_for_insider]
    if baseline is None:
        baseline = cfg.baseline_win_rate
    if baseline is None:
        baseline = statistics.fmean(rates) if rates else 0.5
    baseline = min(max(baseline, 0.2), 0.7)

    out: dict[str, dict[str, Any]] = {}
    for wallet, (wins, n) in wallet_outcomes.items():
        if n < cfg.min_positions_for_insider:
            continue
        p_value = binomial_sf(wins, n, baseline)
        suspicion = _suspicion_from_p(p_value)
        if wins == n and p_value < 0.05:
            # A spotless record is its own red flag — but only once it is long
            # enough to be unlikely on its own. Six-for-six against a 70%
            # baseline happens to one wallet in eight; that is not evidence.
            suspicion = max(suspicion, 0.6)
        out[wallet] = {
            "insider_p_value": p_value,
            "insider_suspicion": suspicion,
            "baseline_win_rate": baseline,
            "wins": wins,
            "n": n,
        }
    return out


def detect_lockstep_clusters(
    entries: Mapping[str, list[tuple[str, int]]],
    cfg: PatternConfig,
) -> dict[str, dict[str, Any]]:
    """Find wallets that repeatedly enter the same tokens within seconds.

    `entries` maps mint -> [(wallet, first_buy_ts), ...].

    Candidate pairs are formed with a sliding window over each token's
    entry times, so the expensive all-pairs comparison only ever runs on
    wallets that were actually close together.
    """
    co_timed: dict[tuple[str, str], int] = defaultdict(int)
    wallet_tokens: dict[str, set[str]] = defaultdict(set)

    for mint, rows in entries.items():
        if len(rows) > cfg.max_wallets_per_token:
            log.warning(
                "patterns.token_skipped_for_pairing",
                extra={"ctx": {"mint": mint, "wallets": len(rows)}},
            )
            for wallet, _ in rows:
                wallet_tokens[wallet].add(mint)
            continue
        ordered = sorted(rows, key=lambda kv: kv[1])
        for wallet, _ in ordered:
            wallet_tokens[wallet].add(mint)
        left = 0
        for right in range(len(ordered)):
            wallet_r, ts_r = ordered[right]
            while ts_r - ordered[left][1] > cfg.lockstep_window_seconds:
                left += 1
            for index in range(left, right):
                wallet_l, _ = ordered[index]
                if wallet_l == wallet_r:
                    continue
                key = (wallet_l, wallet_r) if wallet_l < wallet_r else (wallet_r, wallet_l)
                co_timed[key] += 1

    union = UnionFind()
    peers: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (a, b), hits in co_timed.items():
        if hits < cfg.min_shared_tokens:
            continue
        shared = len(wallet_tokens[a] & wallet_tokens[b])
        if shared < cfg.min_shared_tokens:
            continue
        rate = hits / shared
        if rate < cfg.lockstep_min_rate:
            continue
        union.union(a, b)
        peers[a].append((b, rate))
        peers[b].append((a, rate))

    clusters: dict[str, list[str]] = defaultdict(list)
    for wallet in peers:
        clusters[union.find(wallet)].append(wallet)

    out: dict[str, dict[str, Any]] = {}
    for index, (root, members) in enumerate(sorted(clusters.items()), start=1):
        if len(members) < 2:
            continue
        for wallet in members:
            rates = [rate for _, rate in peers[wallet]]
            out[wallet] = {
                "cluster_id": index,
                "cluster_size": len(members),
                "lockstep_score": max(rates) if rates else 0.0,
                "lockstep_peers": len(rates),
                "cluster_members": sorted(members)[:25],
            }
    return out


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _penalty_and_reasons(flags: WalletFlags, cfg: PatternConfig, detail: Mapping[str, Any]) -> None:
    reasons: list[str] = []
    penalty = 0.0

    excess = max(0.0, flags.sniper_rate - cfg.sniper_rate_floor) / max(
        1e-9, 1.0 - cfg.sniper_rate_floor
    )
    if excess > 0:
        contribution = cfg.weight_sniper * excess
        penalty += contribution
        reasons.append(
            f"launch-window entries on {flags.launch_window_buys}/{detail.get('sniper_tokens', 0)} "
            f"tokens ({flags.sniper_rate:.0%}, {flags.launch_block_buys} same-block) "
            f"— not copyable at realistic latency (-{contribution:.2f})"
        )

    if flags.insider_suspicion > 0.2:
        contribution = cfg.weight_insider * flags.insider_suspicion
        penalty += contribution
        reasons.append(
            f"win rate {detail.get('wins', 0)}/{detail.get('n', 0)} vs baseline "
            f"{detail.get('baseline_win_rate', 0):.0%} (p={flags.insider_p_value:.2g}) "
            f"— abnormally high (-{contribution:.2f})"
        )

    if flags.cluster_id is not None and flags.cluster_size > 1:
        size_factor = min(1.0, math.log1p(flags.cluster_size - 1) / math.log(5))
        contribution = cfg.weight_sybil * flags.lockstep_score * size_factor
        penalty += contribution
        reasons.append(
            f"trades in lockstep with {flags.lockstep_peers} wallet(s), cluster #{flags.cluster_id} "
            f"of {flags.cluster_size} (co-entry rate {flags.lockstep_score:.0%}) "
            f"— possible sybil set (-{contribution:.2f})"
        )

    flags.penalty = min(cfg.max_penalty, penalty)
    flags.reasons = reasons


def analyse_patterns(
    conn: sqlite3.Connection,
    *,
    cfg: Optional[PatternConfig] = None,
    persist: bool = True,
) -> dict[str, WalletFlags]:
    """Run all detectors over stored P&L and write `wallet_flags`."""
    from .db import upsert_flags

    cfg = cfg or PatternConfig()

    positions = [
        dict(row)
        for row in conn.execute(
            "SELECT wallet, mint, first_buy_ts, first_buy_slot, is_closed, roi, "
            "realised_pnl_usd, cost_usd FROM wallet_token_pnl"
        )
    ]
    if not positions:
        log.warning("patterns.no_positions")
        return {}

    launches = {
        row["mint"]: {"launch_ts": row["launch_ts"], "launch_slot": row["launch_slot"]}
        for row in conn.execute("SELECT mint, launch_ts, launch_slot FROM tokens")
    }
    # Tokens ingested only as a side effect of wallet expansion may have no row
    # in `tokens`; fall back to the earliest trade we hold.
    missing = {p["mint"] for p in positions} - set(launches)
    if missing:
        placeholders = ",".join("?" for _ in missing)
        for row in conn.execute(
            f"SELECT mint, MIN(ts) AS launch_ts, MIN(slot) AS launch_slot FROM trades "
            f"WHERE mint IN ({placeholders}) GROUP BY mint",
            list(missing),
        ):
            launches[row["mint"]] = {
                "launch_ts": row["launch_ts"],
                "launch_slot": row["launch_slot"],
            }

    snipers = detect_launch_snipers(positions, launches, cfg)

    outcomes: dict[str, tuple[int, int]] = {}
    tallies: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in positions:
        if not int(row.get("is_closed") or 0) or row.get("roi") is None:
            continue
        tally = tallies[row["wallet"]]
        tally[1] += 1
        if float(row.get("realised_pnl_usd") or 0.0) > 0:
            tally[0] += 1
    outcomes = {w: (t[0], t[1]) for w, t in tallies.items() if t[1] > 0}
    insiders = detect_insiders(outcomes, cfg)

    entries: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in positions:
        if row.get("first_buy_ts"):
            entries[row["mint"]].append((row["wallet"], int(row["first_buy_ts"])))
    clusters = detect_lockstep_clusters(entries, cfg)

    wallets = set(snipers) | set(insiders) | set(clusters) | {p["wallet"] for p in positions}
    results: dict[str, WalletFlags] = {}
    for wallet in wallets:
        sniper = snipers.get(wallet, {})
        insider = insiders.get(wallet, {})
        cluster = clusters.get(wallet, {})
        flags = WalletFlags(
            wallet=wallet,
            launch_block_buys=int(sniper.get("launch_block_buys", 0)),
            launch_window_buys=int(sniper.get("launch_window_buys", 0)),
            sniper_rate=float(sniper.get("sniper_rate", 0.0)),
            median_entry_lag_seconds=sniper.get("median_entry_lag_seconds"),
            insider_p_value=insider.get("insider_p_value"),
            insider_suspicion=float(insider.get("insider_suspicion", 0.0)),
            cluster_id=cluster.get("cluster_id"),
            cluster_size=int(cluster.get("cluster_size", 1)),
            lockstep_score=float(cluster.get("lockstep_score", 0.0)),
            lockstep_peers=int(cluster.get("lockstep_peers", 0)),
        )
        _penalty_and_reasons(
            flags,
            cfg,
            {
                "sniper_tokens": sniper.get("tokens", 0),
                "wins": insider.get("wins", 0),
                "n": insider.get("n", 0),
                "baseline_win_rate": insider.get("baseline_win_rate", 0.0),
            },
        )
        results[wallet] = flags

    if persist and results:
        upsert_flags(conn, [f.as_row() for f in results.values()])
        conn.commit()

    flagged = sum(1 for f in results.values() if f.penalty > 0)
    log.info(
        "patterns.done",
        extra={
            "ctx": {
                "wallets": len(results),
                "flagged": flagged,
                "clusters": len({f.cluster_id for f in results.values() if f.cluster_id}),
            }
        },
    )
    return results
