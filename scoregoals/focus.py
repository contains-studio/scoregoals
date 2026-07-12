"""Focus blocks — a lightweight, single-slot "I am heads-down on goal X" flag.

State lives in data/focus.json:
    {"active": bool, "goal_id": str|null, "goal_name": str|null,
     "started_at": "ISO|null", "until": "ISO|null"}

An `until` in the past auto-expires the block (load() returns active=False for
it) so a timed block silently lapses without needing a stop. `scoregoals status`
surfaces the block, and feedback/nudge.py suppresses nudges while a block is
active AND recent activity is on the focus goal.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .models import iso_now

FOCUS_FILENAME = "focus.json"

_INACTIVE: dict = {
    "active": False,
    "goal_id": None,
    "goal_name": None,
    "started_at": None,
    "until": None,
}


def _path(config: Config) -> Path:
    return Path(config.data_dir) / FOCUS_FILENAME


def _warn(msg: str) -> None:
    print(f"[scoregoals.focus] {msg}", file=sys.stderr)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def load(config: Config) -> dict:
    """Return the effective focus block. A missing/corrupt file, or a block
    whose `until` is in the past, reads as inactive. Never raises."""
    path = _path(config)
    if not path.is_file():
        return dict(_INACTIVE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(f"ignoring bad {path} ({exc})")
        return dict(_INACTIVE)
    if not isinstance(data, dict):
        return dict(_INACTIVE)

    block = {
        "active": bool(data.get("active")),
        "goal_id": data.get("goal_id"),
        "goal_name": data.get("goal_name"),
        "started_at": data.get("started_at"),
        "until": data.get("until"),
    }
    # Auto-expire a timed block whose deadline has passed.
    until = _parse(block.get("until"))
    if block["active"] and until is not None and datetime.now() > until:
        block["active"] = False
    return block


def _save(config: Config, block: dict) -> dict:
    path = _path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(block, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return block


def _resolve_goal(ident: str, config: Config) -> tuple[str, str]:
    """Map a goal id / name / slug to (goal_id, goal_name). Falls back to the
    raw identifier for both when goals.md has no match, so `focus start` never
    fails just because a goal isn't declared yet."""
    from .compare import align

    ident_l = (ident or "").strip().lower()
    for g in align.load_goals(config):
        if ident_l in (g.id.lower(), g.name.lower()) or align._slug(g.name) == align._slug(ident):
            return g.id, g.name
    return ident, ident


def start(config: Config, goal_ident: str, minutes: int | None = None) -> dict:
    """Begin a focus block on `goal_ident` (id/name/slug). Optional `minutes`
    sets an auto-expiring deadline; omit for an open-ended block."""
    goal_id, goal_name = _resolve_goal(goal_ident, config)
    started_at = iso_now()
    until = None
    if minutes and minutes > 0:
        until = (datetime.now().astimezone() + timedelta(minutes=int(minutes))).isoformat(
            timespec="seconds"
        )
    return _save(
        config,
        {
            "active": True,
            "goal_id": goal_id,
            "goal_name": goal_name,
            "started_at": started_at,
            "until": until,
        },
    )


def stop(config: Config) -> dict:
    """End any active focus block (idempotent)."""
    return _save(config, dict(_INACTIVE))
