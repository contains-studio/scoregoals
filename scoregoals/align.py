"""scoregoals.align — authority-ordered session→verdict resolution + rescoring.

The deterministic keyword alignment lives in ``compare/align.py``. This module
sits on top of it and layers in the correction/learning signals from
``labels.py`` and ``learn.py``, applying a strict authority order:

    user label  >  learned rule  >  keyword match  >  none

with calibrated confidences:

    label   1.0
    rule    0.9
    keyword 0.6   (0.4 when two+ goals tie on keyword hits — a collision)
    none    0.2

A verdict is a goal id, ``off_track`` (worked but on no goal), or ``not_work``
(out of scope). ``not_work`` sessions are excluded from active minutes and every
goal computation entirely — personal time is not penalized. ``off_track`` time
still counts as active but is attributed to no goal (it lands in ``unaligned``).

Below ``MIN_ACTIVE_MINUTES`` of captured active time the day is *unscored*: the
score is reported as ``None`` (honest uncertainty — unknown is not off-track).
"""

from __future__ import annotations

from .compare import align as _kw
from .labels import NOT_WORK, OFF_TRACK, session_id_for
from .models import DayTimeline, Goal, GoalAlignment, Session

__all__ = [
    "MIN_ACTIVE_MINUTES",
    "CONF_LABEL",
    "CONF_RULE",
    "CONF_KEYWORD",
    "CONF_KEYWORD_COLLISION",
    "CONF_NONE",
    "resolve_session",
    "resolve_day",
    "score_day",
]

# Below this much captured active time, a day reads "insufficient data".
MIN_ACTIVE_MINUTES = 30.0

CONF_LABEL = 1.0
CONF_RULE = 0.9
CONF_KEYWORD = 0.6
CONF_KEYWORD_COLLISION = 0.4
CONF_NONE = 0.2

_SPECIAL = (OFF_TRACK, NOT_WORK)


def _keyword_verdict(session: Session, goals: list[Goal]) -> tuple[str | None, bool]:
    """(goal_id, collision) from keyword matching. collision is True when two or
    more goals share the top (non-zero) hit count — an ambiguous match."""
    hay = _kw._session_haystack(session)
    if not hay:
        return None, False
    scored = [(g.id, _kw._keyword_hits(g.keywords, hay)) for g in goals]
    best = max((h for _, h in scored), default=0)
    if best <= 0:
        return None, False
    top = [gid for gid, h in scored if h == best]
    winner = next(gid for gid, h in scored if h == best)  # goals-order tie-break
    return winner, len(top) > 1


def resolve_session(
    session: Session,
    goals: list[Goal],
    labels_by_id: dict[str, dict],
    rules: list[dict],
    date: str | None = None,
) -> dict:
    """Resolve one session to a verdict with source + confidence + needs_review.

    Returns a dict:
      session_id, verdict (goal_id|off_track|not_work|None),
      goal_id, goal_name (None unless verdict is an *active* goal id),
      source ("label"|"rule"|"keyword"|"none"), confidence, needs_review.
    """
    goals_by_id = {g.id: g for g in goals}
    sid = session_id_for(session, date)

    verdict: str | None = None
    source = "none"
    confidence = CONF_NONE

    label = labels_by_id.get(sid)
    if label is not None:
        verdict = str(label.get("verdict"))
        source, confidence = "label", CONF_LABEL
    else:
        rule_verdict = _apply_rules(session, rules)
        if rule_verdict is not None:
            verdict, source, confidence = rule_verdict, "rule", CONF_RULE
        else:
            kw_id, collision = _keyword_verdict(session, goals)
            if kw_id is not None:
                verdict = kw_id
                source = "keyword"
                confidence = CONF_KEYWORD_COLLISION if collision else CONF_KEYWORD

    # goal_id / goal_name only when the verdict names a real (active) goal.
    goal_id = verdict if verdict not in _SPECIAL and verdict is not None else None
    goal = goals_by_id.get(goal_id) if goal_id else None
    return {
        "session_id": sid,
        "verdict": verdict,
        "goal_id": goal_id,
        "goal_name": goal.name if goal else None,
        "source": source,
        "confidence": round(confidence, 2),
        # High-authority signals (a user label or a promoted rule) are settled;
        # keyword guesses and unmatched sessions surface for review.
        "needs_review": source not in ("label", "rule"),
    }


