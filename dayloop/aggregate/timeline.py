"""Build the DayTimeline for one date (the estimator's entry point)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..config import Config
from ..models import ActivityRecord, DayTimeline, iso_now
from .redact import redact_timeline
from .segment import segment


def _warn(msg: str) -> None:
    print(f"dayloop: warning: {msg}", file=sys.stderr)


def _safe_fetch(name: str, fn: Callable[..., list[ActivityRecord]],
                *args) -> list[ActivityRecord]:
    """Run one source fetch; any failure logs one line and yields []."""
    try:
        records = fn(*args)
        return records if isinstance(records, list) else []
    except NotImplementedError:
        _warn(f"source '{name}' not implemented yet — skipping")
        return []
    except Exception as exc:  # noqa: BLE001 — sources must never kill the build
        _warn(f"source '{name}' failed ({type(exc).__name__}: {exc}) — skipping")
        return []


def _day_window_utc(date: str) -> tuple[str, str]:
    """[date 00:00, next day 00:00) local time -> ISO-8601 UTC strings."""
    day = datetime.strptime(date, "%Y-%m-%d")
    start_local = day.astimezone()  # naive local midnight -> aware local
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


def build(date: str, config: Config) -> DayTimeline:
    """Build the full DayTimeline for `date` (YYYY-MM-DD).

    sources (each failing independently) -> segment -> stats -> redact.
    Every source may legitimately return [] (tool not installed yet) — the
    build still succeeds and produces a valid, possibly sparse, timeline.
    """
    from ..sources import calendar as calendar_src
    from ..sources import github as github_src
    from ..sources import granola as granola_src
    from ..sources import screenpipe as screenpipe_src

    start_iso, end_iso = _day_window_utc(date)

    screen = _safe_fetch("screenpipe", screenpipe_src.fetch, start_iso, end_iso, config)
    cal = _safe_fetch("calendar", calendar_src.fetch, date, config)
    gh = _safe_fetch("github", github_src.fetch, date, config)
    gran = _safe_fetch("granola", granola_src.fetch, date, config)

    sessions = segment(screen)

    audio = [r for r in screen if r.kind == "audio"]
    meetings = sorted(audio + gran, key=lambda r: r.start or "")

    per_app: dict[str, float] = {}
    per_cat: dict[str, float] = {}
    total = 0.0
    for s in sessions:
        total += s.minutes
        if s.app:
            per_app[s.app] = per_app.get(s.app, 0.0) + s.minutes
        cat = s.category or "other"
        per_cat[cat] = per_cat.get(cat, 0.0) + s.minutes

    stats = {
        "total_active_minutes": round(total, 1),
        "per_app_minutes": {k: round(v, 1) for k, v in per_app.items()},
        "per_category_minutes": {k: round(v, 1) for k, v in per_cat.items()},
        "counts": {
            "sessions": len(sessions),
            "calendar_events": len(cal),
            "github_events": len(gh),
            "meeting_records": len(meetings),
            "raw_records": sum(s.record_count for s in sessions),
        },
    }

    tl = DayTimeline(
        date=date,
        sessions=sessions,
        calendar=cal,
        github=gh,
        meetings=meetings,
        stats=stats,
        generated_at=iso_now(),
    )
    # Redact before returning so anything persisted downstream is already safe.
    return redact_timeline(tl)
