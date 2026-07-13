"""End-of-day report: the day's alignment score + narrative, as markdown."""

from __future__ import annotations

import sys

from ..config import Config
from ..models import GoalAlignment, Report


def _make_backend(name: str, config: Config):
    """Construct an analysis backend by name ("gemini" | "ollama")."""
    if name == "gemini":
        from ..analyze.gemini import GeminiBackend

        return GeminiBackend(config)
    from ..analyze.ollama import OllamaBackend

    return OllamaBackend(config)


def _fallback_narrative(alignments: list[GoalAlignment]) -> str:
    """Deterministic plain-text narrative for when no LLM is reachable."""
    if not alignments:
        return "No goals configured and no activity captured for the day."
    bits = [f"{a.goal_name} {a.minutes:.0f}m ({a.pct_time:.0f}%)" for a in alignments]
    return (
        "Deterministic end-of-day summary (LLM narrative unavailable). "
        "Time on goals: " + "; ".join(bits) + "."
    )


def generate(date: str, config: Config, backend_name: str) -> Report:
    """Produce the end-of-day Report for `date` using backend `backend_name`
    ("gemini" | "ollama").

    Flow: store.load_timeline (or aggregate.timeline.build), align.load_goals,
    align.align, construct the chosen backend, backend.analyze(kind="eod"),
    attach alignments, store.save_report + store.save_benchmark, return it.

    The backend call is guarded: if the LLM is unreachable (nightly launchd
    job, transient outage, missing gemini key) we emit a one-line warning and
    fall back to a deterministic Report so the pipeline never crashes and the
    report always renders. The alignment score + drift flags are always the
    deterministic values from align.py (identical across backends, consistent
    with the alignment table); the LLM only contributes the free-form narrative
    + suggestions.

    The headline score comes from ``align.score_day`` — the SAME
    corrections-aware, min-data-guarded number the menu bar shows — not the raw
    keyword alignment. So a day the user has corrected (or a short day below
    MIN_ACTIVE_MINUTES) reports the same score the app does, and an unscored day
    renders "insufficient data" rather than a misleading keyword-only number.
    """
    from .. import align as align_mod
    from .. import labels as labels_mod
    from .. import learn as learn_mod
    from ..compare import align
    from ..store import load_timeline, save_benchmark, save_report

    timeline = load_timeline(config, date)
    if timeline is None:
        from ..aggregate import timeline as timeline_mod

        timeline = timeline_mod.build(date, config)

    goals = align.load_goals(config)
    alignments = align.align(timeline, goals)

    # Corrections-aware, min-data-guarded score (matches the menu bar headline).
    # Fold in the cached local-LLM verdicts so the EOD number matches the app.
    from .. import classify as classify_mod

    labels_by_id = labels_mod.labels_by_session(config)
    labels_by_fp = labels_mod.labels_by_fingerprint(config)
    rules = learn_mod.active_rules(config)
    llm_verdicts = classify_mod.load_verdicts(config)
    day = align_mod.score_day(timeline, goals, labels_by_id, rules,
                              labels_by_fp=labels_by_fp, llm_verdicts=llm_verdicts)

    backend = _make_backend(backend_name, config)
    try:
        report = backend.analyze(timeline, goals, "eod", alignments)
    except Exception as exc:  # LLM unreachable — degrade to a deterministic report
        print(
            f"warning: eod backend '{backend.name}' unavailable ({exc}); "
            "using deterministic summary",
            file=sys.stderr,
        )
        report = Report(
            date=timeline.date or date,
            kind="eod",
            backend=backend.name,
            model=getattr(backend, "model", ""),
            narrative=_fallback_narrative(alignments),
            alignments=list(alignments),
            raw={"error": str(exc), "error_type": exc.__class__.__name__},
        )

    # Guarantee the identifying fields + the keyword alignment table are set,
    # regardless of what the backend chose to populate.
    if not report.date:
        report.date = timeline.date or date
    report.kind = "eod"
    if not report.backend:
        report.backend = backend.name
    if not report.alignments:
        report.alignments = alignments

    # Override the model's self-reported score/drift with the deterministic,
    # corrections-aware ones so the number is reproducible, identical across
    # backends, and consistent with the menu bar. Keep whatever the LLM said in
    # raw for transparency. When the day is unscored (< MIN_ACTIVE_MINUTES),
    # scored=False and overall_score is left at 0 — render_markdown then prints
    # "insufficient data" instead of the number.
    report.raw = dict(report.raw or {})
    report.raw["llm_overall_score"] = report.overall_score
    report.raw["llm_drift_flags"] = list(report.drift_flags)
    report.scored = day["scored"]
    report.overall_score = day["overall"] if day["scored"] else 0
    report.drift_flags = align.drift_flags(timeline, goals, alignments)

    save_report(config, report)
    save_benchmark(config, report)
    return report


def _fmt_target(a: GoalAlignment) -> str:
    return f"{a.target_pct:.0f}%" if a.target_pct is not None else "—"


def render_markdown(report: Report) -> str:
    """Render a Report as human-friendly markdown: H1 with date + score,
    narrative, alignment table (goal | minutes | % | target | on track),
    drift flags, suggestions, and a footer with backend/model/cost/latency."""
    lines: list[str] = []
    lines.append(f"# scoregoals — End of day {report.date}")
    lines.append("")
    if report.scored:
        lines.append(f"**Goal alignment score: {report.overall_score}/100**")
    else:
        lines.append(
            "**Goal alignment score: insufficient data** "
            "(under 30 active minutes captured — day left unscored)"
        )
    lines.append("")
    lines.append(report.narrative.strip() if report.narrative else "_No narrative produced._")
    lines.append("")

    lines.append("## Goal alignment")
    lines.append("")
    if report.alignments:
        lines.append("| Goal | Minutes | % time | Target | On track |")
        lines.append("|------|--------:|-------:|-------:|:--------:|")
        for a in report.alignments:
            ok = "yes" if a.on_track else "no"
            lines.append(
                f"| {a.goal_name} | {a.minutes:.0f} | {a.pct_time:.1f}% "
                f"| {_fmt_target(a)} | {ok} |"
            )
    else:
        lines.append("_No goals configured (see goals.md)._")
    lines.append("")

    if report.drift_flags:
        lines.append("## Drift flags")
        lines.append("")
        for flag in report.drift_flags:
            lines.append(f"- {flag}")
        lines.append("")

    if report.suggestions:
        lines.append("## Tomorrow")
        lines.append("")
        for s in report.suggestions:
            lines.append(f"- {s}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_backend {report.backend or '?'} · model {report.model or '?'} · "
        f"{report.tokens_in}+{report.tokens_out} tok · "
        f"${report.cost_usd:.4f} · {report.latency_s:.1f}s · "
        f"generated {report.generated_at}_"
    )
    return "\n".join(lines) + "\n"
