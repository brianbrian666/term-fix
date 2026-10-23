"""Configuration: defaults < config file < environment < CLI flags."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]


HOME_DIR = Path.home() / ".term-fix"
CONFIG_PATH = Path(os.environ.get("TERM_FIX_CONFIG") or HOME_DIR / "config.toml")
HISTORY_PATH = Path(os.environ.get("TERM_FIX_HISTORY") or HOME_DIR / "history.jsonl")
LOCAL_CONFIG_NAME = ".term-fix.toml"

DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}

PROVIDERS = ("auto", "gemini", "openai")


@dataclass
class Settings:
    """Effective settings for a single run."""

    provider: str = "auto"
    model: str | None = None
    base_url: str | None = None
    lang: str = "zh"
    auto_yes: bool = False
    max_error_chars: int = 6000
    timeout: float = 60.0

    def model_for(self, provider: str) -> str:
        return self.model or DEFAULT_MODELS.get(provider, "")


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_config_file(path: Path) -> dict[str, Any]:
    """Read a TOML config file, returning {} when missing or unreadable."""
    if tomllib is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    # Accept both top-level keys and a [term-fix] table.
    for key in ("term-fix", "term_fix", "tool.term-fix"):
        table = data.get(key)
        if isinstance(table, dict):
            data = {**data, **table}
    return data


def load_settings(
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    lang: str | None = None,
    auto_yes: bool | None = None,
    max_error_chars: int | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
) -> Settings:
    """Merge every configuration source; explicit arguments win."""
    merged: dict[str, Any] = {}
    merged.update(read_config_file(CONFIG_PATH))
    merged.update(read_config_file(Path(cwd or Path.cwd()) / LOCAL_CONFIG_NAME))

    env_overrides = {
        "provider": _as_str(os.environ.get("TERM_FIX_PROVIDER")),
        "model": _as_str(os.environ.get("TERM_FIX_MODEL")),
        "base_url": _as_str(os.environ.get("TERM_FIX_BASE_URL")),
        "lang": _as_str(os.environ.get("TERM_FIX_LANG")),
        "auto_yes": _as_bool(os.environ.get("TERM_FIX_AUTO_YES")),
        "max_error_chars": _as_int(os.environ.get("TERM_FIX_MAX_ERROR_CHARS")),
        "timeout": _as_float(os.environ.get("TERM_FIX_TIMEOUT")),
    }
    for key, value in env_overrides.items():
        if value is not None:
            merged[key] = value

    cli_overrides = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "lang": lang,
        "auto_yes": auto_yes,
        "max_error_chars": max_error_chars,
        "timeout": timeout,
    }
    for key, value in cli_overrides.items():
        if value is not None:
            merged[key] = value

    settings = Settings()
    settings.provider = _as_str(merged.get("provider")) or settings.provider
    if settings.provider not in PROVIDERS:
        settings.provider = "auto"
    settings.model = _as_str(merged.get("model"))
    settings.base_url = _as_str(merged.get("base_url"))
    settings.lang = _as_str(merged.get("lang")) or settings.lang
    settings.auto_yes = _as_bool(merged.get("auto_yes")) or False
    settings.max_error_chars = _as_int(merged.get("max_error_chars")) or settings.max_error_chars
    settings.timeout = _as_float(merged.get("timeout")) or settings.timeout
    return settings
