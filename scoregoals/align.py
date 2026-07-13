"""scoregoals.align — authority-ordered session→verdict resolution + rescoring.

The deterministic keyword alignment lives in ``compare/align.py``. This module
sits on top of it and layers in the correction/learning signals from
``labels.py`` and ``learn.py``, applying a strict authority order:

    user label  >  learned rule  >  keyword match  >  llm  >  none

with calibrated confidences:

    label   1.0
    rule    0.9
    keyword 0.6   (0.4 when two+ goals tie on keyword hits — a collision)
    llm     0.5   (a local-LLM guess — see classify.py; below keyword, above none)
    none    0.2

The ``llm`` tier only fills sessions the deterministic tiers left at source
``none``: it turns an "unmatched" row into a best-guess suggestion (which beats
showing nothing), but it is still a guess — ``needs_review`` stays True so the
review pane surfaces it. A ``not_work`` llm verdict excludes a session from
active minutes only at confidence >= 0.7 (a low-confidence "personal" guess is
not trusted enough to silently delete time).

A verdict is a goal id, ``off_track`` (worked but on no goal), or ``not_work``
(out of scope). ``not_work`` sessions are excluded from active minutes and every
goal computation entirely — personal time is not penalized. ``off_track`` time
still counts as active but is attributed to no goal (it lands in ``unaligned``).

Below ``MIN_ACTIVE_MINUTES`` of captured active time the day is *unscored*: the
score is reported as ``None`` (honest uncertainty — unknown is not off-track).
"""

from __future__ import annotations

from .compare import align as _kw
from .labels import NOT_WORK, OFF_TRACK, match_label_by_fingerprint, session_id_for
from .models import DayTimeline, Goal, GoalAlignment, Session

__all__ = [
    "MIN_ACTIVE_MINUTES",
    "CONF_LABEL",
    "CONF_RULE",
    "CONF_KEYWORD",
    "CONF_KEYWORD_COLLISION",
    "CONF_LLM",
    "CONF_LLM_NOT_WORK_MIN",
    "CONF_NONE",
    "resolve_session",
    "resolve_day",
    "score_day",
]

# Below this much captured active time, a day reads "insufficient data".
MIN_ACTIVE_MINUTES = 30.0

CONF_LABEL = 1.0
CONF_SYSTEM = 0.95  # curated macOS system surfaces -> not_work, no review needed
CONF_IMPLICIT = 0.7  # unreviewed completed day = weak acceptance of the guess
CONF_RULE = 0.9

# macOS system surfaces that can never be "work": permission prompts, the lock
# screen, the screensaver, window chrome. Sessions from these apps auto-resolve
# to not_work (source "system") unless a user label says otherwise.
SYSTEM_NOISE_APPS: frozenset = frozenset({
    "UserNotificationCenter",
    "loginwindow",
    "ScreenSaverEngine",
    "LockScreen",
    "Dock",
    "WindowServer",
    "Window Server",
})
CONF_KEYWORD = 0.6
CONF_KEYWORD_COLLISION = 0.4
CONF_LLM = 0.5  # a local-LLM guess (classify.py): below keyword, above none
# A cached llm verdict is only used to fill a "none" when its own confidence
# clears this bar (it is still a guess, so it must at least be a confident one).
CONF_LLM_MIN = 0.5
# not_work llm verdicts exclude a session from active minutes only at/above this.
CONF_LLM_NOT_WORK_MIN = 0.7
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
    labels_by_fp: dict[tuple[str, int], list[dict]] | None = None,
    llm_verdicts: dict[str, dict] | None = None,
) -> dict:
    """Resolve one session to a verdict with source + confidence + needs_review.

    Returns a dict:
      session_id, verdict (goal_id|off_track|not_work|None),
      goal_id, goal_name (None unless verdict is an *active* goal id),
      source ("label"|"rule"|"keyword"|"llm"|"none"), confidence, needs_review.

    `labels_by_fp` (from labels.labels_by_fingerprint) enables a fingerprint
    fallback when the session_id doesn't match a stored label — segmentation can
    re-run and jitter a session's id, and a correction must not silently orphan.

    `llm_verdicts` (from classify.load_verdicts) is the local-LLM guess cache.
    It is consulted LAST — only when the deterministic tiers would leave the
    session at source ``none`` — and only when the cached confidence clears
    CONF_LLM_MIN. The result stays ``needs_review`` (a guess, shown as a
    suggestion). See the module docstring for the authority order.
    """
    goals_by_id = {g.id: g for g in goals}
    sid = session_id_for(session, date)

    verdict: str | None = None
    source = "none"
    confidence = CONF_NONE

    label = labels_by_id.get(sid)
    if label is None and labels_by_fp:
        # Id didn't match (re-segmentation jitter) — try the fingerprint fallback.
        label = match_label_by_fingerprint(session, labels_by_fp)
    if (label is not None and str(label.get("source")) == "implicit"
            and date and str(label.get("date")) != str(date)):
        # Implicit acceptance is a weak, same-day signal: it must never settle a
        # DIFFERENT day's session via the fingerprint fallback.
        label = None
    if label is not None:
        verdict = str(label.get("verdict"))
        if str(label.get("source")) == "implicit":
            # A completed, never-corrected day: weak acceptance, no review needed.
            source, confidence = "implicit", CONF_IMPLICIT
        else:
            source, confidence = "label", CONF_LABEL
    elif (session.app or "").strip() in SYSTEM_NOISE_APPS:
        # macOS system surfaces (permission prompts, the lock screen, the
        # screensaver) are never "work" — learned the hard way when an overnight
        # permission dialog booked 7.5 phantom hours. A user label still wins
        # above; this settles the rest without review.
        verdict, source, confidence = "not_work", "system", CONF_SYSTEM
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

    # llm tier: fill an otherwise-unmatched session from the local-LLM guess
    # cache (classify.py). Only when nothing deterministic matched (source
    # "none") and the cached guess is at least CONF_LLM_MIN confident.
    if source == "none" and llm_verdicts:
        cached = llm_verdicts.get(sid)
        if isinstance(cached, dict):
            try:
                c_conf = float(cached.get("confidence", 0.0))
            except (TypeError, ValueError):
                c_conf = 0.0
            c_verdict = cached.get("verdict")
            if c_verdict is not None and c_conf >= CONF_LLM_MIN:
                verdict = str(c_verdict)
                source = "llm"
                confidence = CONF_LLM

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
        # Settled signals (user label, implicit acceptance of a completed day,
        # a promoted rule, or a known system surface) skip review; live keyword
        # guesses and unmatched sessions surface for it.
        "needs_review": source not in ("label", "implicit", "rule", "system"),
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
        # An empty title_token is an app-only rule: it would match EVERY session
        # of that app (e.g. 3 windowless Chrome not_work labels would delete all
        # real Chrome time). learn.py no longer promotes these; ignore any that
        # linger so a broad rule can never silently rewrite a whole app's time.
        if not r_tok:
            continue
        if r_app != app:
            continue
        if r_tok not in title_tokens:
            continue
        return str(verdict)
    return None


