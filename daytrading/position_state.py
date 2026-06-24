from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_FILENAME = "alpaca_open_position_state.json"


def _state_dir() -> Path:
    # This file lives at daytrading/position_state.py, so the repo root is
    # two levels up (daytrading/ -> repo root).
    path = Path(__file__).resolve().parent.parent / "data" / "logs" / "broker"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    return _state_dir() / STATE_FILENAME


def load_open_position_state() -> dict[str, Any] | None:
    """
    Read the current Alpaca paper position-tracking state, if any.

    This file is the link between the entry job and the position monitor,
    since they run as separate scheduled jobs (possibly hours apart) and
    each run gets a fresh checkout of the repo. It is committed back to the
    repository by both workflows so the next run -- on either workflow --
    sees the latest state.
    """
    path = state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_open_position_state(state: dict[str, Any]) -> Path:
    path = state_path()
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    return path


def clear_open_position_state() -> None:
    path = state_path()
    if path.exists():
        path.unlink()
