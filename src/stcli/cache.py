"""A small answer cache, so asking the same thing twice is free.

Keyed on everything that could change the answer: the harness, its settings
and the exact prompt. Entries are plain JSON files under the stcli app dir.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import click


def cache_dir() -> Path:
    return Path(click.get_app_dir("stcli")) / "answers"


def key_for(parts: list[str]) -> str:
    material = "\x00".join(parts).encode("utf-8", "replace")
    return hashlib.sha256(material).hexdigest()[:32]


def _entry_path(key: str) -> Path:
    return cache_dir() / f"{key}.json"


def load(key: str, max_age_days: int) -> str | None:
    """The stored answer, if there is one and it is still young enough."""
    if max_age_days <= 0:
        return None

    path = _entry_path(key)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    stored = entry.get("stored_at", 0)
    if time.time() - stored > max_age_days * 86400:
        path.unlink(missing_ok=True)
        return None

    answer = entry.get("answer")
    return answer if isinstance(answer, str) and answer.strip() else None


def store(key: str, answer: str, question: str = "") -> None:
    """Keep an answer. Failing to write one is never worth an error."""
    path = _entry_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"question": question, "answer": answer, "stored_at": time.time()},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def clear() -> int:
    """Drop every stored answer. Returns how many there were."""
    directory = cache_dir()
    if not directory.exists():
        return 0

    removed = 0
    for path in directory.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
