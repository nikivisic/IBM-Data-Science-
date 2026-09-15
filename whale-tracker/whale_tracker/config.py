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

from .budget import CreditCosts, ProviderBudget

# Well-known mints used as quote currencies on Solana DEXes.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

#: Mints treated as ~$1.00 when valuing the quote leg of a swap.
STABLE_MINTS = frozenset({USDC_MINT, USDT_MINT})
#: Mints accepted as the "quote" side of a memecoin trade.
QUOTE_MINTS = frozenset({WSOL_MINT, USDC_MINT, USDT_MINT})

#: Keyless price sources, in the order they are tried by default.
DEFAULT_PRICE_SOURCES = ("jupiter", "dexscreener", "geckoterminal")
KNOWN_PRICE_SOURCES = frozenset({*DEFAULT_PRICE_SOURCES, "birdeye"})


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


def _env_bool(key: str, default: bool) -> bool:
    raw = _env_str(key).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}")


def _env_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env_str(key)
    if not raw:
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


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

    # --- fan-out caps (deliberately low; raise them knowingly) -------------
    max_wallets_per_token: int = 50
    max_txs_per_wallet: int = 200

    # --- budget caps -------------------------------------------------------
    budget_enforce: bool = True
    helius_max_requests_per_run: int = 5_000
    helius_max_requests_per_day: int = 50_000
    helius_max_credits_per_run: int = 100_000
    helius_max_credits_per_day: int = 250_000
    helius_monthly_credit_budget: int = 1_000_000
    birdeye_max_requests_per_run: int = 2_000
    birdeye_max_requests_per_day: int = 20_000
    #: Applied to each keyless price source separately, not to all of them together.
    price_max_requests_per_run: int = 1_000
    price_max_requests_per_day: int = 10_000

    # --- Helius credit costs ----------------------------------------------
    helius_credits_rpc: int = 1
    helius_credits_heavy_rpc: int = 10
    helius_credits_das: int = 10
    helius_credits_enhanced_tx: int = 100

    # --- price layer -------------------------------------------------------
    price_sources: tuple[str, ...] = DEFAULT_PRICE_SOURCES
    enable_birdeye: bool = False
    price_cache_ttl_seconds: int = 86_400
    spot_price_ttl_seconds: int = 300
    price_source_cooldown_seconds: int = 300
    jupiter_base_url: str = "https://api.jup.ag"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    geckoterminal_base_url: str = "https://api.geckoterminal.com"
    jupiter_rate_limit_rps: float = 2.0
    dexscreener_rate_limit_rps: float = 4.0
    geckoterminal_rate_limit_rps: float = 0.5

    def credit_costs(self) -> CreditCosts:
        return CreditCosts(
            rpc=self.helius_credits_rpc,
            heavy_rpc=self.helius_credits_heavy_rpc,
            das=self.helius_credits_das,
            enhanced_tx=self.helius_credits_enhanced_tx,
        )

    def provider_budgets(self) -> dict[str, ProviderBudget]:
        """Caps keyed by provider name, as the BudgetTracker expects them."""
        budgets = {
            "helius": ProviderBudget(
                "helius",
                max_requests_per_run=self.helius_max_requests_per_run,
                max_requests_per_day=self.helius_max_requests_per_day,
                max_credits_per_run=self.helius_max_credits_per_run,
                max_credits_per_day=self.helius_max_credits_per_day,
                monthly_credit_budget=self.helius_monthly_credit_budget,
            ),
            "birdeye": ProviderBudget(
                "birdeye",
                max_requests_per_run=self.birdeye_max_requests_per_run,
                max_requests_per_day=self.birdeye_max_requests_per_day,
            ),
        }
        for source in DEFAULT_PRICE_SOURCES:
            budgets[source] = ProviderBudget(
                source,
                max_requests_per_run=self.price_max_requests_per_run,
                max_requests_per_day=self.price_max_requests_per_day,
            )
        return budgets

    def active_price_sources(self) -> tuple[str, ...]:
        """Configured sources, with Birdeye appended only when enabled."""
        sources = tuple(s for s in self.price_sources if s != "birdeye")
        unknown = [s for s in sources if s not in KNOWN_PRICE_SOURCES]
        if unknown:
            raise ConfigError(
                f"unknown price source(s): {', '.join(unknown)}. "
                f"Known sources: {', '.join(sorted(KNOWN_PRICE_SOURCES))}"
            )
        if self.enable_birdeye and self.birdeye_api_key:
            sources = sources + ("birdeye",)
        return sources

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
        max_wallets_per_token=_env_int("MAX_WALLETS_PER_TOKEN", 50),
        max_txs_per_wallet=_env_int("MAX_TXS_PER_WALLET", 200),
        budget_enforce=_env_bool("BUDGET_ENFORCE", True),
        helius_max_requests_per_run=_env_int("HELIUS_MAX_REQUESTS_PER_RUN", 5_000),
        helius_max_requests_per_day=_env_int("HELIUS_MAX_REQUESTS_PER_DAY", 50_000),
        helius_max_credits_per_run=_env_int("HELIUS_MAX_CREDITS_PER_RUN", 100_000),
        helius_max_credits_per_day=_env_int("HELIUS_MAX_CREDITS_PER_DAY", 250_000),
        helius_monthly_credit_budget=_env_int("HELIUS_MONTHLY_CREDIT_BUDGET", 1_000_000),
        birdeye_max_requests_per_run=_env_int("BIRDEYE_MAX_REQUESTS_PER_RUN", 2_000),
        birdeye_max_requests_per_day=_env_int("BIRDEYE_MAX_REQUESTS_PER_DAY", 20_000),
        price_max_requests_per_run=_env_int("PRICE_MAX_REQUESTS_PER_RUN", 1_000),
        price_max_requests_per_day=_env_int("PRICE_MAX_REQUESTS_PER_DAY", 10_000),
        helius_credits_rpc=_env_int("HELIUS_CREDITS_RPC", 1),
        helius_credits_heavy_rpc=_env_int("HELIUS_CREDITS_HEAVY_RPC", 10),
        helius_credits_das=_env_int("HELIUS_CREDITS_DAS", 10),
        helius_credits_enhanced_tx=_env_int("HELIUS_CREDITS_ENHANCED_TX", 100),
        price_sources=_env_list("PRICE_SOURCES", DEFAULT_PRICE_SOURCES),
        enable_birdeye=_env_bool("ENABLE_BIRDEYE", False),
        price_cache_ttl_seconds=_env_int("PRICE_CACHE_TTL_SECONDS", 86_400),
        spot_price_ttl_seconds=_env_int("SPOT_PRICE_TTL_SECONDS", 300),
        price_source_cooldown_seconds=_env_int("PRICE_SOURCE_COOLDOWN_SECONDS", 300),
        jupiter_base_url=_env_str("JUPITER_BASE_URL", "https://api.jup.ag").rstrip("/"),
        dexscreener_base_url=_env_str(
            "DEXSCREENER_BASE_URL", "https://api.dexscreener.com"
        ).rstrip("/"),
        geckoterminal_base_url=_env_str(
            "GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com"
        ).rstrip("/"),
        jupiter_rate_limit_rps=_env_float("JUPITER_RATE_LIMIT_RPS", 2.0),
        dexscreener_rate_limit_rps=_env_float("DEXSCREENER_RATE_LIMIT_RPS", 4.0),
        geckoterminal_rate_limit_rps=_env_float("GECKOTERMINAL_RATE_LIMIT_RPS", 0.5),
    )