def resolve_day(
    timeline: DayTimeline,
    goals: list[Goal],
    labels_by_id: dict[str, dict],
    rules: list[dict],
    labels_by_fp: dict[tuple[str, int], list[dict]] | None = None,
    llm_verdicts: dict[str, dict] | None = None,
) -> list[dict]:
    """Resolve every session in the day. Each dict also carries display fields
    (span/app/title/minutes/category). Ordering is uncertain-first: sessions
    needing review come first, biggest minutes first, so a day's review reads
    top-down (see the plan's <60s review goal)."""
    out: list[dict] = []
    for s in timeline.sessions:
        r = resolve_session(s, goals, labels_by_id, rules, date=timeline.date,
                            labels_by_fp=labels_by_fp, llm_verdicts=llm_verdicts)
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


def _excludes_from_active(resolved: dict, llm_verdicts: dict | None) -> bool:
    """Whether a not_work session is excluded from active minutes. A label /
    system / rule not_work always excludes; an llm not_work excludes only when
    the MODEL's own confidence (from the cache, not the fixed 0.5 tier
    confidence) clears CONF_LLM_NOT_WORK_MIN."""
    if resolved["source"] != "llm":
        return True
    cached = (llm_verdicts or {}).get(resolved["session_id"])
    try:
        raw_conf = float((cached or {}).get("confidence", 0.0))
    except (TypeError, ValueError):
        raw_conf = 0.0
    return raw_conf >= CONF_LLM_NOT_WORK_MIN


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
    labels_by_fp: dict[tuple[str, int], list[dict]] | None = None,
    llm_verdicts: dict[str, dict] | None = None,
) -> dict:
    """Recompute the day using labels+rules+keywords+llm.

    Returns {overall (int|None), scored (bool), active_minutes (float),
    alignments (list[GoalAlignment])}. not_work sessions are excluded from
    active minutes and all goal math; off_track and unmatched time lands in the
    trailing ``unaligned`` pseudo-goal. When active minutes < MIN_ACTIVE_MINUTES
    the day is unscored (overall=None, scored=False).

    An llm-sourced ``not_work`` is trusted to EXCLUDE a session only when its
    confidence clears CONF_LLM_NOT_WORK_MIN (0.7); a lower-confidence "personal"
    guess keeps the time in the active total (landing in ``unaligned``) rather
    than silently deleting it."""
    minutes_by_goal: dict[str, float] = {g.id: 0.0 for g in goals}
    goal_ids = set(minutes_by_goal)
    unaligned = 0.0
    active = 0.0

    for s in timeline.sessions:
        r = resolve_session(s, goals, labels_by_id, rules, date=timeline.date,
                            labels_by_fp=labels_by_fp, llm_verdicts=llm_verdicts)
        verdict = r["verdict"]
        if verdict == NOT_WORK and _excludes_from_active(r, llm_verdicts):
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
