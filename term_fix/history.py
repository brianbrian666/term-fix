"""Append-only JSONL log of past runs, for ``tf --history``.

The log never stores raw stderr, so a failing build cannot leak project source
or credentials into a file on disk.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Any

from .config import HISTORY_PATH


def record(**fields: Any) -> None:
    """Append one entry. Logging must never break the run, so errors are ignored."""
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(HISTORY_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def load(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent entries, oldest first."""
    if not HISTORY_PATH.is_file():
        return []
    try:
        with HISTORY_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=max(limit, 1))
    except OSError:
        return []

    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries
