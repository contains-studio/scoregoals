"""Weekly synthesis: trends, wins, drift patterns, one system tweak."""

from __future__ import annotations

import sys
from datetime import date as _date
from datetime import timedelta

from ..config import Config
from ..models import DayTimeline, GoalAlignment


def _default_backend(config: Config):
    """Prefer the local ollama backend; use gemini only when config asks.

    Reads config.default_backend (the app-mutable setting); "both" still picks
    the cheap local backend here since the synthesis wants a single narrative.
    """
    backend = str(getattr(config, "default_backend", "") or "").lower()
    name = "gemini" if backend == "gemini" else "ollama"
    if name == "gemini":
        from ..analyze.gemini import GeminiBackend

        return GeminiBackend(config)
    from ..analyze.ollama import OllamaBackend

    return OllamaBackend(config)


def _week_dates(week_start: str) -> list[str]:
    try:
        start = _date.fromisoformat(week_start)
    except ValueError:
        return [week_start]
    return [(start + timedelta(days=i)).isoformat() for i in range(7)]


def _top_category(tl: DayTimeline) -> str:
    per_cat = (tl.stats or {}).get("per_category_minutes") or {}
    if not per_cat:
        return "—"
    return max(per_cat.items(), key=lambda kv: kv[1])[0]


def generate(week_start: str, config: Config) -> str:
    """Return the weekly synthesis (markdown) for the 7 days starting at
    `week_start` (YYYY-MM-DD, a Monday by CLI convention).

    Load available timelines/reports for the window (missing days are fine),
    aggregate per-goal minutes across days, and run the LLM (kind="weekly",
    prefer ollama) over the digest. Degrade to a stats-only markdown table
    when no LLM is reachable.
    """
    from ..compare import align
    from ..store import load_timeline

    dates = _week_dates(week_start)
    week_end = dates[-1]
    goals = align.load_goals(config)

    timelines: list[DayTimeline] = []
    for d in dates:
        tl = load_timeline(config, d)
        if tl is not None:
            timelines.append(tl)

    if not timelines:
        return (
            f"# scoregoals — Weekly synthesis {week_start} → {week_end}\n\n"
            "_No timelines captured for this week — run `scoregoals capture <date>` "
            "(or `scoregoals mock`) first._\n"
        )

    # Aggregate the week into a single synthetic DayTimeline for analysis.
    all_sessions = [s for tl in timelines for s in tl.sessions]
    total_minutes = sum(s.minutes for s in all_sessions)
    per_cat: dict[str, float] = {}
    for s in all_sessions:
        cat = s.category or "other"
        per_cat[cat] = per_cat.get(cat, 0.0) + s.minutes

    week_timeline = DayTimeline(
        date=week_start,
        sessions=all_sessions,
        calendar=[r for tl in timelines for r in tl.calendar],
        github=[r for tl in timelines for r in tl.github],
        meetings=[r for tl in timelines for r in tl.meetings],
        stats={
            "total_active_minutes": round(total_minutes, 1),
            "per_category_minutes": {k: round(v, 1) for k, v in per_cat.items()},
            "counts": {"days": len(timelines), "sessions": len(all_sessions)},
        },
    )
    alignments: list[GoalAlignment] = align.align(week_timeline, goals)

    narrative: str | None = None
    drift_flags: list[str] = []
    suggestions: list[str] = []
    backend_label = "stats-only"
    try:
        backend = _default_backend(config)
        report = backend.analyze(week_timeline, goals, "weekly", alignments)
        narrative = (report.narrative or "").strip() or None
        drift_flags = report.drift_flags or []
        suggestions = report.suggestions or []
        backend_label = f"{report.backend or backend.name}/{report.model or ''}".rstrip("/")
    except Exception as exc:  # LLM unreachable — stats-only synthesis
        print(f"warning: weekly LLM unavailable ({exc}); stats-only", file=sys.stderr)

    lines: list[str] = [f"# scoregoals — Weekly synthesis {week_start} → {week_end}", ""]
    lines.append(
        f"{len(timelines)} day(s) captured · {round(total_minutes)} active minutes total."
    )
    lines.append("")

    if narrative:
        lines.append(narrative)
        lines.append("")

    # Per-day trend table.
    lines.append("## Days")
    lines.append("")
    lines.append("| Date | Active min | Sessions | Top category |")
    lines.append("|------|-----------:|---------:|--------------|")
    for tl in timelines:
        active = round(float((tl.stats or {}).get("total_active_minutes", 0)))
        lines.append(f"| {tl.date} | {active} | {len(tl.sessions)} | {_top_category(tl)} |")
    lines.append("")

    # Per-goal weekly totals.
    lines.append("## Goal totals")
    lines.append("")
    if alignments:
        lines.append("| Goal | Minutes | % week | Target | On track |")
        lines.append("|------|--------:|-------:|-------:|:--------:|")
        for a in alignments:
            tgt = f"{a.target_pct:.0f}%" if a.target_pct is not None else "—"
            ok = "yes" if a.on_track else "no"
            lines.append(
                f"| {a.goal_name} | {a.minutes:.0f} | {a.pct_time:.1f}% | {tgt} | {ok} |"
            )
    else:
        lines.append("_No goals configured (see goals.md)._")
    lines.append("")

    if drift_flags:
        lines.append("## Recurring drift")
        lines.append("")
        for flag in drift_flags:
            lines.append(f"- {flag}")
        lines.append("")

    if suggestions:
        lines.append("## System tweak for next week")
        lines.append("")
        for s in suggestions:
            lines.append(f"- {s}")
        lines.append("")

    lines.append("---")
    lines.append(f"_source {backend_label}_")
    return "\n".join(lines) + "\n"
