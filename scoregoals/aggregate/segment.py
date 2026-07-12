"""Segment raw ActivityRecords into contiguous Sessions."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from ..models import ActivityRecord, Session, session_id

# Screen-activity kinds that form sessions. Audio is meeting material and is
# routed to DayTimeline.meetings by timeline.build, never into sessions.
_SCREEN_KINDS = {"ocr", "window", "ui"}

# Records on the same app within this many seconds of each other merge.
_GAP_MERGE_S = 90.0
# A same-app-sandwiched foreign blip shorter than this folds into the
# surrounding session instead of splitting it.
_MICRO_FLIP_S = 30.0
# Concatenated OCR/text excerpt cap per session.
_EXCERPT_CHARS = 1500

# App-name substrings (lowercased) -> category.
_CODING_APPS = (
    "code", "cursor", "xcode", "terminal", "iterm", "warp", "alacritty",
    "kitty", "ghostty", "pycharm", "intellij", "webstorm", "goland",
    "sublime", "zed", "nova", "vim", "neovim", "emacs",
)
_COMMS_APPS = (
    "slack", "mail", "messages", "discord", "telegram", "whatsapp",
    "signal", "superhuman", "spark", "outlook",
)
_MEETING_APPS = ("zoom", "meet", "teams", "facetime", "webex", "around", "granola")
_BROWSER_APPS = ("chrome", "safari", "arc", "firefox", "brave", "edge", "orion", "vivaldi", "dia")
_DESIGN_APPS = ("figma", "sketch", "framer", "canva", "photoshop", "illustrator")
_RESEARCH_APPS = ("tradingview",)

# Browser-title substrings (lowercased) that flip browsing -> research.
_RESEARCH_TITLE_HINTS = (
    "docs.", "documentation", "readthedocs", "api reference", "developer.",
    "mdn", "stack overflow", "stackoverflow", "github", "arxiv", "wikipedia",
    "research", "paper", "tradingview", "python 3", "pypi",
)


def _parse_ts(ts: str | None) -> datetime | None:
    """ISO string -> naive local datetime (aware inputs are converted)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _categorize(app: str | None, title: str | None) -> str:
    a = (app or "").lower()
    t = (title or "").lower()
    if not a:
        return "other"
    if any(s in a for s in _RESEARCH_APPS):
        return "research"
    if any(s in a for s in _MEETING_APPS):
        return "meeting"
    if any(s in a for s in _DESIGN_APPS):
        return "design"
    if any(s in a for s in _COMMS_APPS):
        return "comms"
    if any(s in a for s in _CODING_APPS):
        return "coding"
    if any(s in a for s in _BROWSER_APPS):
        if any(h in t for h in _RESEARCH_TITLE_HINTS):
            return "research"
        return "browsing"
    return "other"


def _pick_title(items: list[tuple[datetime, datetime, ActivityRecord]]) -> str | None:
    """Most common non-empty window title; earliest-seen wins ties."""
    titles = [it[2].title for it in items if it[2].title]
    if not titles:
        return None
    counts = Counter(titles)
    first_seen: dict[str, int] = {}
    for i, t in enumerate(titles):
        first_seen.setdefault(t, i)
    return max(counts, key=lambda t: (counts[t], -first_seen[t]))


def _infer_project(items: list[tuple[datetime, datetime, ActivityRecord]],
                   title: str | None, category: str) -> str | None:
    """meta['project'] if any record carries one; else the trailing segment of
    an editor-style title like 'cli.py — scoregoals' for coding sessions."""
    for _, _, rec in items:
        proj = rec.meta.get("project") if isinstance(rec.meta, dict) else None
        if isinstance(proj, str) and proj:
            return proj
    if category == "coding" and title:
        for sep in (" — ", " – ", " - "):
            if sep in title:
                tail = title.rsplit(sep, 1)[1].strip()
                if tail and " " not in tail and "/" not in tail and len(tail) <= 40:
                    return tail
                break
    return None


