"""Configuration loading.

All secrets and tunables come from the environment (optionally via a `.env`
file). Nothing is hardcoded; a missing API key raises a clear error at the
point of use rather than producing a confusing 401 deep inside a client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv

# Well-known mints used as quote currencies on Solana DEXes.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

#: Mints treated as ~$1.00 when valuing the quote leg of a swap.
STABLE_MINTS = frozenset({USDC_MINT, USDT_MINT})
#: Mints accepted as the "quote" side of a memecoin trade.
QUOTE_MINTS = frozenset({WSOL_MINT, USDC_MINT, USDT_MINT})


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip()


def _env_int(key: str, default: int) -> int:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of runtime configuration."""

    helius_api_key: str = ""
    birdeye_api_key: str = ""

    helius_base_url: str = "https://api.helius.xyz"
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    birdeye_base_url: str = "https://public-api.birdeye.so"

    db_path: Path = field(default_factory=lambda: Path("data/whale_tracker.db"))

    log_level: str = "INFO"
    log_format: str = "json"
    log_file: Optional[Path] = None

    helius_rate_limit_rps: float = 8.0
    birdeye_rate_limit_rps: float = 1.0
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 5
    http_cache_ttl_seconds: int = 86_400

    max_txs_per_token: int = 5_000
    min_trade_usd: float = 10.0

    def require_helius_key(self) -> str:
        if not self.helius_api_key:
            raise ConfigError(
                "HELIUS_API_KEY is not set. Copy .env.example to .env and add your key "
                "(get one at https://dashboard.helius.dev)."
            )
        return self.helius_api_key

    def require_birdeye_key(self) -> str:
        if not self.birdeye_api_key:
            raise ConfigError(
                "BIRDEYE_API_KEY is not set. Copy .env.example to .env and add your key "
                "(get one at https://bds.birdeye.so)."
            )
        return self.birdeye_api_key


def load_settings(env_file: Optional[str | Path] = None, *, override: bool = False) -> Settings:
    """Load settings from the environment, seeding it from a `.env` file first.

    Args:
        env_file: Explicit path to a dotenv file. When omitted, the nearest
            `.env` found by walking up from the current working directory is
            used, if one exists.
        override: Whether dotenv values beat already-exported environment vars.
    """
    if env_file is not None:
        path = Path(env_file)
        if not path.exists():
            raise ConfigError(f"env file not found: {path}")
        load_dotenv(path, override=override)
    else:
        # Anchor the search at the working directory. python-dotenv's default
        # starts from the *calling module's* directory, which for an installed
        # console script is site-packages — so the user's own .env is missed.
        discovered = find_dotenv(usecwd=True)
        if discovered:
            load_dotenv(discovered, override=override)

    log_file_raw = _env_str("LOG_FILE")

    return Settings(
        helius_api_key=_env_str("HELIUS_API_KEY"),
        birdeye_api_key=_env_str("BIRDEYE_API_KEY"),
        helius_base_url=_env_str("HELIUS_BASE_URL", "https://api.helius.xyz").rstrip("/"),
        helius_rpc_url=_env_str("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com").rstrip("/"),
        birdeye_base_url=_env_str("BIRDEYE_BASE_URL", "https://public-api.birdeye.so").rstrip("/"),
        db_path=Path(_env_str("WHALE_DB_PATH", "data/whale_tracker.db")),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        log_format=_env_str("LOG_FORMAT", "json").lower(),
        log_file=Path(log_file_raw) if log_file_raw else None,
        helius_rate_limit_rps=_env_float("HELIUS_RATE_LIMIT_RPS", 8.0),
        birdeye_rate_limit_rps=_env_float("BIRDEYE_RATE_LIMIT_RPS", 1.0),
        http_timeout_seconds=_env_float("HTTP_TIMEOUT_SECONDS", 30.0),
        http_max_retries=_env_int("HTTP_MAX_RETRIES", 5),
        http_cache_ttl_seconds=_env_int("HTTP_CACHE_TTL_SECONDS", 86_400),
        max_txs_per_token=_env_int("MAX_TXS_PER_TOKEN", 5_000),
        min_trade_usd=_env_float("MIN_TRADE_USD", 10.0),
    )
