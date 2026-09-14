"""Synthetic dataset generator.

Lets you exercise the whole pipeline — ingest -> P&L -> scoring -> pattern
detection -> backtest — before spending a single API credit, and gives the
tests a universe whose right answers are known by construction.

The generated universe deliberately contains one of each thing the scorer is
supposed to separate:

* `consistent-*`  — modest, repeatable wins across many tokens (should rank top)
* `oneshot-*`     — flat-to-losing except a single monster (should be flagged
                    `single_outlier` and pushed down)
* `sniper-*`      — buys in the launch block every time and always wins
                    (should be flagged and penalised)
* `sybil-*`       — four wallets entering the same tokens seconds apart
                    (should land in one cluster)
* `loser-*`       — negative expectancy (should rank bottom)
* `noise-*`       — background tape so fills have prices to hit
"""

from __future__ import annotations

import math
import random
import sqlite3
import time
from typing import Any, Optional

from .config import WSOL_MINT
from .db import insert_trades, upsert_token
from .logging_setup import get_logger
from .models import Trade

log = get_logger(__name__)

SOL_PRICE_USD = 150.0
SLOTS_PER_SECOND = 2.5


def slot_of(ts: float) -> int:
    """Deterministic slot number for a timestamp (~2.5 slots/second)."""
    return int(ts * SLOTS_PER_SECOND)


def _b58ish(rng: random.Random, prefix: str) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    tail = "".join(rng.choice(alphabet) for _ in range(44 - len(prefix)))
    return f"{prefix}{tail}"


class _PricePath:
    """A memecoin's life: vertical launch, a peak, then a long bleed."""

    def __init__(self, rng: random.Random, launch_ts: int):
        self.launch_ts = launch_ts
        self.p0 = rng.uniform(2e-7, 4e-6)
        self.peak_mult = rng.uniform(4.0, 90.0)
        self.time_to_peak = rng.uniform(1_800, 5 * 3_600)
        self.decay_floor = rng.uniform(0.04, 0.45)
        self.decay_tau = rng.uniform(3 * 3_600, 24 * 3_600)
        self.noise = rng.uniform(0.01, 0.05)
        self._rng = rng

    def __call__(self, ts: float) -> float:
        elapsed = max(0.0, ts - self.launch_ts)
        if elapsed <= self.time_to_peak:
            # Smooth ramp to the peak (ease-out, so early entries win most).
            progress = elapsed / self.time_to_peak
            mult = 1.0 + (self.peak_mult - 1.0) * (progress**0.6)
        else:
            decayed = math.exp(-(elapsed - self.time_to_peak) / self.decay_tau)
            mult = self.peak_mult * (self.decay_floor + (1 - self.decay_floor) * decayed)
        jitter = 1.0 + self._rng.gauss(0.0, self.noise)
        return max(1e-12, self.p0 * mult * jitter)


def _trade(
    rng: random.Random,
    wallet: str,
    mint: str,
    side: str,
    usd: float,
    price: float,
    ts: int,
    *,
    slot: Optional[int] = None,
) -> Trade:
    tokens = usd / price if price > 0 else 0.0
    return Trade(
        signature=_b58ish(rng, "sig"),
        wallet=wallet,
        mint=mint,
        side=side,
        token_amount=tokens,
        quote_mint=WSOL_MINT,
        quote_amount=usd / SOL_PRICE_USD,
        price_usd=price,
        value_usd=usd,
        ts=int(ts),
        slot=slot if slot is not None else slot_of(ts),
        dex=rng.choice(["RAYDIUM", "PUMP_FUN", "ORCA", "METEORA"]),
        source="demo",
    )


