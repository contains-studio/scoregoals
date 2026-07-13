"""Goal alignment: parse goals.md, map session time onto goals, score the day.

Fully deterministic and dependency-light (stdlib only, no LLM calls) so the
alignment numbers are reproducible — the Gemini-vs-Ollama comparison should
only differ in narrative, never in the underlying math.

goals.md format:
    ## Goal: <name>
    keywords: a, b, c
    target_pct: 30            (optional)
    archived: true            (optional — retires the goal, see below)
    <free-text description paragraph(s)>

    ## Project: <name>
    keywords: a, b, c
    archived: true            (optional)
    <free-text description paragraph(s)>

A ``## Project:`` section is TRACKED but never JUDGED: it carries the same
fields as a goal (minus target_pct — a project has no target) and its keywords
participate in resolution exactly like a goal's, so a session about the project
resolves to the project id instead of "unaligned". But project time is excluded
from the unaligned share and from overall_score — it is accounted, not scored.
Each parsed Goal carries ``.kind`` ("goal" | "project").

Archived goals/projects are parsed but excluded from alignment/targets/drift by
default; load_goals(include_archived=True) returns them too (each Goal carries
.archived). ``load_goals`` returns BOTH kinds — callers that want only scored
goals filter on ``.kind`` (or use ``only_goals`` / ``load_projects``).

Matching strategy (see align()):
    Each Session is assigned to AT MOST ONE goal. A goal matches a session
    when any of its keywords appears (case-insensitive, whole-word-ish —
    bounded by non-alphanumerics) in the session's app, title, project,
    topic, category, summary, or text_excerpt. If several goals match, the
    goal with the most distinct keyword hits wins; ties break by goals.md
    order. Sessions matching no goal accrue to an implicit "unaligned"
    pseudo-goal, which is always included in the returned alignments.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from ..config import Config
from ..models import DayTimeline, Goal, GoalAlignment, Session

UNALIGNED_ID = "unaligned"
UNALIGNED_NAME = "Unaligned"

# on_track tolerance: within 70% of target counts as on track for the day.
_ON_TRACK_FACTOR = 0.7

# drift thresholds
_UNALIGNED_FLAG_PCT = 25.0     # flag when unaligned share exceeds this
_BROWSING_FLAG_MIN = 90.0      # flag when a "leisure-ish" category exceeds this many minutes
_FLAGGED_CATEGORIES = ("browsing", "idle")


def _warn(msg: str) -> None:
    print(f"[scoregoals.align] warning: {msg}", file=sys.stderr)


def _slug(name: str) -> str:
    """Stable id: lowercase, runs of non-alphanumerics -> single hyphen."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "goal"


_HEADING_RE = re.compile(r"^##\s*(Goal|Project)\s*:\s*(.+?)\s*$", re.IGNORECASE)


