from __future__ import annotations

from pathlib import Path

import pytest

from term_fix import config


@pytest.fixture(autouse=True)
def no_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "missing.toml")
    for name in (
        "TERM_FIX_PROVIDER",
        "TERM_FIX_MODEL",
        "TERM_FIX_BASE_URL",
        "TERM_FIX_LANG",
        "TERM_FIX_AUTO_YES",
        "TERM_FIX_MAX_ERROR_CHARS",
        "TERM_FIX_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = config.load_settings(cwd=Path("/tmp"))
    assert settings.provider == "auto"
    assert settings.lang == "zh"
    assert settings.max_error_chars == 6000
    assert settings.auto_yes is False
    assert settings.model_for("gemini") == "gemini-2.5-flash"


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_FIX_PROVIDER", "openai")
    monkeypatch.setenv("TERM_FIX_MODEL", "qwen2.5-coder")
    monkeypatch.setenv("TERM_FIX_LANG", "en")
    monkeypatch.setenv("TERM_FIX_MAX_ERROR_CHARS", "1200")
    monkeypatch.setenv("TERM_FIX_AUTO_YES", "true")
    settings = config.load_settings(cwd=Path("/tmp"))
    assert settings.provider == "openai"
    assert settings.model == "qwen2.5-coder"
    assert settings.lang == "en"
    assert settings.max_error_chars == 1200
    assert settings.auto_yes is True


def test_cli_flags_beat_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_FIX_MODEL", "from-env")
    settings = config.load_settings(model="from-cli", cwd=Path("/tmp"))
    assert settings.model == "from-cli"


def test_project_config_file_is_loaded(tmp_path: Path) -> None:
    (tmp_path / ".term-fix.toml").write_text(
        'provider = "openai"\nmodel = "deepseek-coder"\ntimeout = 12.5\n', encoding="utf-8"
    )
    settings = config.load_settings(cwd=tmp_path)
    assert settings.provider == "openai"
    assert settings.model == "deepseek-coder"
    assert settings.timeout == 12.5


def test_project_config_table_syntax(tmp_path: Path) -> None:
    (tmp_path / ".term-fix.toml").write_text(
        '[term-fix]\nlang = "en"\nauto_yes = true\n', encoding="utf-8"
    )
    settings = config.load_settings(cwd=tmp_path)
    assert settings.lang == "en"
    assert settings.auto_yes is True


def test_invalid_provider_falls_back_to_auto(tmp_path: Path) -> None:
    (tmp_path / ".term-fix.toml").write_text('provider = "nonsense"\n', encoding="utf-8")
    assert config.load_settings(cwd=tmp_path).provider == "auto"


def test_broken_config_file_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("this is not toml {{{", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", broken)
    settings = config.load_settings(cwd=Path("/tmp"))
    assert settings.lang == "zh"