def seed_demo(
    conn: sqlite3.Connection,
    *,
    tokens: int = 10,
    seed: int = 7,
    days: int = 45,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Populate `trades` and `tokens` with a synthetic but coherent universe."""
    rng = random.Random(seed)
    now = int(now_ts if now_ts is not None else time.time())
    window_start = now - days * 86_400

    mints: list[str] = []
    paths: dict[str, _PricePath] = {}
    launches: dict[str, int] = {}
    for index in range(tokens):
        mint = _b58ish(rng, f"Mint{index:02d}")
        launch = rng.randint(window_start, now - 3 * 86_400)
        mints.append(mint)
        launches[mint] = launch
        paths[mint] = _PricePath(rng, launch)
        upsert_token(
            conn,
            {
                "mint": mint,
                "symbol": f"DEMO{index:02d}",
                "name": f"Demo Token {index:02d}",
                "decimals": 6,
                "launch_ts": launch,
                "launch_slot": slot_of(launch),
            },
        )

    batch: list[Trade] = []

    # --- the launch trade itself: defines each token's launch block --------
    for mint in mints:
        launch = launches[mint]
        batch.append(
            _trade(
                rng, _b58ish(rng, "deploy"), mint, "buy", rng.uniform(200, 2_000),
                paths[mint](launch), launch, slot=slot_of(launch),
            )
        )

    # --- background tape ---------------------------------------------------
    noise_wallets = [_b58ish(rng, f"noise{i:02d}") for i in range(12)]
    for mint in mints:
        launch = launches[mint]
        ts = launch
        end = min(now, launch + 6 * 86_400)
        while ts < end:
            ts += rng.randint(4, 25)
            price = paths[mint](ts)
            batch.append(
                _trade(
                    rng,
                    rng.choice(noise_wallets),
                    mint,
                    rng.choice(["buy", "sell"]),
                    rng.uniform(20, 900),
                    price,
                    ts,
                )
            )

    def round_trip(
        wallet: str, mint: str, entry_offset: float, exit_offset: float, usd: float,
        *, entry_slot: Optional[int] = None,
    ) -> None:
        launch = launches[mint]
        entry_ts = int(launch + entry_offset)
        exit_ts = int(launch + exit_offset)
        if exit_ts <= entry_ts:
            exit_ts = entry_ts + 60
        entry_price = paths[mint](entry_ts)
        exit_price = paths[mint](exit_ts)
        buy = _trade(rng, wallet, mint, "buy", usd, entry_price, entry_ts, slot=entry_slot)
        batch.append(buy)
        batch.append(
            _trade(rng, wallet, mint, "sell", buy.token_amount * exit_price, exit_price, exit_ts)
        )

    # --- archetype: consistent, repeatable performer -----------------------
    consistent = [_b58ish(rng, f"consist{i}") for i in range(2)]
    for wallet in consistent:
        for mint in mints:
            if rng.random() < 0.15:
                continue  # sits some out
            peak = paths[mint].time_to_peak
            entry = rng.uniform(0.15, 0.5) * peak
            if rng.random() < 0.3:  # a third of trades go wrong
                exit_at = peak * rng.uniform(3.0, 6.0)
            else:
                exit_at = peak * rng.uniform(0.75, 1.1)
            round_trip(wallet, mint, entry, exit_at, rng.uniform(300, 900))

    # --- archetype: one-hit wonder -----------------------------------------
    oneshot = _b58ish(rng, "oneshot")
    jackpot = mints[0]
    for mint in mints:
        peak = paths[mint].time_to_peak
        if mint == jackpot:
            round_trip(oneshot, mint, peak * 0.02, peak * 1.0, 4_000)
        else:
            round_trip(oneshot, mint, peak * 0.9, peak * 3.5, rng.uniform(200, 500))

    # --- archetype: launch-block sniper ------------------------------------
    sniper = _b58ish(rng, "sniper")
    for mint in mints:
        launch = launches[mint]
        round_trip(
            sniper, mint, 0, paths[mint].time_to_peak * rng.uniform(0.8, 1.0),
            rng.uniform(500, 1_500), entry_slot=slot_of(launch),
        )

    # --- archetype: sybil cluster ------------------------------------------
    sybils = [_b58ish(rng, f"sybil{i}") for i in range(4)]
    for mint in mints[:6]:
        peak = paths[mint].time_to_peak
        base_entry = rng.uniform(0.2, 0.4) * peak
        base_exit = peak * rng.uniform(0.8, 1.05)
        for offset, wallet in enumerate(sybils):
            round_trip(
                wallet, mint, base_entry + offset * rng.uniform(1, 8),
                base_exit + offset * rng.uniform(1, 10), rng.uniform(250, 600),
            )

    # --- archetype: consistent loser ---------------------------------------
    loser = _b58ish(rng, "loser")
    for mint in mints:
        peak = paths[mint].time_to_peak
        round_trip(loser, mint, peak * rng.uniform(1.0, 1.4), peak * rng.uniform(2.5, 5.0),
                   rng.uniform(150, 600))

    written = insert_trades(conn, [t.as_row() for t in batch])
    conn.commit()

    for mint in mints:
        row = conn.execute(
            "SELECT COUNT(*) AS trades, COUNT(DISTINCT wallet) AS wallets, "
            "MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM trades WHERE mint = ?",
            (mint,),
        ).fetchone()
        upsert_token(
            conn,
            {
                "mint": mint,
                "trade_count": row["trades"],
                "wallet_count": row["wallets"],
                "first_trade_ts": row["first_ts"],
                "last_trade_ts": row["last_ts"],
            },
        )
    conn.commit()

    summary = {
        "tokens": len(mints),
        "trades": written,
        "window_start": window_start,
        "window_end": now,
        "archetypes": {
            "consistent": consistent,
            "oneshot": [oneshot],
            "sniper": [sniper],
            "sybil": sybils,
            "loser": [loser],
        },
    }
    log.info(
        "demo.seeded",
        extra={"ctx": {"tokens": len(mints), "trades": written, "days": days}},
    )
    return summary
