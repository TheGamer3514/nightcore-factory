"""Load KEY=VALUE pairs from a .env file next to this script into os.environ.

Imported first by nightcore.py, because the other modules read their settings
from the environment at import time. Variables already set in the real
environment win, so a .env never overrides an explicit `export`.
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def load(path: Path = ENV_PATH) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def flag(name: str, default: bool = True) -> bool:
    """A boolean setting. Blank or unset gives `default`; 0/false/no/off is False."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


load()
