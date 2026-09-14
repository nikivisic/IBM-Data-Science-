from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whale_tracker.db import init_db  # noqa: E402
from whale_tracker.logging_setup import configure_logging  # noqa: E402

configure_logging("ERROR", "console")


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture()
def trade_factory():
    """Build trade rows without repeating every column at each call site."""
    counter = {"n": 0}

    def make(wallet, mint, side, token_amount, value_usd, ts, slot=None, signature=None):
        counter["n"] += 1
        return {
            "signature": signature or f"sig{counter['n']:05d}",
            "wallet": wallet,
            "mint": mint,
            "side": side,
            "token_amount": float(token_amount),
            "quote_mint": "So11111111111111111111111111111111111111112",
            "quote_amount": float(value_usd) / 150.0,
            "price_usd": float(value_usd) / float(token_amount),
            "value_usd": float(value_usd),
            "ts": int(ts),
            "slot": int(slot if slot is not None else ts * 2),
            "dex": "RAYDIUM",
            "source": "test",
        }

    return make
