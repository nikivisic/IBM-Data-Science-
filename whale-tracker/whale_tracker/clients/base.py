"""Shared HTTP plumbing: rate limiting, retries and an on-disk response cache.

Both providers meter by requests-per-second and answer 429 aggressively, so
every call goes through a token bucket, and every retry backs off exponentially
with jitter. Successful responses are cached in SQLite, which makes re-runs of
an ingest free in both time and API credits.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
from typing import Any, Mapping, Optional

import requests

from ..db import cache_get, cache_put
from ..logging_setup import get_logger

log = get_logger(__name__)

RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """A provider call failed in a way retries could not fix."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:500]


class RateLimiter:
    """Thread-safe token bucket smoothing calls to `rps` requests per second."""

    def __init__(self, rps: float, burst: Optional[float] = None):
        self.rps = max(float(rps), 0.01)
        self.capacity = float(burst if burst is not None else max(1.0, self.rps))
        # Start nearly empty: a full bucket would let the first second issue
        # `capacity` calls on top of the refill and trip the provider's limiter.
        self._tokens = min(self.capacity, 1.0)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rps
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rps
            time.sleep(sleep_for)
            waited += sleep_for


class HttpClient:
    """A small JSON-over-HTTP client with retry, rate limiting and caching."""

    def __init__(
        self,
        base_url: str,
        *,
        provider: str,
        rate_limit_rps: float = 5.0,
        timeout: float = 30.0,
        max_retries: int = 5,
        conn: Optional[sqlite3.Connection] = None,
        cache_ttl_seconds: int = 0,
        default_headers: Optional[Mapping[str, str]] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.limiter = RateLimiter(rate_limit_rps)
        self.timeout = timeout
        self.max_retries = max_retries
        self.conn = conn
        self.cache_ttl_seconds = cache_ttl_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "whale-tracker/0.1 (+phase1-research)"})
        if default_headers:
            self.session.headers.update(dict(default_headers))
        # Secrets seen by this client, so they can be scrubbed from anything we
        # log or raise. `requests` puts the full URL — query string included —
        # into its exception text, which would otherwise print the API key.
        self._secrets: set[str] = {
            str(value)
            for key, value in dict(default_headers or {}).items()
            if "key" in key.lower() and value
        }
        self.calls = 0
        self.cache_hits = 0

    def _scrub(self, text: str) -> str:
        """Replace any known secret in `text` with `***`."""
        for secret in self._secrets:
            if len(secret) >= 6:
                text = text.replace(secret, "***")
        return text

    # -- cache ------------------------------------------------------------
    def _cache_key(self, method: str, url: str, params: Mapping[str, Any], body: Any) -> str:
        # Secrets live in the URL/params for some providers; hash so the cache
        # key never stores an API key in the clear.
        blob = json.dumps(
            {"m": method, "u": url, "p": dict(sorted((params or {}).items())), "b": body},
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(blob.encode()).hexdigest()
        return f"{self.provider}:{digest}"

    # -- request ----------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        use_cache: bool = True,
        redact_params: tuple[str, ...] = ("api-key", "api_key"),
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        params = dict(params or {})
        self._secrets.update(
            str(params[key]) for key in redact_params if params.get(key)
        )
        cache_key = self._cache_key(method, url, params, json_body)

        if use_cache and self.conn is not None and self.cache_ttl_seconds > 0:
            cached = cache_get(self.conn, cache_key, self.cache_ttl_seconds)
            if cached is not None:
                self.cache_hits += 1
                log.debug(
                    "http.cache_hit",
                    extra={"ctx": {"provider": self.provider, "path": path}},
                )
                return cached

        safe_params = {
            k: ("***" if k in redact_params else v) for k, v in params.items()
        }
        last_error: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            self.limiter.acquire()
            self.calls += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params or None,
                    json=json_body,
                    headers=dict(headers or {}),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = self._scrub(f"{type(exc).__name__}: {exc}")
                log.warning(
                    "http.network_error",
                    extra={
                        "ctx": {
                            "provider": self.provider,
                            "path": path,
                            "attempt": attempt,
                            "error": last_error,
                        }
                    },
                )
                self._sleep_backoff(attempt)
                continue

            if response.status_code in RETRY_STATUS:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_error = f"HTTP {response.status_code}"
                log.warning(
                    "http.retryable_status",
                    extra={
                        "ctx": {
                            "provider": self.provider,
                            "path": path,
                            "status": response.status_code,
                            "attempt": attempt,
                            "retry_after": retry_after,
                        }
                    },
                )
                if attempt == self.max_retries:
                    break
                self._sleep_backoff(attempt, floor=retry_after)
                continue

            if response.status_code in (401, 403):
                raise ApiError(
                    f"{self.provider} rejected the API key (HTTP {response.status_code}). "
                    "Check the key in your .env and its plan limits.",
                    status=response.status_code,
                    body=self._scrub(response.text),
                )

            if not response.ok:
                raise ApiError(
                    f"{self.provider} returned HTTP {response.status_code} for {path}",
                    status=response.status_code,
                    body=self._scrub(response.text),
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise ApiError(
                    f"{self.provider} returned non-JSON for {path}", body=self._scrub(response.text)
                ) from exc

            log.debug(
                "http.ok",
                extra={
                    "ctx": {
                        "provider": self.provider,
                        "path": path,
                        "params": json.dumps(safe_params, default=str),
                    }
                },
            )
            if use_cache and self.conn is not None and self.cache_ttl_seconds > 0:
                cache_put(self.conn, cache_key, payload)
            return payload

        raise ApiError(
            self._scrub(
                f"{self.provider} failed after {self.max_retries} attempts for {path}: {last_error}"
            )
        )

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def _sleep_backoff(self, attempt: int, floor: Optional[float] = None) -> None:
        delay = min(30.0, 2.0 ** (attempt - 1)) * (0.75 + random.random() * 0.5)
        if floor:
            delay = max(delay, floor)
        time.sleep(delay)

    def stats(self) -> dict[str, int]:
        return {"calls": self.calls, "cache_hits": self.cache_hits}


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
