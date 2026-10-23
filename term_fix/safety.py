"""Local safety net: secret redaction before upload, risk scoring before exec.

A tool that pipes your stderr to a model and then *runs* what comes back needs
two guards. Both live here so they are easy to audit and to unit-test.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum


class Risk(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return {
            Risk.LOW: "low",
            Risk.MEDIUM: "medium",
            Risk.HIGH: "high",
            Risk.CRITICAL: "critical",
        }[self]


@dataclass
class Assessment:
    risk: Risk = Risk.LOW
    reasons: tuple[str, ...] = ()

    @property
    def is_dangerous(self) -> bool:
        return self.risk >= Risk.HIGH


# --- secret redaction -------------------------------------------------------
# Everything here is deliberately conservative: a missed secret is far worse
# than an over-redacted error message.
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{12,}"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "AIza***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "xox*-***"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), "glpat-***"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "hf_***"),
    (re.compile(r"(?i)((?:passw(?:or)?d|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key)\s*[=:]\s*)"
                r"(['\"]?)[^\s'\"]{4,}\2"), r"\1\2***\2"),
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)"), r"\1***\3"),
)


def redact(text: str) -> str:
    """Strip credentials from a chunk of terminal output."""
    if not text:
        return text
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


def mask_secret(value: str | None) -> str:
    """Show just enough of a key to recognise it, never enough to use it."""
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


# --- risk assessment --------------------------------------------------------
_ARTIFACT_DIRS = {
    "node_modules", ".venv", "venv", "env", "dist", "build", "target", "out",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
    ".nuxt", ".cache", ".tox", "coverage", ".coverage", "vendor", "Pods",
    "DerivedData", "tmp", ".turbo", ".parcel-cache",
}
_SYSTEM_PREFIXES = (
    "/usr", "/etc", "/var", "/bin", "/sbin", "/System", "/Library", "/opt",
    "/boot", "/dev", "/proc", "/private", "C:\\Windows", "C:\\Program",
)
_ROOT_TARGETS = {
    "/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "${HOME}", "${HOME}/",
    "*", "..", "../", ".", "./", "/*.*",
}

_STATIC_RULES: tuple[tuple[Risk, re.Pattern[str], str], ...] = (
    (Risk.CRITICAL, re.compile(r"--no-preserve-root"), "rm with --no-preserve-root"),
    (Risk.CRITICAL, re.compile(r"\bmkfs(\.\w+)?\b"), "formats a filesystem"),
    (Risk.CRITICAL, re.compile(r"\bdd\b[^\n;&|]*\bof=/dev/"), "writes raw disk devices"),
    (Risk.CRITICAL, re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "shuts the machine down"),
    (Risk.CRITICAL, re.compile(r">\s*/dev/[hs]d[a-z]"), "overwrites a block device"),
    (Risk.CRITICAL, re.compile(r"\bkill\s+(-9\s+)?-1\b"), "kills every process"),
    (Risk.CRITICAL, re.compile(r":\(\)\s*\{.*\|.*&.*\}\s*;\s*:"), "fork bomb"),
    (Risk.HIGH, re.compile(r"\bsudo\b|\bdoas\b"), "runs with elevated privileges"),
    (Risk.HIGH, re.compile(r"(?:curl|wget)[^\n;&|]*\|\s*(?:sudo\s+)?(?:ba|z|k|fi|da)?sh\b"), "pipes a download into a shell"),
    (Risk.HIGH, re.compile(r"\bgit\s+push\b[^\n;&|]*(?:--force\b|--force-with-lease\b|\s-f\b)"), "force-pushes a branch"),
    (Risk.HIGH, re.compile(r"\bgit\s+reset\s+--hard\b"), "discards local commits/changes"),
    (Risk.HIGH, re.compile(r"\bgit\s+clean\b[^\n;&|]*-[a-zA-Z]*[fdx]"), "deletes untracked files"),
    (Risk.HIGH, re.compile(r"\bgit\s+stash\s+(drop|clear)\b"), "drops stashed work"),
    (Risk.HIGH, re.compile(r"\b(?:chmod|chown)\s+-R\b[^\n;&|]*\s/(?:\s|$)"), "rewrites root permissions"),
    (Risk.HIGH, re.compile(r"\b(?:DROP|TRUNCATE)\s+(?:DATABASE|TABLE|SCHEMA)\b", re.I), "destroys database objects"),
    (Risk.MEDIUM, re.compile(r"\bdocker\s+(?:system|volume|image|container)\s+prune\b"), "prunes docker resources"),
    (Risk.MEDIUM, re.compile(r"\bdocker\s+(?:rm|rmi)\b"), "removes containers or images"),
    (Risk.MEDIUM, re.compile(r"\bnpm\s+publish\b|\byarn\s+publish\b|\btwine\s+upload\b"), "publishes a package"),
    (Risk.MEDIUM, re.compile(r"--break-system-packages\b"), "alters system python packages"),
    (Risk.MEDIUM, re.compile(r"\b(?:pkill|killall|kill)\b"), "terminates running processes"),
    (Risk.MEDIUM, re.compile(r"\bgit\s+(?:checkout|restore)\s+(?:--\s+)?\.\s*$"), "overwrites uncommitted edits"),
)


def _rm_assessment(segment: str) -> Assessment | None:
    """Classify ``rm`` by what it targets, not just by its flags."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    index = next((i for i, token in enumerate(tokens) if token.rsplit("/", 1)[-1] == "rm"), None)
    if index is None:
        return None

    rest = tokens[index + 1:]
    flags = [token for token in rest if token.startswith("-")]
    targets = [token for token in rest if not token.startswith("-") or token in {"-"}]
    recursive = any("r" in flag or "R" in flag for flag in flags)
    forced = any("f" in flag for flag in flags)
    if not recursive and not forced:
        return Assessment(Risk.LOW, ("plain rm",))

    elevated = bool(re.search(r"\b(?:sudo|doas)\b", segment))
    for target in targets:
        normalised = target.rstrip("/") or "/"
        if target in _ROOT_TARGETS or normalised in _ROOT_TARGETS:
            return Assessment(Risk.CRITICAL, (f"recursive delete of {target}",))
        if target.startswith(("~/", "$HOME/", "${HOME}/")):
            return Assessment(Risk.CRITICAL, (f"recursive delete inside your home directory: {target}",))
        if normalised.startswith(_SYSTEM_PREFIXES):
            risk = Risk.CRITICAL if elevated else Risk.HIGH
            return Assessment(risk, (f"recursive delete inside system path {target}",))

    if any(target.rstrip("/").rsplit("/", 1)[-1] in _ARTIFACT_DIRS for target in targets):
        return Assessment(Risk.MEDIUM, ("deletes a rebuildable build artifact",))
    return Assessment(Risk.HIGH, ("recursive/forced delete",))


def assess(command: str) -> Assessment:
    """Score a shell command by how much damage it could do."""
    command = (command or "").strip()
    if not command:
        return Assessment(Risk.LOW, ("empty command",))

    best = Assessment()
    # Single pipes are kept intact so "curl ... | sh" stays one analysable line.
    for segment in re.split(r"&&|\|\||;", command):
        segment = segment.strip()
        if not segment:
            continue
        rm = _rm_assessment(segment)
        if rm is not None and rm.risk > best.risk:
            best = rm
        for risk, pattern, reason in _STATIC_RULES:
            if risk > best.risk and pattern.search(segment):
                best = Assessment(risk, (reason,))
        if best.risk is Risk.CRITICAL:
            break
    return best