def _apply_rules(session: Session, rules: list[dict]) -> str | None:
    """First active rule whose (app, title_token) matches this session -> its
    verdict, else None. Matching: case-insensitive app equality and the rule's
    title token present in the session title's tokens."""
    from .labels import _tokens  # local import: fingerprint tokenizer

    app = (session.app or "").strip().lower()
    if not app:
        return None
    title_tokens = set(_tokens(session.title, 12))
    for rule in rules:
        pat = rule.get("rule") if isinstance(rule, dict) else None
        if not isinstance(pat, dict):
            continue
        r_app = str(pat.get("app") or "").strip().lower()
        r_tok = str(pat.get("title_token") or "").strip().lower()
        verdict = pat.get("verdict")
        if not r_app or not verdict:
            continue
        if r_app != app:
            continue
        if r_tok and r_tok not in title_tokens:
            continue
        return str(verdict)
    return None


def resolve_day(
    timeline: DayTimeline,
    goals: list[Goal],
    labels_by_id: dict[str, dict],
    rules: list[dict],
) -> list[dict]:
    """Resolve every session in the day. Each dict also carries display fields
    (span/app/title/minutes/category). Ordering is uncertain-first: sessions
    needing review come first, biggest minutes first, so a day's review reads
    top-down (see the plan's <60s review goal)."""
    out: list[dict] = []
    for s in timeline.sessions:
        r = resolve_session(s, goals, labels_by_id, rules, date=timeline.date)
        r.update(
            {
                "start": s.start,
                "end": s.end,
                "app": s.app,
                "title": s.title,
                "category": s.category,
                "minutes": round(float(s.minutes), 1),
            }
        )
        out.append(r)
    out.sort(key=lambda r: (not r["needs_review"], -r["minutes"], r["start"]))
    return out


def _mk_alignment(gid: str, name: str, minutes: float, total: float,
                  target: float | None) -> GoalAlignment:
    pct = (minutes / total * 100.0) if total > 0 else 0.0
    on_track = (target is None) or (pct >= _kw._ON_TRACK_FACTOR * target)
    return GoalAlignment(
        goal_id=gid, goal_name=name, minutes=round(minutes, 1),
        pct_time=round(pct, 1), target_pct=target, on_track=on_track,
    )


def score_day(
    timeline: DayTimeline,
    goals: list[Goal],
    labels_by_id: dict[str, dict],
    rules: list[dict],
) -> dict:
    """Recompute the day using labels+rules+keywords.

    Returns {overall (int|None), scored (bool), active_minutes (float),
    alignments (list[GoalAlignment])}. not_work sessions are excluded from
    active minutes and all goal math; off_track and unmatched time lands in the
    trailing ``unaligned`` pseudo-goal. When active minutes < MIN_ACTIVE_MINUTES
    the day is unscored (overall=None, scored=False)."""
    minutes_by_goal: dict[str, float] = {g.id: 0.0 for g in goals}
    goal_ids = set(minutes_by_goal)
    unaligned = 0.0
    active = 0.0

    for s in timeline.sessions:
        r = resolve_session(s, goals, labels_by_id, rules, date=timeline.date)
        verdict = r["verdict"]
        if verdict == NOT_WORK:
            continue  # out of scope: not active, not scored
        active += s.minutes
        if verdict in goal_ids:
            minutes_by_goal[verdict] += s.minutes
        else:
            # off_track, unmatched (None), or a verdict naming an archived/removed
            # goal — all worked-but-unaligned time.
            unaligned += s.minutes

    active = round(active, 1)
    alignments = [
        _mk_alignment(g.id, g.name, minutes_by_goal[g.id], active, g.target_pct)
        for g in goals
    ]
    alignments.append(_mk_alignment(_kw.UNALIGNED_ID, _kw.UNALIGNED_NAME, unaligned, active, None))

    scored = active >= MIN_ACTIVE_MINUTES
    overall = _kw.overall_score(alignments) if scored else None
    return {
        "overall": overall,
        "scored": scored,
        "active_minutes": active,
        "alignments": alignments,
    }
