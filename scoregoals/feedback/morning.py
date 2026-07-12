"""Morning plan: a short, concrete plan for today."""

from __future__ import annotations

import sys
from datetime import date as _date
from datetime import timedelta

from ..config import Config
from ..models import ActivityRecord, DayTimeline


def _default_backend(config: Config):
    """Prefer the local ollama backend; use gemini only when config asks.

    Reads config.default_backend (the app-mutable setting); "both" still picks
    the cheap local backend here since the plan wants a single narrative.
    """
    backend = str(getattr(config, "default_backend", "") or "").lower()
    name = "gemini" if backend == "gemini" else "ollama"
    if name == "gemini":
        from ..analyze.gemini import GeminiBackend

        return GeminiBackend(config)
    from ..analyze.ollama import OllamaBackend

    return OllamaBackend(config)


def _hhmm(iso: str | None) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else (iso or "?")


def _prev_day(date: str) -> str:
    try:
        return (_date.fromisoformat(date) - timedelta(days=1)).isoformat()
    except ValueError:
        return date


def _calendar_block(calendar: list[ActivityRecord]) -> list[str]:
    lines: list[str] = ["## Today's calendar", ""]
    if calendar:
        for r in calendar:
            when = f"{_hhmm(r.start)}–{_hhmm(r.end)}" if r.end else _hhmm(r.start)
            lines.append(f"- {when} {r.title or r.text[:80]}")
    else:
        lines.append("_No calendar events (icalBuddy not installed, or nothing scheduled)._")
    lines.append("")
    return lines


def generate(date: str, config: Config) -> str:
    """Return today's morning plan as markdown.

    Inputs: goals.md, yesterday's timeline (store.load_timeline of date-1, may
    be None), today's calendar (sources.calendar.fetch, may be []). Prefer the
    local ollama backend (kind="morning") for the narrative; fall back to a
    deterministic template when no LLM is reachable — the plan must ALWAYS
    render.
    """
    from ..compare import align
    from ..sources import calendar as calendar_mod
    from ..store import load_timeline

    goals = align.load_goals(config)
    yesterday = load_timeline(config, _prev_day(date))
    today_cal = calendar_mod.fetch(date, config)

    context = yesterday or DayTimeline(date=_prev_day(date))
    alignments = align.align(context, goals)

    # Timeline handed to the LLM: today's calendar to plan around, plus
    # yesterday's activity/stats as the "what happened" context.
    plan_timeline = DayTimeline(
        date=date,
        sessions=context.sessions,
        calendar=today_cal,
        github=context.github,
        meetings=context.meetings,
        stats=context.stats,
    )

    narrative: str | None = None
    suggestions: list[str] = []
    backend_label = "template"
    try:
        backend = _default_backend(config)
        report = backend.analyze(plan_timeline, goals, "morning", alignments)
        narrative = (report.narrative or "").strip() or None
        suggestions = report.suggestions or []
        backend_label = f"{report.backend or backend.name}/{report.model or ''}".rstrip("/")
    except Exception as exc:  # LLM unreachable — degrade to a deterministic plan
        print(f"warning: morning LLM unavailable ({exc}); using template", file=sys.stderr)

    lines: list[str] = [f"# scoregoals — Morning plan {date}", ""]

    if narrative:
        lines.append(narrative)
        lines.append("")
    else:
        # Deterministic fallback narrative from goals + yesterday's alignment.
        lines.append("_Local plan (no LLM reachable) — focus on the goals below._")
        lines.append("")

    lines.extend(_calendar_block(today_cal))

    lines.append("## Focus")
    lines.append("")
    if suggestions:
        for s in suggestions:
            lines.append(f"- {s}")
    elif goals:
        # Suggest the goals that got the least attention yesterday first.
        ranked = sorted(alignments, key=lambda a: a.pct_time)
        seen: set[str] = set()
        for a in ranked:
            if a.goal_name in seen:
                continue
            seen.add(a.goal_name)
            tgt = f" (target {a.target_pct:.0f}%)" if a.target_pct is not None else ""
            lines.append(
                f"- Make progress on **{a.goal_name}**{tgt} — "
                f"{a.minutes:.0f}m ({a.pct_time:.0f}%) yesterday."
            )
            if len(seen) >= 3:
                break
    else:
        lines.append("- No goals configured yet — add some to goals.md.")
    lines.append("")

    # Seed today's intentions from the plan — only when none are set yet, so a
    # manual `today set` earlier in the day is never clobbered.
    try:
        from .. import intentions as intentions_mod

        if suggestions:
            prefill_texts = [s for s in suggestions if s.strip()][:3]
        else:
            prefill_texts = []
            seen: set[str] = set()
            for a in sorted(alignments, key=lambda a: a.pct_time):
                if a.goal_id == "unaligned" or a.goal_name in seen:
                    continue
                seen.add(a.goal_name)
                prefill_texts.append(f"Make progress on {a.goal_name}")
                if len(prefill_texts) >= 3:
                    break
        if prefill_texts:
            intentions_mod.prefill(config, date, prefill_texts, goals=goals)
    except Exception as exc:  # intentions are a nicety — never fail the plan
        print(f"warning: could not pre-fill intentions ({exc})", file=sys.stderr)

    lines.append("---")
    lines.append(f"_source {backend_label}_")
    return "\n".join(lines) + "\n"
