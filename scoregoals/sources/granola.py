"""Sensor: Granola meeting notes (best-effort, optional).

Uses the official Granola public API and ONLY when a token is configured
(config.granola_api_key or env GRANOLA_API_KEY):

    GET https://public-api.granola.ai/v1/notes?created_after=<date>T00:00:00Z
    GET https://public-api.granola.ai/v1/notes/<id>?include=transcript

Each note -> ActivityRecord(source="granola", kind="granola",
title=<note title>, text=<summary + transcript>, start=<created_at>).

If no key is set, return [] immediately with a one-line info log. We do NOT
read/decrypt the local Granola cache — it is encrypted and brittle. Network
only inside fetch(); requests imported lazily; every call has a timeout and is
wrapped defensively so the pipeline never crashes.
"""

from __future__ import annotations

import os
import sys

from ..config import Config
from ..models import ActivityRecord

_API_BASE = "https://public-api.granola.ai/v1"


def _log(msg: str) -> None:
    print(f"[granola] {msg}", file=sys.stderr)


def _api_key(config: Config) -> str | None:
    key = getattr(config, "granola_api_key", None) or os.environ.get("GRANOLA_API_KEY")
    key = (key or "").strip()
    return key or None


def _note_text(note: dict) -> str:
    """Assemble summary + transcript into one text blob, tolerating shapes."""
    parts: list[str] = []
    summary = note.get("summary") or note.get("notes") or note.get("body")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())

    transcript = note.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        parts.append(transcript.strip())
    elif isinstance(transcript, list):
        segs: list[str] = []
        for seg in transcript:
            if isinstance(seg, dict):
                txt = seg.get("text") or seg.get("content") or ""
                speaker = seg.get("speaker") or seg.get("source")
                if txt:
                    segs.append(f"{speaker}: {txt}" if speaker else str(txt))
            elif isinstance(seg, str):
                segs.append(seg)
        if segs:
            parts.append("\n".join(segs))
    return "\n\n".join(parts)


def fetch(date: str, config: Config) -> list[ActivityRecord]:
    """Fetch Granola meeting notes for `date` (YYYY-MM-DD) as ActivityRecords.

    Returns [] (with a one-line log) when no API key is configured or the API
    is unreachable.
    """
    key = _api_key(config)
    if key is None:
        _log("no GRANOLA_API_KEY / config.granola_api_key set; skipping Granola")
        return []

    try:
        import requests  # lazy: keep core import dependency-light
    except ImportError:
        _log("requests not installed; skipping Granola")
        return []

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    created_after = f"{date}T00:00:00Z"

    try:
        resp = requests.get(
            f"{_API_BASE}/notes",
            params={"created_after": created_after},
            headers=headers, timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — degrade gracefully on any failure
        _log(f"could not list notes: {exc}")
        return []

    if isinstance(payload, dict):
        notes = payload.get("notes") or payload.get("data") or []
    elif isinstance(payload, list):
        notes = payload
    else:
        notes = []

    records: list[ActivityRecord] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = str(note.get("id") or note.get("note_id") or "")
        created = str(note.get("created_at") or note.get("created") or created_after)
        # Only keep notes actually created on `date`.
        if not created.startswith(date):
            continue
        title = note.get("title") or note.get("name") or "Granola note"

        detail = note
        if note_id:
            try:
                dresp = requests.get(
                    f"{_API_BASE}/notes/{note_id}",
                    params={"include": "transcript"},
                    headers=headers, timeout=20,
                )
                dresp.raise_for_status()
                dpayload = dresp.json()
                if isinstance(dpayload, dict):
                    detail = dpayload.get("note") or dpayload
            except Exception as exc:  # noqa: BLE001
                _log(f"could not fetch transcript for {note_id}: {exc}")

        records.append(ActivityRecord(
            source="granola", kind="granola",
            start=created, end=note.get("ended_at") or note.get("end") or None,
            app="Granola", title=str(title),
            text=_note_text(detail),
            meta={"note_id": note_id, "url": note.get("url") or note.get("share_url")},
        ))
    return records
