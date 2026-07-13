"""scoregoals.annotations — the structured-feedback channel from the audit page
to the checking agent.

The audit page (``scoregoals audit``) lets Michael annotate sessions, whole days,
and free-floating ideas with comments. Each comment is appended, verbatim and
append-only, to ``data/feedback/feedback.jsonl`` (under ``data/`` — gitignored).
An agent then ingests the *new* ones with ``scoregoals feedback --json
--new-only``, acts on them, and calls ``scoregoals feedback ack`` so processed
feedback stops resurfacing.

Entry shape (one JSON object per line)::

    {
      "ts": "2026-07-12T18:40:00-07:00",   # when the comment was filed (local ISO)
      "date": "2026-07-12",                # the day the comment is ABOUT
      "kind": "session" | "day" | "idea" | "frame",  # what it annotates
      "session_id": "6c66c14da1ef",        # present for kind=session and kind=frame
      "context": {                          # server-enriched, present for session/frame
        "app": "Claude", "title": "...", "span": "07:31-07:35",
        "minutes": 3.8, "verdict": "deep-work-coding", "source": "keyword"
      },
      "comment": "this was actually research, not coding",
      "status": "new" | "acked"
    }

A ``kind=frame`` entry is a comment on ONE specific screenshot. It carries the
extra keys ``frame_id`` (screenpipe's int frame id), ``frame_ts`` ("HH:MM:SS"),
the owning ``session_id``, and its ``context`` adds ``ocr_snippet`` (the first
~200 chars of that frame's redacted OCR text) so the agent has the exact visual
context::

    {
      "ts": "...", "date": "2026-07-12", "kind": "frame",
      "frame_id": 4821, "frame_ts": "08:14:53", "session_id": "db3a0d0ed69d",
      "context": {"app": "Claude", "title": "...", "span": "07:53-10:34",
        "verdict": "deep-work-coding", "source": "keyword",
        "ocr_snippet": "..."},
      "comment": "this exact screen is the bug", "status": "new"
    }

This module is stdlib-only and never raises on a normal call — a missing or
half-written store degrades to an empty aggregation.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import Config

KINDS = ("session", "day", "idea", "frame")


def _store_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "feedback" / "feedback.jsonl"


def _now_iso() -> str:
    """Local ISO-8601 with offset (e.g. 2026-07-12T18:40:00-07:00), second
    precision — the same convention labels.jsonl uses."""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _read_all(cfg: Config) -> list[dict]:
    """Every stored entry in file order (oldest first). Skips blank/corrupt
    lines rather than raising, so one bad line can't blind the channel."""
    path = _store_path(cfg)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _rewrite_all(cfg: Config, entries: list[dict]) -> None:
    """Atomically rewrite the whole store (used by ack). Writes to a temp file
    in the same dir, then os.replace()s it into place."""
    import os

    path = _store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in entries:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def enrich_context(cfg: Config, date: str, session_id: str) -> dict | None:
    """Look up a session in the day's resolved data and return the compact
    context we store alongside a session comment: ``{app, title, span, minutes,
    verdict, source}``. Returns None when the session can't be found (still a
    valid comment — it just carries no context)."""
    if not session_id:
        return None
    try:
        from . import audit as audit_mod

        day = audit_mod.build_day(cfg, date)
    except Exception:
        return None
    for s in day.get("sessions", []):
        sid = str(s.get("id") or "")
        if sid == session_id or sid.startswith(session_id):
            final = s.get("final") or {}
            return {
                "app": s.get("app"),
                "title": s.get("title"),
                "span": s.get("span"),
                "minutes": s.get("minutes"),
                "verdict": final.get("verdict_name") or final.get("verdict"),
                "source": final.get("source"),
            }
    return None


def enrich_frame_context(
    cfg: Config, date: str, frame_id: int, session_id: str | None = None
) -> dict | None:
    """Resolve everything a frame comment stores beyond the raw text:
    ``{frame_ts, session_id, context}`` where ``context`` is
    ``{app, title, span, verdict, source, ocr_snippet}``.

    The frame's local timestamp + OCR snippet come from screenpipe's db (via
    ``audit.frame_ocr``); the owning session is the one passed in, or — failing
    that — the session whose UTC span contains the frame. Returns None only if
    the audit module can't be imported (still a valid comment: it just carries
    less context)."""
    try:
        from . import audit as audit_mod
    except Exception:
        return None

    fo = audit_mod.frame_ocr(cfg, frame_id) or {}
    frame_ts = fo.get("frame_ts")
    ocr_snippet = fo.get("ocr_snippet") or ""
    futc = fo.get("utc")

    try:
        day = audit_mod.build_day(cfg, date)
    except Exception:
        day = {"sessions": []}
    sessions = day.get("sessions", [])

    sess = None
    if session_id:
        for s in sessions:
            sid = str(s.get("id") or "")
            if sid == session_id or sid.startswith(session_id) or session_id.startswith(sid):
                sess = s
                break
    if sess is None and futc:
        for s in sessions:
            lo = audit_mod._local_to_utc(s.get("start"))
            hi = audit_mod._local_to_utc(s.get("end") or s.get("start"))
            if lo and hi and lo <= futc <= hi + "~":
                sess = s
                break

    if sess is not None:
        final = sess.get("final") or {}
        context = {
            "app": sess.get("app"),
            "title": sess.get("title"),
            "span": sess.get("span"),
            "verdict": final.get("verdict_name") or final.get("verdict"),
            "source": final.get("source"),
            "ocr_snippet": ocr_snippet,
        }
        resolved_sid = str(sess.get("id") or "") or session_id
    else:
        context = {"ocr_snippet": ocr_snippet}
        resolved_sid = session_id

    return {"frame_ts": frame_ts, "session_id": resolved_sid, "context": context}


