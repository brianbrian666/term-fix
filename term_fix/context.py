"""Cheap, safe environment probing.

The single biggest quality jump in the model's answer comes from knowing where
the failure happened: macOS vs Debian, virtualenv vs system python, npm 9 vs 11.
Everything here is read-only and time-boxed.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PROBE_TIMEOUT = 2.5
MAX_PROBES = 3

# Names we are willing to execute just to read a version string. Anything that
# could prompt, mutate state or hit the network stays out of this list.
KNOWN_TOOLS: dict[str, str] = {
    "npm": "npm",
    "npx": "npm",
    "node": "node",
    "pnpm": "pnpm",
    "yarn": "yarn",
    "bun": "bun",
    "deno": "deno",
    "pip": "pip",
    "pip3": "pip",
    "python": "python",
    "python3": "python",
    "poetry": "poetry",
    "uv": "uv",
    "conda": "conda",
    "mvn": "mvn",
    "mvnw": "mvn",
    "gradle": "gradle",
    "gradlew": "gradle",
    "java": "java",
    "javac": "javac",
    "docker": "docker",
    "docker-compose": "docker-compose",
    "kubectl": "kubectl",
    "helm": "helm",
    "terraform": "terraform",
    "go": "go",
    "cargo": "cargo",
    "rustc": "rustc",
    "git": "git",
    "composer": "composer",
    "dotnet": "dotnet",
    "ruby": "ruby",
    "bundle": "bundle",
    "gcc": "gcc",
    "clang": "clang",
    "make": "make",
    "cmake": "cmake",
    "swift": "swift",
    "xcodebuild": "xcodebuild",
    "php": "php",
}

PROJECT_MARKERS = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "pyproject.toml", "requirements.txt", "poetry.lock", "uv.lock", "setup.py",
    "go.mod", "Cargo.toml", "Gemfile", "composer.json", "Dockerfile",
    "docker-compose.yml", "compose.yaml", "Makefile", "tsconfig.json",
)

VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?")


@dataclass
class EnvContext:
    os_name: str
    arch: str
    shell: str
    cwd: str
    container: str | None = None
    tools: dict[str, str] = field(default_factory=dict)
    project_files: list[str] = field(default_factory=list)
    hints: dict[str, str] = field(default_factory=dict)

    def to_lines(self) -> list[str]:
        lines = [
            f"os: {self.os_name} ({self.arch})",
            f"shell: {self.shell}",
            f"cwd: {self.cwd}",
        ]
        if self.container:
            lines.append(f"container: {self.container}")
        if self.hints:
            lines.append("env: " + ", ".join(f"{k}={v}" for k, v in self.hints.items()))
        if self.project_files:
            lines.append("project files: " + ", ".join(self.project_files))
        if self.tools:
            lines.append("tool versions: " + ", ".join(f"{k} {v}" for k, v in self.tools.items()))
        return lines


def _linux_pretty_name() -> str | None:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"') or None
    return None


def _os_name() -> str:
    """A distro-level description: ``apt`` vs ``dnf`` matters more than the kernel."""
    system = platform.system()
    if system == "Darwin":
        version = platform.mac_ver()[0]
        return f"macOS {version}" if version else f"Darwin {platform.release()}"
    if system == "Linux":
        pretty = _linux_pretty_name()
        if pretty:
            return f"{pretty} (kernel {platform.release()})"
        return f"Linux {platform.release()}"
    if system == "Windows":  # pragma: no cover - Windows only
        return f"Windows {platform.version()}"
    return f"{system} {platform.release()}"


def _detect_container() -> str | None:
    if Path("/.dockerenv").exists():
        return "docker"
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "kubepods" in cgroup:
        return "kubernetes"
    if "docker" in cgroup:
        return "docker"
    if "podman" in cgroup:
        return "podman"
    return None


def _shell_name() -> str:
    candidate = os.environ.get("SHELL") or os.environ.get("COMSPEC") or ""
    if not candidate:
        return "unknown"
    name = Path(candidate).name
    if os.name == "nt":  # pragma: no cover - Windows only
        return name.lower()
    return name


def _env_hints() -> dict[str, str]:
    hints: dict[str, str] = {}
    for var in ("VIRTUAL_ENV", "CONDA_DEFAULT_ENV", "JAVA_HOME", "GOROOT"):
        value = os.environ.get(var)
        if value:
            hints[var.lower()] = Path(value).name or value
    for var in ("NODE_ENV", "NODE_OPTIONS", "PIP_REQUIRE_VIRTUALENV", "CI"):
        value = os.environ.get(var)
        if value:
            hints[var.lower()] = value
    return hints


def detect_tools(command: str) -> list[str]:
    """Known tools mentioned in the command line, in order of appearance."""
    seen: list[str] = []
    for token in re.split(r"[\s;&|()]+", command):
        token = token.strip("'\"")
        base = token.rsplit("/", 1)[-1]
        tool = KNOWN_TOOLS.get(base)
        if tool and tool not in seen:
            seen.append(tool)
    return seen


def probe_version(tool: str, timeout: float = PROBE_TIMEOUT) -> str | None:
    try:
        completed = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = VERSION_RE.search(line)
        if match:
            return match.group(0)
        return line[:60]
    return None


def gather(command: str, cwd: Path | None = None) -> EnvContext:
    """Collect the context block that ships with the error report."""
    working_dir = cwd or Path.cwd()
    context = EnvContext(
        os_name=_os_name(),
        arch=platform.machine() or "unknown",
        shell=_shell_name(),
        cwd=str(working_dir),
        container=_detect_container(),
        project_files=[name for name in PROJECT_MARKERS if (working_dir / name).exists()][:8],
        hints=_env_hints(),
    )
    for tool in detect_tools(command)[:MAX_PROBES]:
        version = probe_version(tool)
        if version:
            context.tools[tool] = version
    return context