def load_goals(config: Config, include_archived: bool = False) -> list[Goal]:
    """Parse config.goals_path (goals.md) into Goals AND Projects.

    Recognized inside each "## Goal: <name>" / "## Project: <name>" section:
      - "keywords: a, b, c"  -> lowercased, stripped, comma-split
      - "target_pct: 30"     -> float (GOALS only; ignored with a warning on a
                                project, which has no target)
      - "archived: true"     -> bool; archived goals/projects are retired
      - any other non-heading text -> appended to the description
    id is a slug of the name; duplicate slugs (across BOTH kinds) get -2, -3, …
    suffixes, so a goal and a project can never collide on id.
    Missing/unreadable file -> one-line warning + [] (pipeline continues).

    Returns BOTH kinds (each Goal carries ``.kind`` == "goal" | "project"):
    callers that score only target-bearing goals filter with ``only_goals`` (or
    ``g.kind == "goal"``); ``load_projects`` is the project-only convenience.

    By default only ACTIVE goals/projects are returned (archived ones are
    excluded from alignment/targets/drift). Pass include_archived=True to get
    every one with its `.archived` flag set (the `goals --json` editing surface).
    """
    try:
        with open(config.goals_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _warn(f"goals file not readable ({config.goals_path}): {exc}")
        return []

    goals: list[Goal] = []
    seen_ids: set[str] = set()
    current: Goal | None = None
    desc_lines: list[str] = []

    def _finish() -> None:
        nonlocal current, desc_lines
        if current is not None:
            current.description = "\n".join(desc_lines).strip()
            goals.append(current)
        current, desc_lines = None, []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _HEADING_RE.match(line)
        if m:
            _finish()
            kind = "project" if m.group(1).lower() == "project" else "goal"
            name = m.group(2)
            gid = base = _slug(name)
            n = 2
            while gid in seen_ids:
                gid, n = f"{base}-{n}", n + 1
            seen_ids.add(gid)
            current = Goal(id=gid, name=name, description="", keywords=[],
                           target_pct=None, kind=kind)
            continue
        if current is None:
            continue  # preamble before the first section
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("keywords:"):
            kws = [k.strip().lower() for k in stripped.split(":", 1)[1].split(",")]
            current.keywords = [k for k in kws if k]
        elif lower.startswith("target_pct:"):
            if current.kind == "project":
                _warn(f"project '{current.name}': target_pct ignored (projects are not scored)")
                continue
            val = stripped.split(":", 1)[1].strip().rstrip("%")
            try:
                current.target_pct = float(val)
            except ValueError:
                _warn(f"goal '{current.name}': ignoring bad target_pct {val!r}")
        elif lower.startswith("archived:"):
            val = stripped.split(":", 1)[1].strip().lower()
            current.archived = val in ("true", "yes", "1", "on")
        elif stripped and not stripped.startswith("#"):
            desc_lines.append(stripped)
    _finish()

    if not goals:
        _warn(f"no goals parsed from {config.goals_path}")
    if not include_archived:
        return [g for g in goals if not g.archived]
    return goals


def only_goals(items: list[Goal]) -> list[Goal]:
    """The scored, target-bearing goals from a mixed load_goals() list."""
    return [g for g in items if getattr(g, "kind", "goal") != "project"]


def only_projects(items: list[Goal]) -> list[Goal]:
    """The tracked-but-not-judged projects from a mixed load_goals() list."""
    return [g for g in items if getattr(g, "kind", "goal") == "project"]


def load_projects(config: Config, include_archived: bool = False) -> list[Goal]:
    """Convenience: just the ``## Project:`` sections from goals.md."""
    return only_projects(load_goals(config, include_archived=include_archived))


def set_archived(config: Config, goal_id: str, archived: bool) -> bool:
    """Toggle the `archived:` flag on the goals.md section whose slug id equals
    `goal_id`, editing the markdown in place with an atomic temp-file + rename.

    Archiving inserts an `archived: true` line right after the goal's heading;
    unarchiving removes any `archived:` line from that section. Returns True when
    the goal was found (and the file rewritten), False otherwise. Historical
    reports that referenced the goal are untouched — only goals.md changes.
    """
    path = Path(config.goals_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"goals file not readable ({config.goals_path}): {exc}")
        return False

    heading_re = _HEADING_RE  # matches both "## Goal:" and "## Project:"
    archived_re = re.compile(r"^\s*archived\s*:", re.IGNORECASE)

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    seen_ids: set[str] = set()
    in_target = False
    found = False
    inserted = False

    for raw in lines:
        m = heading_re.match(raw.rstrip("\n"))
        if m:
            # New section begins — resolve its id the same way load_goals does
            # (group(2) is the name; group(1) is the "Goal"/"Project" kind).
            gid = base = _slug(m.group(2))
            n = 2
            while gid in seen_ids:
                gid, n = f"{base}-{n}", n + 1
            seen_ids.add(gid)
            in_target = gid == goal_id
            out.append(raw)
            if in_target:
                found = True
                inserted = False
                if archived:
                    # Insert immediately after the heading, matching its newline.
                    nl = "\n" if raw.endswith("\n") else "\n"
                    out.append(f"archived: true{nl}")
                    inserted = True
            continue
        if in_target and archived_re.match(raw):
            # Drop any existing archived line in the target section (avoids dups
            # when archiving, and performs the removal when unarchiving).
            continue
        out.append(raw)

    if not found:
        return False

    new_text = "".join(out)
    if new_text == text and (archived and not inserted):
        return True  # nothing to change

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".goals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def _keyword_hits(keywords: list[str], haystack: str) -> int:
    """Count distinct keywords present in haystack, whole-word-ish:
    a keyword matches only when not flanked by alphanumerics, so 'git'
    does not match 'digital' but does match 'github.com/git'. Keywords may
    contain spaces or symbols; they are regex-escaped verbatim."""
    hits = 0
    for kw in keywords:
        if not kw:
            continue
        pat = r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])"
        if re.search(pat, haystack):
            hits += 1
    return hits