def frame_comment_counts(cfg: Config, date: str) -> dict[int, int]:
    """Per-frame comment counts for a day: ``{frame_id: count}`` over all stored
    frame comments (new + acked), so the audit grid can render 💬 badges in one
    pass instead of an N+1 lookup per thumbnail."""
    out: dict[int, int] = {}
    for e in _read_all(cfg):
        if e.get("kind") != "frame" or str(e.get("date")) != date:
            continue
        fid = e.get("frame_id")
        if fid is None:
            continue
        try:
            key = int(fid)
        except (TypeError, ValueError):
            continue
        out[key] = out.get(key, 0) + 1
    return out


def append_comment(
    cfg: Config,
    date: str,
    kind: str,
    comment: str,
    session_id: str | None = None,
    frame_id: int | None = None,
    context: dict | None = None,
    enrich: bool = True,
) -> dict:
    """Append one comment to the feedback store and return the stored entry.

    ``kind`` is one of session|day|idea|frame (anything else is coerced to
    "idea"). For a ``session`` comment, ``context`` is auto-enriched from the day
    data; for a ``frame`` comment, ``frame_ts`` + ``session_id`` + a context that
    includes ``ocr_snippet`` are auto-enriched — unless a context dict is passed
    or ``enrich=False``. Raises ValueError only on an empty comment — an
    agent-facing store should never carry blank entries."""
    text = (comment or "").strip()
    if not text:
        raise ValueError("comment is empty")
    k = kind if kind in KINDS else "idea"

    entry: dict = {"ts": _now_iso(), "date": date, "kind": k}
    if k == "session" and session_id:
        entry["session_id"] = str(session_id)
        ctx = context if context is not None else (
            enrich_context(cfg, date, str(session_id)) if enrich else None
        )
        if ctx:
            entry["context"] = ctx
    elif k == "frame" and frame_id is not None:
        entry["frame_id"] = int(frame_id)
        fe = None
        if context is None and enrich:
            fe = enrich_frame_context(cfg, date, int(frame_id), session_id=session_id)
        if fe:
            if fe.get("frame_ts"):
                entry["frame_ts"] = fe["frame_ts"]
            sid = fe.get("session_id") or session_id
            if sid:
                entry["session_id"] = str(sid)
            if fe.get("context"):
                entry["context"] = fe["context"]
        else:
            if session_id:
                entry["session_id"] = str(session_id)
            if context:
                entry["context"] = context
    entry["comment"] = text
    entry["status"] = "new"

    path = _store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def aggregate(cfg: Config, date: str | None = None, new_only: bool = False) -> dict:
    """The read side the checking agent consumes: ``{generated_at, count,
    new_count, entries:[...]}`` — entries newest first, optionally filtered to a
    date and/or to ``status == "new"``."""
    entries = _read_all(cfg)
    new_count = sum(1 for e in entries if e.get("status") == "new")
    sel = entries
    if date:
        sel = [e for e in sel if str(e.get("date")) == date]
    if new_only:
        sel = [e for e in sel if e.get("status") == "new"]
    sel = list(reversed(sel))  # newest first
    return {
        "generated_at": _now_iso(),
        "count": len(sel),
        "new_count": new_count,
        "entries": sel,
    }


def ack(cfg: Config, before: str | None = None) -> int:
    """Flip ``new`` → ``acked`` for every new entry (or, with ``before``, only
    entries whose ``ts`` is <= that ISO timestamp). Returns the number flipped.
    ISO-8601 timestamps compare correctly lexically when same-offset; we compare
    as strings, which is exact for this store's single-machine local stamps."""
    entries = _read_all(cfg)
    flipped = 0
    for e in entries:
        if e.get("status") != "new":
            continue
        if before is not None and str(e.get("ts") or "") > before:
            continue
        e["status"] = "acked"
        flipped += 1
    if flipped:
        _rewrite_all(cfg, entries)
    return flipped
