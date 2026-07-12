"""Sensor: screenpipe — screen OCR, accessibility (UI) text, meeting audio.

Talks to the local screenpipe HTTP API at config.screenpipe_url (default
http://localhost:3030), e.g. GET /search with content_type=ocr|audio|ui and a
start/end window, paginated.

Implementation rules (HARD, see GOAL.md):
- `import requests` lazily INSIDE fetch(), never at module top
- unreachable/missing screenpipe -> print one-line warning to stderr, return []
- map every hit to ActivityRecord(source="screenpipe",
  kind in {"ocr","audio","window","ui"}, ISO start/end, app/title/text/meta)
"""

from __future__ import annotations

import sys

from ..config import Config
from ..models import ActivityRecord

# screenpipe content_type -> the ActivityRecord.kind we normalize to.
_CONTENT_TYPES: dict[str, str] = {
    "ocr": "ocr",
    "audio": "audio",
    "ui": "ui",
}

_PAGE_LIMIT = 1000

# Cached result of `screenpipe auth token` for this process ("" = probed, none).
_AUTO_TOKEN: str | None = None


def _warn(msg: str) -> None:
    print(f"[screenpipe] {msg}", file=sys.stderr)


def _resolve_token(config: Config) -> str | None:
    """Bearer token for the screenpipe API (required since CLI ~v0.4).

    Precedence: config.screenpipe_api_key (env/settings/toml, resolved by
    config.py) > `screenpipe auth token` (auto, cached per process). Returns
    None when neither is available — the API will then 401 and we warn.
    """
    key = getattr(config, "screenpipe_api_key", None)
    if key:
        return str(key)

    global _AUTO_TOKEN
    if _AUTO_TOKEN is not None:
        return _AUTO_TOKEN or None

    import shutil
    import subprocess

    _AUTO_TOKEN = ""
    exe = shutil.which("screenpipe")
    if exe:
        try:
            out = subprocess.run(
                [exe, "auth", "token"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                token = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
                if token:
                    _AUTO_TOKEN = token
        except (OSError, subprocess.SubprocessError):
            pass
    return _AUTO_TOKEN or None


def _pick(d: dict, *keys: str, default=None):
    """Return the first present, non-None value among keys in dict `d`."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _record_from_hit(hit: dict, kind: str) -> ActivityRecord | None:
    """Normalize one screenpipe search hit into an ActivityRecord.

    The API varies by version, so every field access is defensive. The hit is
    typically {"type": "OCR", "content": {...}}.
    """
    if not isinstance(hit, dict):
        return None
    content = hit.get("content")
    if not isinstance(content, dict):
        # some versions may inline the fields directly on the hit
        content = hit

    start = _pick(content, "timestamp", "start_time", "time", "created_at", default="")
    end = _pick(content, "end_time", "endTime")
    app = _pick(content, "app_name", "appName", "app")
    title = _pick(content, "window_name", "windowName", "title", "window")

    meta: dict = {}
    frame_id = _pick(content, "frame_id", "frameId")
    if frame_id is not None:
        meta["frame_id"] = frame_id

    if kind == "ocr":
        text = _pick(content, "text", "ocr_text", "content", default="") or ""
    elif kind == "audio":
        text = _pick(content, "transcription", "text", "transcript", default="") or ""
        speaker = _pick(content, "speaker", "speaker_id", "speakerId", "device_name", "device")
        if speaker is not None:
            meta["speaker"] = speaker
        # audio events usually have no window/app context
        if app is None:
            app = _pick(content, "device_name", "device")
    else:  # ui / window accessibility text
        text = _pick(content, "text", "ui_text", "content", default="") or ""

    return ActivityRecord(
        source="screenpipe",
        kind=kind,
        start=str(start) if start is not None else "",
        end=str(end) if end is not None else None,
        app=str(app) if app is not None else None,
        title=str(title) if title is not None else None,
        text=str(text),
        meta=meta,
    )


def _fetch_content_type(
    session, base_url: str, content_type: str, start_iso: str, end_iso: str
) -> list[ActivityRecord]:
    """Fetch and paginate one content_type; requests session passed in."""
    kind = _CONTENT_TYPES[content_type]
    records: list[ActivityRecord] = []
    offset = 0
    while True:
        params = {
            "content_type": content_type,
            "start_time": start_iso,
            "end_time": end_iso,
            "limit": _PAGE_LIMIT,
            "offset": offset,
        }
        resp = session.get(f"{base_url}/search", params=params, timeout=15)
        if resp.status_code == 401:
            raise PermissionError(
                "screenpipe API requires auth — set SCREENPIPE_API_KEY or run"
                " `screenpipe auth token` (auto-detection failed)"
            )
        resp.raise_for_status()
        payload = resp.json()

        if isinstance(payload, dict):
            data = payload.get("data")
            if data is None:
                data = payload.get("results", [])
            pagination = payload.get("pagination", {}) or {}
        elif isinstance(payload, list):
            data = payload
            pagination = {}
        else:
            data = []
            pagination = {}

        if not isinstance(data, list):
            data = []

        for hit in data:
            rec = _record_from_hit(hit, kind)
            if rec is not None:
                records.append(rec)

        got = len(data)
        # Stop when the page came back short, or pagination says no more.
        total = pagination.get("total")
        if got < _PAGE_LIMIT:
            break
        offset += got
        if isinstance(total, int) and offset >= total:
            break
        # Safety valve against a misbehaving server that never shrinks pages.
        if offset > 200_000:
            _warn("pagination exceeded 200k rows; stopping early")
            break

    return records


def fetch(start_iso: str, end_iso: str, config: Config) -> list[ActivityRecord]:
    """Fetch screenpipe records in [start_iso, end_iso) as ActivityRecords.

    Returns [] (with a one-line warning) when screenpipe is not running.
    """
    try:
        import requests  # lazy, per HARD rules
    except ImportError:
        _warn("requests not installed; returning no records")
        return []

    base_url = config.screenpipe_url.rstrip("/")
    records: list[ActivityRecord] = []

    token = _resolve_token(config)

    try:
        with requests.Session() as session:
            if token:
                session.headers["Authorization"] = f"Bearer {token}"
            for content_type in _CONTENT_TYPES:
                try:
                    records.extend(
                        _fetch_content_type(
                            session, base_url, content_type, start_iso, end_iso
                        )
                    )
                except PermissionError as exc:
                    # Auth missing/rejected: applies to every content_type.
                    _warn(str(exc))
                    break
                except requests.exceptions.ConnectionError:
                    # Server down: one concise line, stop probing the rest.
                    _warn(
                        f"not reachable at {base_url} — is screenpipe running?"
                        " returning no records (mock mode works without it)"
                    )
                    break
                except requests.exceptions.RequestException as exc:
                    # One content_type failing shouldn't sink the others.
                    _warn(f"query {content_type} failed: {exc.__class__.__name__}: {str(exc)[:120]}")
                except ValueError as exc:  # bad JSON
                    _warn(f"query {content_type} returned invalid JSON: {exc}")
    except requests.exceptions.RequestException as exc:
        _warn(f"unreachable at {base_url}: {exc}")
        return []

    return records
