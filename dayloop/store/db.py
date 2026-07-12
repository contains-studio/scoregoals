"""dayloop.store.db — FROZEN sqlite + JSON-file persistence.

Single sqlite database at <data_dir>/dayloop.db, plus human-readable JSON
copies under data/timeline/ and data/reports/. Stdlib-only.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..models import DayTimeline, Report, iso_now, timeline_from_json, to_json

__all__ = ["connect", "save_timeline", "load_timeline", "save_report", "save_benchmark"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS timelines (
    date         TEXT PRIMARY KEY,
    json         TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    backend      TEXT NOT NULL,
    model        TEXT NOT NULL,
    json         TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    backend       TEXT NOT NULL,
    model         TEXT NOT NULL,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    latency_s     REAL NOT NULL DEFAULT 0,
    overall_score INTEGER NOT NULL DEFAULT 0,
    generated_at  TEXT NOT NULL
);
"""


@contextmanager
def connect(config: Config) -> Iterator[sqlite3.Connection]:
    """Context-managed sqlite connection: ensures schema, commits on success,
    rolls back on error, always closes."""
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path, timeout=10)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_timeline(config: Config, timeline: DayTimeline) -> str:
    """Persist a DayTimeline to sqlite and data/timeline/<date>.json.

    Returns the JSON file path (the human-readable copy).
    """
    payload = to_json(timeline)
    json_path = Path(config.timeline_dir) / f"{timeline.date}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    with connect(config) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO timelines (date, json, generated_at) VALUES (?, ?, ?)",
            (timeline.date, payload, timeline.generated_at or iso_now()),
        )
    return str(json_path)


def _gen_dt(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 generated_at into an aware datetime for comparison.

    Naive strings (e.g. a hand-written "2026-07-09T23:59:00") are treated as
    local time. Junk yields None (treated as "unknown / oldest")."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def load_timeline(config: Config, date: str) -> DayTimeline | None:
    """Load a DayTimeline for `date`, reconciling the two stores.

    dayloop keeps every timeline in BOTH sqlite (the `timelines` row) and a
    human-readable data/timeline/<date>.json file. These can drift out of sync
    (e.g. a test overwrites one but not the other). To avoid ever serving stale
    data, this loads whichever copy exists and — when BOTH exist and disagree —
    keeps the one with the newer `generated_at`, then write-through resyncs the
    stale store so they converge. A one-line note goes to stderr on a heal.

    Returns None when nothing has been captured for that date. The public
    signature is unchanged.
    """
    with connect(config) as conn:
        row = conn.execute(
            "SELECT json, generated_at FROM timelines WHERE date = ?", (date,)
        ).fetchone()
    db_text = row[0] if row else None
    db_gen = row[1] if row else None

    json_path = Path(config.timeline_dir) / f"{date}.json"
    file_text: str | None = None
    if json_path.is_file():
        try:
            file_text = json_path.read_text(encoding="utf-8")
        except OSError as exc:  # unreadable file — fall back to the DB copy
            print(f"dayloop: warning: could not read {json_path} ({exc})", file=sys.stderr)

    # Only one (or neither) store has this date — nothing to reconcile.
    if db_text is None and file_text is None:
        return None
    if db_text is None:
        return timeline_from_json(file_text)
    if file_text is None:
        return timeline_from_json(db_text)

    # Both exist. Compare normalized JSON (ignores the file's trailing newline
    # and any key ordering); identical copies need no heal.
    db_tl = timeline_from_json(db_text)
    file_tl = timeline_from_json(file_text)
    if to_json(db_tl) == to_json(file_tl):
        return file_tl

    # Diverged — keep the newer generated_at (prefer the DB column, falling back
    # to the embedded value). On a tie or unknown timestamps, prefer the file
    # (the last-written human-readable copy).
    db_dt = _gen_dt(db_gen) or _gen_dt(db_tl.generated_at)
    file_dt = _gen_dt(file_tl.generated_at)
    if db_dt is not None and (file_dt is None or db_dt > file_dt):
        chosen, chosen_src, stale_src = db_tl, "db", "file"
    else:
        chosen, chosen_src, stale_src = file_tl, "file", "db"

    payload = to_json(chosen)
    try:
        if stale_src == "file":
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(payload + "\n", encoding="utf-8")
        else:
            with connect(config) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO timelines (date, json, generated_at)"
                    " VALUES (?, ?, ?)",
                    (date, payload, chosen.generated_at or iso_now()),
                )
    except Exception as exc:  # a failed heal must not break the read
        print(f"dayloop: warning: timeline heal write failed for {date} ({exc})", file=sys.stderr)

    print(
        f"dayloop: timeline divergence for {date} healed — kept newer {chosen_src} copy"
        f" (db generated_at={db_tl.generated_at!r}, file generated_at={file_tl.generated_at!r});"
        f" resynced {stale_src}",
        file=sys.stderr,
    )
    return chosen


def save_report(config: Config, report: Report) -> str:
    """Persist a Report to sqlite and data/reports/<date>-<kind>-<backend>.json.

    Returns the JSON file path.
    """
    payload = to_json(report)
    json_path = Path(config.reports_dir) / f"{report.date}-{report.kind}-{report.backend}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(payload + "\n", encoding="utf-8")
    with connect(config) as conn:
        conn.execute(
            "INSERT INTO reports (date, kind, backend, model, json, generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                report.date,
                report.kind,
                report.backend,
                report.model,
                payload,
                report.generated_at or iso_now(),
            ),
        )
    return str(json_path)


def save_benchmark(config: Config, report: Report) -> None:
    """Record one benchmark row (cost/latency/quality) extracted from a Report."""
    with connect(config) as conn:
        conn.execute(
            "INSERT INTO benchmarks (date, backend, model, tokens_in, tokens_out,"
            " cost_usd, latency_s, overall_score, generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.date,
                report.backend,
                report.model,
                report.tokens_in,
                report.tokens_out,
                report.cost_usd,
                report.latency_s,
                report.overall_score,
                report.generated_at or iso_now(),
            ),
        )
