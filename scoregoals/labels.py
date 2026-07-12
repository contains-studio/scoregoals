"""scoregoals.labels — the append-only correction store.

Corrections are sacred (see docs/PLAN-experience-and-learning.md): whenever
Michael reassigns a session to a goal, marks it off-track, or marks it not-work,
one JSON line is appended to ``data/labels.jsonl``. Labels are the highest
authority signal in align.py and the raw material rule mining (learn.py) learns
from. Nothing here rewrites or deletes an existing line — the file is the
archive.

Line schema (one JSON object per line):

    {
      "ts": "2026-07-11T21:50:00-07:00",   # when the correction was made
      "session_id": "ab12cd34ef56",         # models.session_id(...)
      "date": "2026-07-11",                 # the session's day
      "fingerprint": {
        "app": "Code",
        "title_tokens": ["cli", "scoregoals"],
        "text_keywords": ["argparse", "doctor", "sqlite"],
        "hour_bucket": 9                     # 0-23, hour the session started
      },
      "verdict": "ship-scoregoals" | "off_track" | "not_work",
      "source": "user" | "implicit"
    }

`verdict` is a goal id, ``"off_track"`` (worked, but on no goal), or
``"not_work"`` (out of scope — excluded from active minutes entirely). The
loader tolerates malformed lines: a bad line is skipped with a one-line stderr
warning, never crashing the read.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .config import Config
from .models import Session, iso_now, session_id

__all__ = [
    "OFF_TRACK",
    "NOT_WORK",
    "VALID_SOURCES",
    "LABELS_FILENAME",
    "labels_path",
    "fingerprint_for_session",
    "session_id_for",
    "record_label",
    "load_labels",
    "labels_by_session",
    "labels_by_fingerprint",
    "match_label_by_fingerprint",
    "corrections_in_week",
    "corrections_by_week",
]

OFF_TRACK = "off_track"
NOT_WORK = "not_work"
SPECIAL_VERDICTS = frozenset({OFF_TRACK, NOT_WORK})
VALID_SOURCES = frozenset({"user", "implicit"})
LABELS_FILENAME = "labels.jsonl"

_MIN_TOKEN_LEN = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "you", "your", "this", "that", "from",
        "new", "tab", "app", "com", "www", "http", "https", "org", "net",
        "inbox", "untitled", "window", "home", "page", "are", "was", "not",
        "its", "our", "their", "about", "via",
    }
)


def _warn(msg: str) -> None:
    print(f"[scoregoals.labels] warning: {msg}", file=sys.stderr)


def labels_path(config: Config) -> Path:
    return Path(config.data_dir) / LABELS_FILENAME


def _tokens(text: str | None, limit: int) -> list[str]:
    """Lowercased, de-duplicated, stopword-filtered tokens (>= min len), in
    first-seen order, capped at `limit`."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) < _MIN_TOKEN_LEN or tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _hour_bucket(start: str | None) -> int:
    """Hour (0-23) of an ISO start string; -1 when unparseable."""
    m = re.search(r"T(\d{2}):", str(start or ""))
    if m:
        try:
            return max(0, min(23, int(m.group(1))))
        except ValueError:
            return -1
    return -1


def session_id_for(session: Session, date: str | None = None) -> str:
    """The session's own id if it carries one, else a freshly computed stable id
    (segmentation sets ids, but timelines captured before ids existed lack them —
    this keeps review/label working on that historical data)."""
    sid = getattr(session, "id", "") or ""
    if sid:
        return sid
    start = getattr(session, "start", "") or ""
    d = date or (start[:10] if len(start) >= 10 else "")
    return session_id(d, start, getattr(session, "app", None))


def fingerprint_for_session(session: Session) -> dict:
    """Deterministic fingerprint stored with a label — the pattern learn.py
    mines: app, title tokens, a few text keywords, and the start hour bucket."""
    return {
        "app": session.app or "",
        "title_tokens": _tokens(session.title, 6),
        "text_keywords": _tokens(session.text_excerpt, 8),
        "hour_bucket": _hour_bucket(session.start),
    }


