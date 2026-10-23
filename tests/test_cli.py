from __future__ import annotations

from pathlib import Path

import pytest

from term_fix import cli, executor, history
from term_fix.config import Settings
from term_fix.context import EnvContext

ENV = EnvContext(os_name="Darwin 25.0.0", arch="arm64", shell="zsh", cwd="/tmp/project")


@pytest.fixture(autouse=True)
def isolated_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(history, "HISTORY_PATH", path)
    return path


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    recorded: dict[str, list] = {"executed": [], "analyzed": []}

    def fake_capture(argv, **_kwargs):
        return executor.CommandResult(argv=argv, returncode=1, duration=0.2, stderr="npm ERR! ERESOLVE")

    def fake_passthrough(argv, **_kwargs):
        recorded["executed"].append(argv)
        return 0

    monkeypatch.setattr(executor, "run_capture", fake_capture)
    monkeypatch.setattr(executor, "run_passthrough", fake_passthrough)
    monkeypatch.setattr(cli.context, "gather", lambda command: ENV)
    return recorded


def fake_diagnosis(monkeypatch: pytest.MonkeyPatch, fix: str, calls: dict[str, list]) -> None:
    def analyze(**kwargs):
        calls["analyzed"].append(kwargs)
        return cli.ai.Diagnosis(
            explanation="依赖树冲突。",
            fix_command=fix,
            confidence="high",
            provider="gemini",
            model="gemini-2.5-flash",
        )

    monkeypatch.setattr(cli.ai, "analyze_error", analyze)


def run(**overrides):
    options = {
        "assume_yes": False,
        "dry_run": False,
        "force": False,
        "capture_all": False,
        "quiet": True,
        "settings": Settings(lang="zh"),
    }
    options.update(overrides)
    return cli.run_flow(("npm", "install"), **options)


def test_successful_command_exits_zero_without_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executor,
        "run_capture",
        lambda argv, **_kwargs: executor.CommandResult(argv=argv, returncode=0, duration=0.1),
    )
    monkeypatch.setattr(
        cli.ai, "analyze_error", lambda **kwargs: pytest.fail("the model must not be called")
    )
    assert run() == 0


def test_safe_fix_runs_with_yes(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "rm -rf node_modules && npm install", calls)
    code = run(assume_yes=True)
    assert code == 0
    assert calls["executed"] == [executor.shell_argv("rm -rf node_modules && npm install")]


def test_error_text_is_redacted_before_upload(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    monkeypatch.setattr(
        executor,
        "run_capture",
        lambda argv, **_kw: executor.CommandResult(
            argv=argv,
            returncode=1,
            duration=0.1,
            stderr="auth failed with sk-abcdefghijklmnopqrstuvwxyz\nnpm ERR!",
        ),
    )
    fake_diagnosis(monkeypatch, "npm login", calls)
    run(dry_run=True)
    sent = calls["analyzed"][0]["stderr"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sent
    assert "npm ERR!" in sent


def test_dry_run_never_executes(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "npm install --legacy-peer-deps", calls)
    assert run(dry_run=True, assume_yes=True) == 1
    assert calls["executed"] == []


def test_critical_fix_is_refused_even_with_yes(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "rm -rf /", calls)
    assert run(assume_yes=True) == 1
    assert calls["executed"] == []


def test_high_risk_fix_needs_a_tty(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "git reset --hard origin/main", calls)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert run(assume_yes=True) == 1
    assert calls["executed"] == []


def test_high_risk_fix_runs_with_force(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "git reset --hard origin/main", calls)
    assert run(assume_yes=True, force=True) == 0
    assert len(calls["executed"]) == 1


def test_no_fix_command_returns_original_exit_code(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "", calls)
    assert run(assume_yes=True) == 1
    assert calls["executed"] == []


def test_failed_fix_exit_code_is_propagated(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    fake_diagnosis(monkeypatch, "npm install", calls)
    monkeypatch.setattr(executor, "run_passthrough", lambda argv, **_kw: 5)
    assert run(assume_yes=True) == 5


def test_missing_credentials_does_not_crash(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list]) -> None:
    def boom(**kwargs):
        raise cli.ai.MissingCredentials("no model configured")

    monkeypatch.setattr(cli.ai, "analyze_error", boom)
    assert run() == 1


def test_history_entry_is_recorded(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list], isolated_history: Path
) -> None:
    fake_diagnosis(monkeypatch, "npm install", calls)
    run(assume_yes=True)
    entries = history.load(5)
    assert len(entries) == 1
    assert entries[0]["command"] == "npm install"
    assert entries[0]["executed"] is True
    assert "stderr" not in entries[0]


@pytest.mark.parametrize(
    ("argv", "expected_head", "expected_command"),
    [
        (["-y", "npm", "install"], ["-y"], ["npm", "install"]),
        (["npm", "install", "-y"], [], ["npm", "install", "-y"]),
        (["--model", "gemini-2.5-pro", "pytest"], ["--model", "gemini-2.5-pro"], ["pytest"]),
        (["--model=gemini-2.5-pro", "pytest"], ["--model=gemini-2.5-pro"], ["pytest"]),
        (["--dry-run", "--", "ls", "-la"], ["--dry-run"], ["ls", "-la"]),
        (["apt", "install", "--force", "foo"], [], ["apt", "install", "--force", "foo"]),
        (["--doctor"], ["--doctor"], []),
        ([], [], []),
    ],
)
def test_split_argv(argv, expected_head, expected_command) -> None:
    head, command = cli.split_argv(argv)
    assert head == expected_head
    assert command == expected_command


def test_print_history_renders_entries(isolated_history: Path) -> None:
    history.record(
        command="npm install",
        exit_code=1,
        fix_command="npm ci",
        risk="low",
        executed=True,
        fix_exit_code=0,
    )
    history.record(command="mvn package", exit_code=1, risk="high", executed=False)
    assert cli.print_history(5) == 0


def test_print_history_without_any_runs(isolated_history: Path) -> None:
    assert cli.print_history(5) == 0


def test_doctor_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "TERM_FIX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert cli.print_doctor(Settings(), check=False) == 1


def test_doctor_reports_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyD-1234567890abcdefghijklmnopqrstu")
    assert cli.print_doctor(Settings(provider="gemini"), check=False) == 0