def _session_haystack(s: Session) -> str:
    parts = (s.app, s.title, s.project, s.topic, s.category, s.summary, s.text_excerpt)
    return " \n ".join(p for p in parts if p).lower()


def keyword_hits_detail(items: list[Goal], session: Session) -> dict[str, list[str]]:
    """Explainability helper: {goal_or_project_id: [matched keyword tokens]} —
    WHICH of each item's keywords actually appear in the session haystack (same
    whole-word-ish rule as _keyword_hits). Items with zero hits are omitted, so
    the result reads as "these tokens are why this session matched these ids".
    Used by the audit surface to render the keyword tier of the resolution chain.
    """
    hay = _session_haystack(session)
    out: dict[str, list[str]] = {}
    if not hay:
        return out
    for g in items:
        matched = [
            kw for kw in g.keywords
            if kw and re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", hay)
        ]
        if matched:
            out[g.id] = matched
    return out


def _match_session(s: Session, goals: list[Goal]) -> str | None:
    """Goal id for this session, or None. Most keyword hits wins; ties break
    by goals order. Deterministic."""
    hay = _session_haystack(s)
    if not hay:
        return None
    best_id: str | None = None
    best_hits = 0
    for g in goals:
        hits = _keyword_hits(g.keywords, hay)
        if hits > best_hits:
            best_id, best_hits = g.id, hits
    return best_id


def match_text(text: str, goals: list[Goal]) -> str | None:
    """Public: id of the goal whose keywords best match arbitrary text (most
    distinct keyword hits wins; ties break by goals order), or None. Uses the
    same whole-word-ish keyword logic as session alignment, so `status` "now"
    mapping and intention auto-linking stay consistent with the day score."""
    hay = (text or "").lower()
    if not hay:
        return None
    best_id: str | None = None
    best_hits = 0
    for g in goals:
        hits = _keyword_hits(g.keywords, hay)
        if hits > best_hits:
            best_id, best_hits = g.id, hits
    return best_id


def attribute_sessions(timeline: DayTimeline, goals: list[Goal]) -> dict[str, dict]:
    """Attribute each session's minutes and distinct apps to a goal id (same
    at-most-one matching as align()). Returns {goal_id: {"minutes", "apps"}},
    with unmatched time under UNALIGNED_ID. Used to compute per-intention
    attributed_minutes/apps without recomputing alignment elsewhere."""
    out: dict[str, dict] = {}
    for s in timeline.sessions:
        gid = _match_session(s, goals) or UNALIGNED_ID
        entry = out.setdefault(gid, {"minutes": 0.0, "apps": []})
        entry["minutes"] += s.minutes
        if s.app and s.app not in entry["apps"]:
            entry["apps"].append(s.app)
    for entry in out.values():
        entry["minutes"] = round(entry["minutes"], 1)
    return out


def _total_minutes(timeline: DayTimeline) -> float:
    total = timeline.stats.get("total_active_minutes") if isinstance(timeline.stats, dict) else None
    if isinstance(total, (int, float)) and total > 0:
        return float(total)
    return sum(s.minutes for s in timeline.sessions)


def align(timeline: DayTimeline, goals: list[Goal]) -> list[GoalAlignment]:
    """Attribute session minutes to goals; each session counts toward at most
    one goal (see module docstring for the matching strategy).

    `goals` may be a mixed list of goals and projects (as load_goals returns).
    Sessions are matched against BOTH kinds so a project claims its own time, but
    only GOAL alignments are emitted — a project's minutes are tracked (they stay
    in the active total) yet EXCLUDED from the trailing "unaligned" share, since a
    project is accounted, not judged. Returns one GoalAlignment per scored goal in
    order, plus the trailing "unaligned" pseudo-goal (time on neither goal nor
    project).

    pct_time = minutes / total_active_minutes * 100 (0 when the day is empty).
    on_track = (target_pct is None) or (pct_time >= 0.7 * target_pct).
    """
    total = _total_minutes(timeline)
    scored = only_goals(goals)
    project_ids = {p.id for p in only_projects(goals)}
    minutes_by_goal: dict[str, float] = {g.id: 0.0 for g in scored}
    unaligned_min = 0.0
    for s in timeline.sessions:
        gid = _match_session(s, goals)  # match against goals AND projects
        if gid in minutes_by_goal:
            minutes_by_goal[gid] += s.minutes
        elif gid in project_ids:
            continue  # tracked project time: active, but not scored, not unaligned
        else:
            unaligned_min += s.minutes

    def _mk(gid: str, name: str, minutes: float, target: float | None) -> GoalAlignment:
        pct = (minutes / total * 100.0) if total > 0 else 0.0
        on_track = (target is None) or (pct >= _ON_TRACK_FACTOR * target)
        return GoalAlignment(
            goal_id=gid,
            goal_name=name,
            minutes=round(minutes, 1),
            pct_time=round(pct, 1),
            target_pct=target,
            on_track=on_track,
        )

    out = [_mk(g.id, g.name, minutes_by_goal[g.id], g.target_pct) for g in scored]
    out.append(_mk(UNALIGNED_ID, UNALIGNED_NAME, unaligned_min, None))
    return out


