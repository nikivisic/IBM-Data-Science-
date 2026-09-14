"""Configuration: keys come from the environment, never from the source."""

from __future__ import annotations

import pytest

from whale_tracker.config import ConfigError, load_settings

ENV_KEYS = [
    "HELIUS_API_KEY", "BIRDEYE_API_KEY", "WHALE_DB_PATH", "LOG_LEVEL", "LOG_FORMAT",
    "LOG_FILE", "HELIUS_RATE_LIMIT_RPS", "BIRDEYE_RATE_LIMIT_RPS", "HTTP_TIMEOUT_SECONDS",
    "HTTP_MAX_RETRIES", "HTTP_CACHE_TTL_SECONDS", "MAX_TXS_PER_TOKEN", "MIN_TRADE_USD",
    "HELIUS_BASE_URL", "BIRDEYE_BASE_URL", "HELIUS_RPC_URL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_apply_without_any_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.helius_api_key == ""
    assert settings.log_format == "json"
    assert settings.min_trade_usd == 10.0
    assert str(settings.db_path).endswith("whale_tracker.db")


def test_values_are_read_from_a_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text(
        "HELIUS_API_KEY=helius-key\nBIRDEYE_API_KEY=birdeye-key\n"
        "MIN_TRADE_USD=42.5\nLOG_FORMAT=console\nWHALE_DB_PATH=custom/path.db\n",
        encoding="utf-8",
    )
    settings = load_settings(env, override=True)
    assert settings.helius_api_key == "helius-key"
    assert settings.require_birdeye_key() == "birdeye-key"
    assert settings.min_trade_usd == 42.5
    assert settings.log_format == "console"
    assert str(settings.db_path) == "custom/path.db"


def test_missing_keys_fail_loudly_with_a_pointer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    with pytest.raises(ConfigError) as excinfo:
        settings.require_helius_key()
    assert "HELIUS_API_KEY" in str(excinfo.value) and ".env" in str(excinfo.value)
    with pytest.raises(ConfigError):
        settings.require_birdeye_key()


def test_a_missing_env_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.env")


def test_invalid_numbers_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HTTP_MAX_RETRIES", "banana")
    with pytest.raises(ConfigError):
        load_settings()


def test_no_api_key_is_hardcoded_anywhere():
    """Guard against a key being pasted into the source during debugging."""
    import re
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "whale_tracker"
    suspicious = re.compile(r"(api[_-]?key\s*=\s*['\"][A-Za-z0-9\-]{16,})", re.IGNORECASE)
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not suspicious.search(text), f"possible hardcoded key in {path}"


def test_dotenv_is_found_from_the_working_directory(tmp_path, monkeypatch):
    """Regression: an installed console script must still see the user's .env.

    python-dotenv's default search starts at the calling module's directory,
    which is site-packages once the package is installed — so discovery has to
    be anchored at the CWD instead.
    """
    project = tmp_path / "project"
    nested = project / "deeper"
    nested.mkdir(parents=True)
    (project / ".env").write_text("HELIUS_API_KEY=from-cwd\n", encoding="utf-8")

    monkeypatch.chdir(nested)  # walking up from the CWD must still find it
    assert load_settings(override=True).require_helius_key() == "from-cwd"