def record_label(
    config: Config,
    session_id: str,
    date: str,
    fingerprint: dict,
    verdict: str,
    source: str = "user",
) -> dict:
    """Append one correction to data/labels.jsonl and return the written record.

    Append-only: existing lines are never touched. `verdict` should be a goal id,
    OFF_TRACK, or NOT_WORK; `source` one of VALID_SOURCES.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"unknown label source {source!r}")
    record = {
        "ts": iso_now(),
        "session_id": session_id,
        "date": date,
        "fingerprint": fingerprint,
        "verdict": verdict,
        "source": source,
    }
    path = labels_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _valid(rec: object) -> bool:
    if not isinstance(rec, dict):
        return False
    if not rec.get("session_id") or not rec.get("verdict"):
        return False
    if rec.get("source") not in VALID_SOURCES:
        return False
    return True


def load_labels(config: Config) -> list[dict]:
    """Every well-formed label line, oldest first. Malformed lines (bad JSON,
    not an object, missing session_id/verdict, bad source) are skipped with a
    one-line stderr warning so one corrupt line can't break the read."""
    path = labels_path(config)
    if not path.is_file():
        return []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _warn(f"could not read {path} ({exc}); using no labels")
        return []
    out: list[dict] = []
    for lineno, line in enumerate(raw_lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            _warn(f"skipping {path.name}:{lineno}: bad JSON ({exc})")
            continue
        if not _valid(rec):
            _warn(f"skipping {path.name}:{lineno}: incomplete label")
            continue
        out.append(rec)
    return out


def labels_by_session(config: Config, labels: list[dict] | None = None) -> dict[str, dict]:
    """Latest label per session_id (last line wins — newer corrections
    supersede older ones for the same session)."""
    records = load_labels(config) if labels is None else labels
    latest: dict[str, dict] = {}
    for rec in records:
        latest[str(rec["session_id"])] = rec
    return latest


def labels_by_fingerprint(
    config: Config, labels: list[dict] | None = None
) -> dict[tuple[str, int], list[dict]]:
    """Index labels by (app_lower, hour_bucket) for FALLBACK matching.

    A session's stable id is sha1(date|start|app) — but segmentation can re-run
    within a day (gap-bridge / micro-flip) and shift a session's `start`, which
    jitters the id. When that happens a stored label no longer matches by id and
    would silently orphan. This index lets align.py fall back to a fingerprint
    match (same app + hour + overlapping title tokens). Records are kept in
    first-seen (oldest) order per bucket so the latest qualifying label wins.
    """
    records = load_labels(config) if labels is None else labels
    index: dict[tuple[str, int], list[dict]] = {}
    for rec in records:
        fp = rec.get("fingerprint") if isinstance(rec, dict) else None
        if not isinstance(fp, dict):
            continue
        app = str(fp.get("app") or "").strip().lower()
        hb = fp.get("hour_bucket")
        if not app or not isinstance(hb, int) or hb < 0:
            continue
        index.setdefault((app, hb), []).append(rec)
    return index


def match_label_by_fingerprint(
    session: Session, fp_index: dict[tuple[str, int], list[dict]] | None
) -> dict | None:
    """Best label for `session` by fingerprint, or None. Fallback only — used
    when the session_id doesn't match a stored label (see labels_by_fingerprint).

    Match rule (as specced): same app (case-insensitive) AND same start-hour
    bucket AND title-token overlap — or, for windowless sessions, both token
    sets empty (app+hour is then the whole signal). The latest (last-appended)
    qualifying label in the bucket wins, mirroring last-line-wins for ids.
    """
    if not fp_index:
        return None
    fp = fingerprint_for_session(session)
    app = str(fp.get("app") or "").strip().lower()
    hb = fp.get("hour_bucket")
    if not app or not isinstance(hb, int) or hb < 0:
        return None
    candidates = fp_index.get((app, hb))
    if not candidates:
        return None
    stoks = set(fp.get("title_tokens") or [])
    best: dict | None = None
    for rec in candidates:  # oldest-first: keep overwriting so the latest wins
        rfp = rec.get("fingerprint") if isinstance(rec, dict) else None
        rtoks = set((rfp or {}).get("title_tokens") or [])
        if (stoks & rtoks) or (not stoks and not rtoks):
            best = rec
    return best


def _label_day(rec: dict):
    from datetime import date as _date
    from datetime import datetime as _dt

    d = str(rec.get("date") or "")
    try:
        return _date.fromisoformat(d)
    except ValueError:
        try:
            return _dt.fromisoformat(str(rec.get("ts") or "").replace("Z", "+00:00")).date()
        except ValueError:
            return None


def corrections_in_week(labels: list[dict], end_date: str) -> int:
    """Count of user corrections in the 7-day window ending `end_date`."""
    from datetime import date as _date
    from datetime import timedelta

    try:
        end = _date.fromisoformat(end_date)
    except ValueError:
        end = _date.today()
    start = end - timedelta(days=6)
    n = 0
    for rec in labels:
        if rec.get("source") != "user":
            continue
        d = _label_day(rec)
        if d is not None and start <= d <= end:
            n += 1
    return n


def corrections_by_week(labels: list[dict]) -> list[dict]:
    """User corrections grouped by ISO week: [{"week": "2026-W28", "count": N}],
    oldest week first. The learning KPI (trend toward zero) is read from this."""
    counts: dict[tuple[int, int], int] = {}
    for rec in labels:
        if rec.get("source") != "user":
            continue
        d = _label_day(rec)
        if d is None:
            continue
        iso = d.isocalendar()
        counts[(iso[0], iso[1])] = counts.get((iso[0], iso[1]), 0) + 1
    return [
        {"week": f"{y}-W{w:02d}", "count": counts[(y, w)]}
        for (y, w) in sorted(counts)
    ]