def overall_score(alignments: list[GoalAlignment]) -> int:
    """Deterministic 0-100 day score.

    Formula:
      1. base:
         - If any goals carry a target_pct > 0:
              base = 100 * mean over those goals of min(pct_time / target_pct, 1.0)
           i.e. average target attainment, capped at 100% per goal so one
           overshot goal can't mask a neglected one.
         - Otherwise (no targets at all):
              base = 100 - unaligned_pct
           i.e. reward whatever share of time landed on any goal.
      2. bonus: +5 per target-less goal that received any time, capped at +10
         (touching untargeted goals is good but shouldn't dominate).
      3. penalty: unaligned share above 25% of active time costs
         0.8 points per percentage point over, capped at -40.
      4. clamp to [0, 100], round to nearest int.
    Empty input or a zero-activity day scores 0.
    """
    if not alignments:
        return 0
    real = [a for a in alignments if a.goal_id != UNALIGNED_ID]
    unaligned = next((a for a in alignments if a.goal_id == UNALIGNED_ID), None)
    unaligned_pct = unaligned.pct_time if unaligned else 0.0
    if not real and unaligned_pct == 0.0:
        return 0
    if all(a.minutes == 0 for a in real) and (unaligned is None or unaligned.minutes == 0):
        return 0

    targeted = [a for a in real if a.target_pct is not None and a.target_pct > 0]
    if targeted:
        attainment = sum(min(a.pct_time / a.target_pct, 1.0) for a in targeted) / len(targeted)
        base = 100.0 * attainment
    else:
        base = 100.0 - unaligned_pct

    bonus = min(10.0, 5.0 * sum(1 for a in real if a.target_pct is None and a.minutes > 0))
    penalty = min(40.0, max(0.0, unaligned_pct - _UNALIGNED_FLAG_PCT) * 0.8)
    return int(round(max(0.0, min(100.0, base + bonus - penalty))))


def _fmt_duration(minutes: float) -> str:
    return f"{minutes / 60:.1f}h" if minutes >= 60 else f"{int(round(minutes))}m"


def drift_flags(
    timeline: DayTimeline, goals: list[Goal], alignments: list[GoalAlignment]
) -> list[str]:
    """Short human-readable drift warnings. Deterministic heuristics only
    (the LLM narrative is produced separately). Ordering is stable:
    unaligned share, then per-goal shortfalls in goals order, then heavy
    browsing/idle categories.
    """
    flags: list[str] = []
    by_id = {a.goal_id: a for a in alignments}

    ua = by_id.get(UNALIGNED_ID)
    if ua and ua.pct_time > _UNALIGNED_FLAG_PCT:
        flags.append(f"{ua.pct_time:.0f}% of active time unaligned with any goal")

    for g in goals:
        a = by_id.get(g.id)
        if a is None or g.target_pct is None:
            continue
        if a.minutes == 0:
            flags.append(f"No time on '{g.name}' (target {g.target_pct:.0f}%)")
        elif not a.on_track:
            flags.append(f"'{g.name}' at {a.pct_time:.0f}% vs target {g.target_pct:.0f}%")

    per_cat = timeline.stats.get("per_category_minutes") if isinstance(timeline.stats, dict) else None
    if isinstance(per_cat, dict):
        for cat in _FLAGGED_CATEGORIES:
            m = per_cat.get(cat)
            if isinstance(m, (int, float)) and m >= _BROWSING_FLAG_MIN:
                flags.append(f"{_fmt_duration(float(m))} {cat}")

    return flags
