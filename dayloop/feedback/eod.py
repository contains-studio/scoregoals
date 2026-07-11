"""End-of-day report: the day's alignment score + narrative, as markdown."""

from __future__ import annotations

from ..config import Config
from ..models import GoalAlignment, Report


def _make_backend(name: str, config: Config):
    """Construct an analysis backend by name ("gemini" | "ollama")."""
    if name == "gemini":
        from ..analyze.gemini import GeminiBackend

        return GeminiBackend(config)
    from ..analyze.ollama import OllamaBackend

    return OllamaBackend(config)


def generate(date: str, config: Config, backend_name: str) -> Report:
    """Produce the end-of-day Report for `date` using backend `backend_name`
    ("gemini" | "ollama").

    Flow: store.load_timeline (or aggregate.timeline.build), align.load_goals,
    align.align, construct the chosen backend, backend.analyze(kind="eod"),
    attach alignments, store.save_report + store.save_benchmark, return it.
    """
    from ..compare import align
    from ..store import load_timeline, save_benchmark, save_report

    timeline = load_timeline(config, date)
    if timeline is None:
        from ..aggregate import timeline as timeline_mod

        timeline = timeline_mod.build(date, config)

    goals = align.load_goals(config)
    alignments = align.align(timeline, goals)

    backend = _make_backend(backend_name, config)
    report = backend.analyze(timeline, goals, "eod", alignments)

    # Guarantee the identifying fields + the keyword alignment table are set,
    # regardless of what the backend chose to populate.
    if not report.date:
        report.date = timeline.date or date
    report.kind = "eod"
    if not report.backend:
        report.backend = backend.name
    if not report.alignments:
        report.alignments = alignments

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
    lines.append(f"# dayloop — End of day {report.date}")
    lines.append("")
    lines.append(f"**Goal alignment score: {report.overall_score}/100**")
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
