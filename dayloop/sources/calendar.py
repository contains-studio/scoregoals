"""Sensor: macOS Calendar via icalBuddy (preferred) or EventKit (pyobjc extra).

Primary path: subprocess icalBuddy and parse the plain-text output into
ActivityRecords. icalBuddy is NOT installed until Michael runs
`brew install ical-buddy` — fetch() must detect that and return [] with a
one-line warning, never crash.

Exact invocation used:
    icalBuddy -nc -b "" -iep "title,datetime,notes,attendees" \
        -df "%Y-%m-%d" eventsFrom:<date> to:<date>
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

from ..config import Config
from ..models import ActivityRecord

_ICALBUDDY = "icalBuddy"

# "attendees:", "notes:", "location:" style property labels icalBuddy emits.
_LABEL_RE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z ]*):\s*(?P<value>.*)$")

# A YYYY-MM-DD anywhere in a line (from -df "%Y-%m-%d").
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A clock time like "9:30 AM" or "14:05".
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?")


def _warn(msg: str) -> None:
    print(f"[calendar] {msg}", file=sys.stderr)


def _run_icalbuddy(date: str) -> str | None:
    """Run icalBuddy for `date`; return stdout, or None if unavailable."""
    if shutil.which(_ICALBUDDY) is None:
        _warn("icalBuddy not on PATH (brew install ical-buddy); returning no events")
        return None
    cmd = [
        _ICALBUDDY,
        "-nc",
        "-b",
        "",
        "-iep",
        "title,datetime,notes,attendees",
        "-df",
        "%Y-%m-%d",
        f"eventsFrom:{date}",
        f"to:{date}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _warn(f"icalBuddy failed to run: {exc}")
        return None
    if proc.returncode != 0:
        _warn(f"icalBuddy exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return None
    return proc.stdout


def _parse_datetime(value: str, date: str) -> tuple[str, str | None]:
    """Best-effort parse of an icalBuddy datetime line into (start, end).

    Handles ranges split on ' - '. Falls back to the day's date string when a
    precise timestamp can't be recovered. Never raises.
    """
    raw = value.strip()
    if not raw:
        return date, None

    parts = re.split(r"\s+-\s+", raw, maxsplit=1)
    start_part = parts[0].strip()
    end_part = parts[1].strip() if len(parts) > 1 else ""

    def to_iso(chunk: str, fallback_date: str) -> str:
        chunk = chunk.strip()
        if not chunk:
            return fallback_date
        m_date = _DATE_RE.search(chunk)
        d = m_date.group(0) if m_date else fallback_date
        m_time = _TIME_RE.search(chunk)
        if not m_time:
            # all-day or date-only
            return d
        t_raw = m_time.group(0).strip()
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                from datetime import datetime

                t = datetime.strptime(t_raw.upper().replace(" ", " "), fmt)
                return f"{d}T{t.hour:02d}:{t.minute:02d}:00"
            except ValueError:
                continue
        return d

    start_iso = to_iso(start_part, date)
    # An end chunk that carries only a time inherits the start's date.
    end_date_fallback = _DATE_RE.search(start_iso)
    end_fallback = end_date_fallback.group(0) if end_date_fallback else date
    end_iso = to_iso(end_part, end_fallback) if end_part else None
    return start_iso, end_iso


def _looks_like_datetime(value: str) -> bool:
    return bool(_DATE_RE.search(value) or _TIME_RE.search(value))


def _finalize(event: dict, date: str) -> ActivityRecord | None:
    """Turn an accumulated event dict into an ActivityRecord."""
    title = (event.get("title") or "").strip()
    if not title and not event.get("_lines"):
        return None

    dt_line = event.get("datetime", "")
    start, end = _parse_datetime(dt_line, date) if dt_line else (date, None)

    meta: dict = {}
    attendees = event.get("attendees", "").strip()
    if attendees:
        meta["attendees"] = [a.strip() for a in re.split(r"[,;]", attendees) if a.strip()]
    if event.get("location"):
        meta["location"] = event["location"].strip()

    return ActivityRecord(
        source="calendar",
        kind="calendar",
        start=start,
        end=end,
        app=None,
        title=title or None,
        text=(event.get("notes") or "").strip(),
        meta=meta,
    )


def _parse_output(text: str, date: str) -> list[ActivityRecord]:
    """Parse icalBuddy plain-text output into ActivityRecords, tolerantly.

    A non-indented, non-empty line starts a new event (its title). Indented
    lines carry labeled properties (notes:, attendees:, location:) or the
    unlabeled datetime line.
    """
    events: list[dict] = []
    current: dict | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indented = raw_line[0].isspace()
        line = raw_line.strip()

        if not indented:
            # New event title.
            if current is not None:
                events.append(current)
            current = {"title": line, "_lines": []}
            continue

        if current is None:
            # Stray indented line before any title; start a bare event.
            current = {"title": "", "_lines": []}

        current["_lines"].append(line)
        m = _LABEL_RE.match(line)
        if m:
            label = m.group("label").strip().lower()
            value = m.group("value").strip()
            if label in ("notes", "attendees", "location"):
                # Append in case a property spans lines.
                current[label] = (current.get(label, "") + " " + value).strip()
                continue
            if label == "datetime":
                current["datetime"] = value
                continue
        # Unlabeled line: most likely the datetime range.
        if "datetime" not in current and _looks_like_datetime(line):
            current["datetime"] = line
        else:
            # keep as fallback notes
            current["notes"] = (current.get("notes", "") + " " + line).strip()

    if current is not None:
        events.append(current)

    records = []
    for ev in events:
        rec = _finalize(ev, date)
        if rec is not None:
            records.append(rec)
    return records


def fetch(date: str, config: Config) -> list[ActivityRecord]:
    """Fetch calendar events for `date` (YYYY-MM-DD) as ActivityRecords
    (source="calendar", kind="calendar").

    Returns [] (with a one-line warning) when icalBuddy is missing.
    """
    out = _run_icalbuddy(date)
    if out is None:
        return []
    try:
        return _parse_output(out, date)
    except Exception as exc:  # parser must never crash the pipeline
        _warn(f"failed to parse icalBuddy output: {exc}")
        return []
