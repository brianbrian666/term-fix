"""Model access: prompt construction, provider calls, response parsing.

Two providers are supported on purpose. ``gemini`` is the default because it is
cheap and fast; ``openai`` means any OpenAI-compatible endpoint, which keeps the
tool usable with DeepSeek, Qwen, Kimi, Groq or a local Ollama server.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .context import EnvContext

SYSTEM_PROMPT = (
    "You are term-fix, a terminal debugging agent. You see a command that just failed "
    "and its error output. You are terse, concrete and safety-conscious. "
    "You answer only with the requested JSON object."
)

MAX_OUTPUT_TOKENS = 700

LANG_NAMES = {
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "en": "English",
    "ja": "Japanese",
}

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


class AIError(RuntimeError):
    """Anything that stopped us from getting an answer."""


class MissingCredentials(AIError):
    """No API key and no local endpoint configured."""


@dataclass
class Diagnosis:
    explanation: str
    fix_command: str
    confidence: str = "medium"
    notes: str = ""
    provider: str = ""
    model: str = ""

    @property
    def has_fix(self) -> bool:
        return bool(self.fix_command.strip())


def gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def openai_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("TERM_FIX_API_KEY")


def available_providers() -> list[str]:
    providers = []
    if gemini_key():
        providers.append("gemini")
    if openai_key():
        providers.append("openai")
    return providers


def resolve_provider(settings: Settings) -> tuple[str, str]:
    """Return ``(provider, model)`` for this run."""
    provider = settings.provider
    if provider == "auto":
        if settings.base_url:
            provider = "openai"
        elif gemini_key():
            provider = "gemini"
        elif openai_key():
            provider = "openai"
        else:
            raise MissingCredentials(
                "no model configured: set GEMINI_API_KEY (or OPENAI_API_KEY / TERM_FIX_BASE_URL)"
            )

    if provider == "gemini" and not gemini_key():
        raise MissingCredentials("GEMINI_API_KEY is not set")
    if provider == "openai" and not openai_key() and not settings.base_url:
        raise MissingCredentials("OPENAI_API_KEY is not set (or pass --base-url for a local model)")
    return provider, settings.model_for(provider)


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code.strip().lower(), code)


def build_prompt(
    *,
    command: str,
    exit_code: int,
    stderr: str,
    env: EnvContext,
    lang: str = "zh",
    dropped_chars: int = 0,
    stream: str = "stderr",
) -> str:
    truncated = f" ({dropped_chars} earlier characters omitted)" if dropped_chars else ""
    context = "\n".join(env.to_lines())
    return f"""## Environment
{context}

## Failed command
```
{command}
```

## Exit code
{exit_code}

## Error output ({stream}){truncated}
```
{stderr or "(empty - the command produced no output)"}
```

## Task
Explain the failure and give the single best fix command.

Reply with ONLY this JSON object, no markdown fences and no extra text:
{{"explanation": "...", "fix_command": "...", "confidence": "high|medium|low", "notes": "..."}}

