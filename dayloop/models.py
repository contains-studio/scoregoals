"""dayloop.models — FROZEN shared data contracts.

Every module codes against these dataclasses. Do NOT edit without
coordinating across all modules (see GOAL.md).

Conventions:
- All timestamps are ISO-8601 *strings* (e.g. "2026-07-11T09:30:00-07:00"),
  never datetime objects, so JSON round-tripping is trivial.
- All (de)serialization goes through to_json / from_json below.
- Fields carry safe defaults so partial JSON reconstructs without errors;
  the field names and types are the contract.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "ActivityRecord",
    "Session",
    "DayTimeline",
    "Goal",
    "GoalAlignment",
    "Report",
    "iso_now",
    "to_dict",
    "to_json",
    "from_json",
    "timeline_from_json",
    "report_from_json",
]


def iso_now() -> str:
    """Current local time as an ISO-8601 string with UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ActivityRecord:
    """One raw observation from a sensor."""

    source: str = ""        # screenpipe | calendar | github | granola
    kind: str = ""          # ocr | audio | window | ui | calendar | git | github | granola
    start: str = ""         # ISO-8601
    end: str | None = None  # ISO-8601, or None for point events (e.g. a commit)
    app: str | None = None
    title: str | None = None
    text: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Session:
    """A contiguous block of related activity, produced by aggregate/segment.py."""

    start: str = ""
    end: str = ""
    app: str | None = None
    title: str | None = None
    project: str | None = None
    topic: str | None = None
    category: str | None = None  # coding|comms|meeting|browsing|research|design|idle|other
    summary: str | None = None
    minutes: float = 0.0
    text_excerpt: str = ""
    record_count: int = 0


@dataclass
class DayTimeline:
    """Everything dayloop knows about one day."""

    date: str = ""  # YYYY-MM-DD
    sessions: list[Session] = field(default_factory=list)
    calendar: list[ActivityRecord] = field(default_factory=list)
    github: list[ActivityRecord] = field(default_factory=list)
    meetings: list[ActivityRecord] = field(default_factory=list)
    # stats keys: total_active_minutes, per_app_minutes, per_category_minutes, counts
    stats: dict = field(default_factory=dict)
    generated_at: str = field(default_factory=iso_now)


@dataclass
class Goal:
    """One goal parsed from goals.md."""

    id: str = ""
    name: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    target_pct: float | None = None


@dataclass
class GoalAlignment:
    """How one day's time maps onto one goal."""

    goal_id: str = ""
    goal_name: str = ""
    minutes: float = 0.0
    pct_time: float = 0.0
    target_pct: float | None = None
    on_track: bool = False


@dataclass
class Report:
    """Output of one analysis backend run."""

    date: str = ""
    kind: str = "eod"  # eod | weekly | morning
    backend: str = ""  # gemini | ollama
    model: str = ""
    narrative: str = ""
    alignments: list[GoalAlignment] = field(default_factory=list)
    overall_score: int = 0  # 0-100
    drift_flags: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    generated_at: str = field(default_factory=iso_now)
    raw: dict = field(default_factory=dict)


# --- (de)serialization -------------------------------------------------------

# Which fields of which container hold lists of nested dataclasses.
_NESTED_LIST_FIELDS: dict[type, dict[str, type]] = {
    DayTimeline: {
        "sessions": Session,
        "calendar": ActivityRecord,
        "github": ActivityRecord,
        "meetings": ActivityRecord,
    },
    Report: {"alignments": GoalAlignment},
}


def to_dict(obj: Any) -> dict:
    """Dataclass -> plain dict (all leaves JSON-safe: str/num/bool/list/dict)."""
    return dataclasses.asdict(obj)


def to_json(obj: Any, indent: int = 2) -> str:
    """Dataclass -> pretty JSON string."""
    return json.dumps(to_dict(obj), indent=indent, ensure_ascii=False)


def _coerce(cls: type, data: dict) -> Any:
    """Build `cls` from a dict: ignore unknown keys, default missing ones,
    and reconstruct the known nested dataclass lists."""
    names = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in names}
    for key, subcls in _NESTED_LIST_FIELDS.get(cls, {}).items():
        items = kwargs.get(key)
        if isinstance(items, list):
            kwargs[key] = [_coerce(subcls, it) if isinstance(it, dict) else it for it in items]
    return cls(**kwargs)


def from_json(data: str | bytes | dict, cls: type) -> Any:
    """JSON string (or already-parsed dict) -> dataclass instance of `cls`.

    Robust: missing keys get defaults, unknown keys are ignored, nested lists
    (DayTimeline sessions/calendar/github/meetings, Report alignments) are
    reconstructed as proper dataclasses.
    """
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise TypeError(f"expected a JSON object for {cls.__name__}, got {type(data).__name__}")
    return _coerce(cls, data)


def timeline_from_json(data: str | bytes | dict) -> DayTimeline:
    """JSON -> DayTimeline."""
    return from_json(data, DayTimeline)


def report_from_json(data: str | bytes | dict) -> Report:
    """JSON -> Report."""
    return from_json(data, Report)
