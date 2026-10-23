"""Child-process execution with live output and bounded stderr capture.

Two invariants matter for a wrapper like this:

* the wrapped command keeps writing to the real terminal, so colours, progress
  bars and pagers still work (stdout is inherited, never buffered);
* whatever the model sees stays bounded in size, so a noisy build log cannot
  blow up the prompt or the bill.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO

MAX_CAPTURE_CHARS = 64_000

# Tokens that only make sense to a shell. If we see one of these as a whole
# argument we re-run the line through the user's shell instead of exec'ing argv.
SHELL_OPERATORS = {
    "&&", "||", "|", ";", "&",
    ">", ">>", "<", "<<", "2>", "2>>", "&>", "2>&1", "1>&2",
}


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    duration: float
    stderr: str = ""
    stdout: str = ""
    interrupted: bool = False
    timed_out: bool = False
    missing_binary: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def display(self) -> str:
        return shlex.join(self.argv)


class _TailBuffer:
    """Accumulates text but only keeps the last ``limit`` characters."""

    def __init__(self, limit: int = MAX_CAPTURE_CHARS) -> None:
        self._chunks: list[str] = []
        self._size = 0
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            while self._size > self._limit and len(self._chunks) > 1:
                self._size -= len(self._chunks.pop(0))
            if self._size > self._limit:
                self._chunks[0] = self._chunks[0][-self._limit:]
                self._size = self._limit

    def value(self) -> str:
        with self._lock:
            return "".join(self._chunks)


def _pump(stream: IO[str], sink: _TailBuffer, mirror: IO[str] | None) -> None:
    """Drain ``stream`` into ``sink`` while echoing it to ``mirror``."""
    try:
        while True:
            try:
                chunk = stream.readline()
            except (ValueError, OSError):
                break
            if not chunk:
                break
            sink.add(chunk)
            if mirror is not None:
                try:
                    mirror.write(chunk)
                    mirror.flush()
                except (BrokenPipeError, ValueError, OSError):
                    mirror = None
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _terminate(process: subprocess.Popen[str]) -> None:
    for sig in ("terminate", "kill"):
        if process.poll() is not None:
            return
        try:
            getattr(process, sig)()
        except OSError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            continue


def needs_shell(tokens: list[str]) -> bool:
    """True when the line contains shell syntax that argv cannot express."""
    return any(token in SHELL_OPERATORS for token in tokens)


def shell_argv(command: str) -> list[str]:
    """Wrap a shell one-liner for the current platform."""
    if os.name == "nt":  # pragma: no cover - Windows only
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
    shell = os.environ.get("SHELL") or "/bin/sh"
    if not os.path.exists(shell):
        shell = "/bin/sh"
    return [shell, "-c", command]


def build_invocation(tokens: list[str]) -> list[str]:
    """Turn CLI tokens into a concrete argv."""
    if needs_shell(tokens):
        return shell_argv(" ".join(tokens))
    return tokens


def run_capture(
    argv: list[str],
    *,
    capture_stdout: bool = False,
    tee: bool = True,
    timeout: float | None = None,
    cwd: str | None = None,
) -> CommandResult:
    """Run ``argv`` and return its exit code plus captured stderr.

    stdout is inherited so the child sees a real TTY; pass ``capture_stdout``
    to pipe it through us instead (loses TTY detection, gains capture).
    """
    started = time.monotonic()
    stderr_sink = _TailBuffer()
    stdout_sink = _TailBuffer()

    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            cwd=cwd,
        )
    except FileNotFoundError:
        return CommandResult(
            argv=argv,
            returncode=127,
            duration=time.monotonic() - started,
            stderr=f"{argv[0]}: command not found",
            missing_binary=True,
        )
    except PermissionError:
        return CommandResult(
            argv=argv,
            returncode=126,
            duration=time.monotonic() - started,
            stderr=f"{argv[0]}: permission denied",
        )
    except OSError as exc:
        return CommandResult(
            argv=argv,
            returncode=127,
            duration=time.monotonic() - started,
            stderr=str(exc),
        )

    threads: list[threading.Thread] = []
    assert process.stderr is not None
    stderr_thread = threading.Thread(
        target=_pump,
        args=(process.stderr, stderr_sink, sys.stderr if tee else None),
        name="term-fix-stderr",
        daemon=True,
    )
    stderr_thread.start()
    threads.append(stderr_thread)

    if capture_stdout and process.stdout is not None:
        stdout_thread = threading.Thread(
            target=_pump,
            args=(process.stdout, stdout_sink, sys.stdout if tee else None),
            name="term-fix-stdout",
            daemon=True,
        )
        stdout_thread.start()
        threads.append(stdout_thread)

    interrupted = False
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
    except KeyboardInterrupt:
        interrupted = True
        # The terminal already delivered SIGINT to the child; give it a moment
        # to exit cleanly before escalating.
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate(process)

    for thread in threads:
        thread.join(timeout=5)

    return CommandResult(
        argv=argv,
        returncode=process.returncode if process.returncode is not None else 130,
        duration=time.monotonic() - started,
        stderr=stderr_sink.value(),
        stdout=stdout_sink.value(),
        interrupted=interrupted,
        timed_out=timed_out,
    )


def run_passthrough(argv: list[str], *, cwd: str | None = None) -> int:
    """Run a fix command attached to the real terminal (stdin/stdout/stderr)."""
    try:
        completed = subprocess.run(argv, cwd=cwd)
        return completed.returncode
    except FileNotFoundError:
        print(f"{argv[0]}: command not found", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"{argv[0]}: permission denied", file=sys.stderr)
        return 126
    except OSError as exc:
        print(f"failed to run fix: {exc}", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


def tail_for_ai(text: str, limit: int) -> tuple[str, int]:
    """Return the last ``limit`` characters plus how many were dropped."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    return text[-limit:], len(text) - limit