Rules:
- "explanation": at most 2 short sentences, in {lang_name(lang)}. Name the root cause, not the symptom.
- "fix_command": ONE line that can be pasted into a {env.shell} shell and run non-interactively from {env.cwd}. Chain steps with && if needed. No markdown, no placeholders like <path>, no leading "$", no comments.
- If the correct fix needs human judgement, secrets, or several manual steps, set "fix_command" to "" and explain the next step in "notes".
- Target {env.os_name}. Only use flags and package names that exist there.
- Prefer the minimal action that makes the original command succeed. Never invent a different project.
"""


def _clean_command(text: str) -> str:
    command = (text or "").strip()
    if command.startswith("```"):
        command = re.sub(r"^```[a-zA-Z]*\s*", "", command)
        command = re.sub(r"\s*```$", "", command)
    command = command.strip().strip("`").strip()
    for prefix in ("$ ", "> ", "# "):
        if command.startswith(prefix):
            command = command[len(prefix):].strip()
    if command.lower() in {"", "none", "n/a", "null", "nil", "-"}:
        return ""
    return command


def _loads_block(text: str) -> dict | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    for attempt in (candidate, None):
        if attempt is None:
            match = _JSON_BLOCK.search(text)
            if not match:
                return None
            attempt = match.group(0)
        try:
            parsed = json.loads(attempt)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_response(text: str, *, provider: str = "", model: str = "") -> Diagnosis:
    """Parse a model reply, tolerating JSON, fenced JSON or the tag format."""
    raw = (text or "").strip()
    if not raw:
        raise AIError("the model returned an empty response")

    payload = _loads_block(raw)
    if payload is not None:
        return Diagnosis(
            explanation=str(payload.get("explanation") or "").strip(),
            fix_command=_clean_command(str(payload.get("fix_command") or "")),
            confidence=str(payload.get("confidence") or "medium").strip().lower(),
            notes=str(payload.get("notes") or "").strip(),
            provider=provider,
            model=model,
        )

    # Fallback: the delimited format some models fall back to.
    if "[FIX_COMMAND]" in raw:
        head, _, tail = raw.partition("[FIX_COMMAND]")
        return Diagnosis(
            explanation=head.replace("[EXPLANATION]", "").strip(),
            fix_command=_clean_command(tail),
            provider=provider,
            model=model,
        )

    # Last resort: keep the prose, offer no command.
    return Diagnosis(explanation=raw[:600], fix_command="", provider=provider, model=model)


def _with_retry(func, attempts: int = 2):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
        except AIError:
            raise
        except Exception as exc:  # provider SDK errors: retry once, then surface
            last = exc
            if attempt == attempts - 1:
                break
        if attempt < attempts - 1:
            time.sleep(1.0 + attempt)
    raise AIError(str(last) if last else "unknown model error")


def _call_gemini(prompt: str, model: str, timeout: float) -> str:
    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise AIError("google-genai is not installed: pip install google-genai") from exc

    api_key = gemini_key()
    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout * 1000)),
        )
    except (TypeError, ValueError):  # older SDKs without http_options
        client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        response_mime_type="application/json",
    )
    response = client.models.generate_content(model=model, contents=prompt, config=config)
    text = getattr(response, "text", None)
    if not text:
        raise AIError("gemini returned no text (blocked or empty candidate)")
    return text


def _call_openai_compatible(prompt: str, model: str, timeout: float, base_url: str | None) -> str:
    root = (base_url or "https://api.openai.com/v1").rstrip("/")
    url = root if root.endswith("/chat/completions") else f"{root}/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = openai_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=payload)
        if response.status_code == 400 and "response_format" in response.text:
            # Local servers (Ollama, llama.cpp) often reject this field.
            payload.pop("response_format", None)
            response = client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:300]
            raise AIError(f"{url} returned HTTP {response.status_code}: {detail}")
        data = response.json()

    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(f"unexpected response shape from {url}") from exc


def analyze_error(
    *,
    command: str,
    exit_code: int,
    stderr: str,
    env: EnvContext,
    settings: Settings,
    dropped_chars: int = 0,
    stream: str = "stderr",
) -> Diagnosis:
    """One round trip: error in, diagnosis out."""
    provider, model = resolve_provider(settings)
    if not model:
        raise AIError(f"no default model known for provider '{provider}', pass --model")

    prompt = build_prompt(
        command=command,
        exit_code=exit_code,
        stderr=stderr,
        env=env,
        lang=settings.lang,
        dropped_chars=dropped_chars,
        stream=stream,
    )
    if provider == "gemini":
        raw = _with_retry(lambda: _call_gemini(prompt, model, settings.timeout))
    else:
        raw = _with_retry(
            lambda: _call_openai_compatible(prompt, model, settings.timeout, settings.base_url)
        )
    return parse_response(raw, provider=provider, model=model)
