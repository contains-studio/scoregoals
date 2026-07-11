"""Real-time drift nudge: short message when current activity is off-goal."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..config import Config
from ..models import Goal, Session


def _session_haystack(s: Session) -> str:
    parts = [s.app, s.title, s.project, s.topic, s.summary, s.text_excerpt]
    return " ".join(p for p in parts if p).lower()


def _matches_any_goal(s: Session, keywords: list[str]) -> bool:
    hay = _session_haystack(s)
    return any(kw in hay for kw in keywords)


def check(config: Config) -> str | None:
    """Return a short nudge message when the last ~config.nudge_threshold_min
    minutes of screenpipe activity match no goal keywords; None when on track
    OR when screenpipe is unreachable (no data -> no nagging).

    Flow: sources.screenpipe.fetch for the recent window, quick keyword match
    against compare.align.load_goals. Cheap heuristic — no LLM calls here.
    """
    from ..aggregate.segment import segment
    from ..compare import align
    from ..sources import screenpipe

    threshold = int(getattr(config, "nudge_threshold_min", 20) or 20)
    end = datetime.now().astimezone()
    start = end - timedelta(minutes=threshold)

    records = screenpipe.fetch(
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
        config,
    )
    if not records:  # screenpipe down / nothing captured -> stay quiet
        return None

    sessions = segment(records)
    if not sessions:
        return None

    goals: list[Goal] = align.load_goals(config)
    keywords = sorted({kw.lower().strip() for g in goals for kw in g.keywords if kw.strip()})
    if not keywords:  # no goals to judge against -> no nagging
        return None

    total = sum(s.minutes for s in sessions)
    if total <= 0:
        return None

    matched_min = sum(s.minutes for s in sessions if _matches_any_goal(s, keywords))
    # On track when at least half of the recent window aligns with some goal.
    if matched_min >= total * 0.5:
        return None

    unaligned = [s for s in sessions if not _matches_any_goal(s, keywords)]
    if not unaligned:
        return None
    worst = max(unaligned, key=lambda s: s.minutes)
    label = worst.title or worst.app or (worst.category or "off-goal activity")
    return (
        f"Last {threshold}m mostly off-goal — {round(worst.minutes)}m on {label} "
        f"[{worst.category or 'other'}] matched no goal. Refocus?"
    )
