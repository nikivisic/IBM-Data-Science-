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


# ---------------------------------------------------------------------------
# HTTP doubles, shared by the client, price and budget tests
# ---------------------------------------------------------------------------


class FakeResponse:
    """Stands in for a `requests.Response`."""

    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """A `requests.Session` whose answers are scripted.

    `responses` may be a flat list (consumed in order) or a mapping of URL
    substring -> response/list of responses, which keeps route-specific tests
    readable when one client calls several endpoints.
    """

    def __init__(self, responses=None, routes=None, default=None):
        self.responses = list(responses or [])
        self.routes = {k: (v if isinstance(v, list) else [v]) for k, v in (routes or {}).items()}
        self.default = default
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        for fragment, queued in self.routes.items():
            if fragment in url:
                if len(queued) > 1:
                    return queued.pop(0)
                return queued[0]
        if self.responses:
            return self.responses.pop(0)
        if self.default is not None:
            return self.default
        raise AssertionError(f"unexpected request to {url}")

    def urls(self):
        return [call["url"] for call in self.calls]


@pytest.fixture()
def no_sleep(monkeypatch):
    """Make retry backoff instant."""
    monkeypatch.setattr("whale_tracker.clients.base.time.sleep", lambda _s: None)
