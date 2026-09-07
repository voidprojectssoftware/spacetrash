"""Small on-disk config shared by stcli commands.

Lives at ``config.json`` in the per-user app dir (``%APPDATA%\\stcli`` on
Windows, ``~/.config/stcli`` elsewhere).
"""

from __future__ import annotations

import json
from pathlib import Path

import click


def config_path() -> Path:
    return Path(click.get_app_dir("stcli")) / "config.json"


def load() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(config: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