def _excerpt(items: list[tuple[datetime, datetime, ActivityRecord]]) -> str:
    """Concatenate record text in time order, skipping consecutive duplicates
    (OCR frames repeat), truncated to ~_EXCERPT_CHARS chars."""
    parts: list[str] = []
    prev = None
    for _, _, rec in items:
        text = (rec.text or "").strip()
        if not text or text == prev:
            continue
        parts.append(text)
        prev = text
        if sum(len(p) + 1 for p in parts) >= _EXCERPT_CHARS:
            break
    out = "\n".join(parts)
    if len(out) > _EXCERPT_CHARS:
        out = out[: _EXCERPT_CHARS - 1] + "…"
    return out


def _group_dur_s(g: dict) -> float:
    return (g["end"] - g["start"]).total_seconds()


def segment(records: list[ActivityRecord]) -> list[Session]:
    """Collapse time-ordered screen records into Sessions.

    Deterministic algorithm:
    1. keep ocr/window/ui records with a parseable start; sort by (start, end)
    2. group contiguously: same app AND gap from the previous record's end
       <= 90s stays in the group; an app change or a larger gap starts a new
       one (so idle gaps > the threshold simply become session boundaries —
       no idle Sessions are emitted)
    3. fold micro-flips: a group < 30s sandwiched between two groups of the
       same (different) app merges into the surrounding session
    4. per group fill start/end/minutes, app, most-common title, category
       heuristic, project inference, text excerpt (~1500 chars),
       record_count; summary stays None for the LLM to fill later
    """
    items: list[tuple[datetime, datetime, ActivityRecord]] = []
    for rec in records:
        if rec.kind not in _SCREEN_KINDS:
            continue
        start = _parse_ts(rec.start)
        if start is None:
            continue
        end = _parse_ts(rec.end)
        if end is None or end < start:
            end = start
        items.append((start, end, rec))
    items.sort(key=lambda it: (it[0], it[1]))

    # Pass 1: contiguous same-app grouping.
    groups: list[dict] = []
    for start, end, rec in items:
        app = rec.app or ""
        if groups:
            g = groups[-1]
            if g["app"] == app and (start - g["end"]).total_seconds() <= _GAP_MERGE_S:
                g["items"].append((start, end, rec))
                g["end"] = max(g["end"], end)
                continue
        groups.append({"app": app, "start": start, "end": end,
                       "items": [(start, end, rec)]})

    # Pass 2: absorb micro-flips (A B A, with B < 30s) into the A session.
    i = 1
    while i < len(groups) - 1:
        prev, cur, nxt = groups[i - 1], groups[i], groups[i + 1]
        if (
            _group_dur_s(cur) < _MICRO_FLIP_S
            and prev["app"] == nxt["app"]
            and cur["app"] != prev["app"]
            and (nxt["start"] - prev["end"]).total_seconds()
            <= _GAP_MERGE_S + _MICRO_FLIP_S
        ):
            prev["items"].extend(cur["items"])
            prev["items"].extend(nxt["items"])
            prev["end"] = max(prev["end"], nxt["end"])
            del groups[i : i + 2]
        else:
            i += 1

    # Pass 3: groups -> Sessions.
    sessions: list[Session] = []
    for g in groups:
        app = g["app"] or None
        title = _pick_title(g["items"])
        category = _categorize(app, title)
        start_iso = g["start"].isoformat(timespec="seconds")
        sessions.append(
            Session(
                id=session_id(start_iso[:10], start_iso, app),
                start=start_iso,
                end=g["end"].isoformat(timespec="seconds"),
                app=app,
                title=title,
                project=_infer_project(g["items"], title, category),
                topic=None,
                category=category,
                summary=None,
                minutes=round(_group_dur_s(g) / 60.0, 1),
                text_excerpt=_excerpt(g["items"]),
                record_count=len(g["items"]),
            )
        )
    return sessions
