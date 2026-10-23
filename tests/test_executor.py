from __future__ import annotations

import os
import sys

import pytest

from term_fix import executor


def test_captures_stderr_and_exit_code() -> None:
    result = executor.run_capture(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        tee=False,
    )
    assert result.returncode == 3
    assert "boom" in result.stderr
    assert not result.ok


def test_stdout_is_not_captured_by_default(capfd: pytest.CaptureFixture[str]) -> None:
    result = executor.run_capture([sys.executable, "-c", "print('hello')"], tee=False)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "hello" in capfd.readouterr().out


def test_capture_all_pipes_stdout(capfd: pytest.CaptureFixture[str]) -> None:
    result = executor.run_capture(
        [sys.executable, "-c", "print('hello'); print('world')"],
        capture_stdout=True,
        tee=True,
    )
    assert "hello" in result.stdout
    assert "hello" in capfd.readouterr().out


def test_missing_binary_is_reported_as_127() -> None:
    result = executor.run_capture(["definitely-not-a-real-binary-42"])
    assert result.returncode == 127
    assert result.missing_binary
    assert "command not found" in result.stderr


def test_timeout_kills_the_child() -> None:
    result = executor.run_capture(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tee=False,
        timeout=0.4,
    )
    assert result.timed_out
    assert result.returncode != 0


def test_capture_is_bounded() -> None:
    result = executor.run_capture(
        [sys.executable, "-c", "import sys; [sys.stderr.write('x' * 100 + '\\n') for _ in range(2000)]"],
        tee=False,
    )
    assert len(result.stderr) <= executor.MAX_CAPTURE_CHARS
    assert "x" * 100 in result.stderr


@pytest.mark.parametrize(
    "tokens",
    [
        ["npm", "install"],
        ["mvn", "-q", "package"],
        ["docker", "compose", "up", "-d"],
    ],
)
def test_plain_commands_do_not_need_a_shell(tokens: list[str]) -> None:
    assert executor.needs_shell(tokens) is False
    assert executor.build_invocation(tokens) == tokens


@pytest.mark.parametrize(
    "tokens",
    [
        ["npm", "install", "&&", "npm", "run", "build"],
        ["ps", "aux", "|", "grep", "python"],
        ["echo", "hi", ">", "out.txt"],
    ],
)
def test_shell_syntax_is_routed_through_a_shell(tokens: list[str]) -> None:
    assert executor.needs_shell(tokens) is True
    argv = executor.build_invocation(tokens)
    assert argv[-2] == "-c" or os.name == "nt"
    assert argv[-1] == " ".join(tokens)


def test_tail_for_ai_keeps_the_end() -> None:
    text = "a" * 100 + "TAIL"
    tail, dropped = executor.tail_for_ai(text, 10)
    assert tail == text[-10:]
    assert dropped == len(text) - 10


def test_tail_for_ai_no_truncation() -> None:
    tail, dropped = executor.tail_for_ai("short", 100)
    assert tail == "short"
    assert dropped == 0
