"""dayloop.store.db — FROZEN sqlite + JSON-file persistence.

Single sqlite database at <data_dir>/dayloop.db, plus human-readable JSON
copies under data/timeline/ and data/reports/. Stdlib-only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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


def load_timeline(config: Config, date: str) -> DayTimeline | None:
    """Load a DayTimeline for `date` (sqlite first, JSON file fallback).

    Returns None when nothing has been captured for that date.
    """
    with connect(config) as conn:
        row = conn.execute("SELECT json FROM timelines WHERE date = ?", (date,)).fetchone()
    if row:
        return timeline_from_json(row[0])
    json_path = Path(config.timeline_dir) / f"{date}.json"
    if json_path.is_file():
        return timeline_from_json(json_path.read_text(encoding="utf-8"))
    return None


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
