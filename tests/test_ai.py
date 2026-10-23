from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from term_fix import ai
from term_fix.config import Settings
from term_fix.context import EnvContext


def sample_env() -> EnvContext:
    return EnvContext(
        os_name="Darwin 25.0.0",
        arch="arm64",
        shell="zsh",
        cwd="/Users/dev/shop",
        tools={"npm": "11.2.0"},
        project_files=["package.json", "package-lock.json"],
    )


def test_parse_json_reply() -> None:
    raw = json.dumps(
        {
            "explanation": "依赖树冲突：react 18 与 react-dom 17 不兼容。",
            "fix_command": "npm install react-dom@18",
            "confidence": "high",
            "notes": "",
        }
    )
    diagnosis = ai.parse_response(raw, provider="gemini", model="gemini-2.5-flash")
    assert diagnosis.fix_command == "npm install react-dom@18"
    assert diagnosis.confidence == "high"
    assert diagnosis.provider == "gemini"


def test_parse_fenced_json_reply() -> None:
    raw = '```json\n{"explanation": "缺少虚拟环境。", "fix_command": "python -m venv .venv"}\n```'
    diagnosis = ai.parse_response(raw)
    assert diagnosis.fix_command == "python -m venv .venv"
    assert diagnosis.explanation == "缺少虚拟环境。"


def test_parse_json_with_prose_around_it() -> None:
    raw = 'Sure! {"explanation": "x", "fix_command": "ls -la"}\nHope that helps.'
    assert ai.parse_response(raw).fix_command == "ls -la"


def test_parse_tag_format_fallback() -> None:
    raw = "[EXPLANATION]\n端口被占用。\n[FIX_COMMAND]\nlsof -ti:3000 | xargs kill -9"
    diagnosis = ai.parse_response(raw)
    assert diagnosis.explanation == "端口被占用。"
    assert diagnosis.fix_command == "lsof -ti:3000 | xargs kill -9"


def test_parse_strips_shell_prompt_and_fences() -> None:
    raw = '{"explanation": "x", "fix_command": "```bash\\n$ brew install openjdk@17\\n```"}'
    assert ai.parse_response(raw).fix_command == "brew install openjdk@17"


def test_parse_treats_none_as_no_fix() -> None:
    raw = '{"explanation": "需要人工判断。", "fix_command": "none", "notes": "手动检查配置"}'
    diagnosis = ai.parse_response(raw)
    assert diagnosis.has_fix is False
    assert diagnosis.notes == "手动检查配置"


def test_parse_empty_reply_raises() -> None:
    with pytest.raises(ai.AIError):
        ai.parse_response("   ")


def test_prose_only_reply_keeps_text_without_command() -> None:
    diagnosis = ai.parse_response("Looks like a port conflict, check your .env file.")
    assert diagnosis.has_fix is False
    assert "port conflict" in diagnosis.explanation


def test_build_prompt_carries_environment_and_error() -> None:
    prompt = ai.build_prompt(
        command="npm install",
        exit_code=1,
        stderr="npm ERR! ERESOLVE",
        env=sample_env(),
        lang="zh",
        dropped_chars=120,
    )
    assert "npm install" in prompt
    assert "npm ERR! ERESOLVE" in prompt
    assert "Darwin 25.0.0" in prompt
    assert "zsh" in prompt
    assert "120 earlier characters omitted" in prompt
    assert "Simplified Chinese" in prompt
    assert "## Exit code" in prompt


def test_resolve_provider_prefers_explicit_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings(provider="auto", base_url="http://localhost:11434/v1")
    provider, _ = ai.resolve_provider(settings)
    assert provider == "openai"


def test_resolve_provider_autodetects_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    provider, model = ai.resolve_provider(Settings(provider="auto"))
    assert provider == "gemini"
    assert model == "gemini-2.5-flash"


def test_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "TERM_FIX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ai.MissingCredentials):
        ai.resolve_provider(Settings(provider="auto"))


class _StubHandler(BaseHTTPRequestHandler):
    reply = {"explanation": "缺少依赖。", "fix_command": "pip install requests", "confidence": "high"}

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(self.reply, ensure_ascii=False)}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_openai_compatible_round_trip(stub_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    diagnosis = ai.analyze_error(
        command="python app.py",
        exit_code=1,
        stderr="ModuleNotFoundError: No module named 'requests'",
        env=sample_env(),
        settings=Settings(provider="openai", model="gpt-4o-mini", base_url=stub_server),
    )
    assert diagnosis.fix_command == "pip install requests"
    assert diagnosis.provider == "openai"
