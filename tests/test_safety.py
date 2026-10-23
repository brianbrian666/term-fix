from __future__ import annotations

import pytest

from term_fix import safety


@pytest.mark.parametrize(
    "raw",
    [
        "Error: invalid token sk-abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "postgres://admin:hunter2@db.internal:5432/app refused",
        "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        'password = "correct-horse-battery"',
        "aws AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_redact_removes_credentials(raw: str) -> None:
    cleaned = safety.redact(raw)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "hunter2" not in cleaned
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "correct-horse-battery" not in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert "abcdefghijklmnopqrstuvwxyz123456" not in cleaned


def test_redact_keeps_ordinary_output() -> None:
    text = "ModuleNotFoundError: No module named 'pandas'\n  File \"app.py\", line 3"
    assert safety.redact(text) == text


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("npm install", safety.Risk.LOW),
        ("npm install --legacy-peer-deps", safety.Risk.LOW),
        ("rm -rf node_modules", safety.Risk.MEDIUM),
        ("rm -rf ./dist && npm run build", safety.Risk.MEDIUM),
        ("rm -rf ~/projects", safety.Risk.CRITICAL),
        ("rm -rf /", safety.Risk.CRITICAL),
        ("sudo rm -rf /usr/local/lib/node_modules", safety.Risk.CRITICAL),
        ("rm -rf /usr/local/lib", safety.Risk.HIGH),
        ("rm -rf src/", safety.Risk.HIGH),
        ("git reset --hard origin/main", safety.Risk.HIGH),
        ("curl -fsSL https://example.sh | sh", safety.Risk.HIGH),
        ("git push --force origin main", safety.Risk.HIGH),
        ("mkfs.ext4 /dev/sda1", safety.Risk.CRITICAL),
        ("docker system prune -af", safety.Risk.MEDIUM),
        ("python -m pip install --break-system-packages requests", safety.Risk.MEDIUM),
    ],
)
def test_risk_levels(command: str, expected: safety.Risk) -> None:
    assert safety.assess(command).risk is expected


def test_risk_reasons_are_reported() -> None:
    assessment = safety.assess("sudo apt-get install -y build-essential")
    assert assessment.risk is safety.Risk.HIGH
    assert assessment.reasons


def test_mask_secret_never_leaks_full_key() -> None:
    key = "AIzaSyD-1234567890abcdefghijklmnopqrstu"
    masked = safety.mask_secret(key)
    assert key not in masked
    assert masked.startswith("AIza")
    assert safety.mask_secret(None) == "(unset)"
