"""dayloop.analyze.base — FROZEN backend interface + shared prompt/pricing.

Both backends (gemini, ollama) MUST build their prompt via build_prompt() so
the benchmark comparison is apples-to-apples. Stdlib-only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import DayTimeline, Goal, GoalAlignment, Report

__all__ = ["AnalysisBackend", "PRICING", "estimate_tokens", "estimate_cost", "build_prompt"]

# USD per 1M tokens, keyed by model name. EDITABLE placeholders — verify
# against current provider pricing (config.toml can carry overrides for
# gemini via gemini_price_in_per_1m / gemini_price_out_per_1m).
PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    # Local models cost nothing per token.
    "huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16": {"input": 0.0, "output": 0.0},
}


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Used when the API does not
    report usage (e.g. gemini CLI, some ollama responses)."""
    return max(1, len(text) // 4)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """USD cost for a call. Unknown models are treated as free (local)."""
    p = PRICING.get(model, {"input": 0.0, "output": 0.0})
    return (tokens_in * p.get("input", 0.0) + tokens_out * p.get("output", 0.0)) / 1_000_000


def build_prompt(
    timeline: DayTimeline,
    goals: list[Goal],
    kind: str,
    alignments: list[GoalAlignment],
) -> str:
    """Build the SHARED analysis prompt used by every backend.

    Digest of the day (sessions, calendar, github, meetings) + goals +
    pre-computed keyword alignment, then a task instruction per `kind`
    (eod|morning|weekly) demanding a strict JSON reply:
    {"narrative": str, "overall_score": int, "drift_flags": [str], "suggestions": [str]}
    """

    def hhmm(iso: str | None) -> str:
        return iso[11:16] if iso and len(iso) >= 16 else (iso or "?")

    lines: list[str] = []
    lines.append(
        "You are dayloop, a blunt but supportive personal activity analyst for Michael."
    )
    lines.append(f"Date: {timeline.date}. Report kind: {kind}.")
    stats = timeline.stats or {}
    lines.append(f"Total active minutes: {stats.get('total_active_minutes', 0)}")
    per_cat = stats.get("per_category_minutes") or {}
    if per_cat:
        lines.append(
            "Minutes by category: "
            + ", ".join(f"{k}={round(float(v))}" for k, v in per_cat.items())
        )

    lines.append("")
    lines.append("== SESSIONS ==")
    for s in timeline.sessions:
        bits = [b for b in (s.app, s.title, f"project:{s.project}" if s.project else None) if b]
        lines.append(
            f"- {hhmm(s.start)}-{hhmm(s.end)} ({round(s.minutes)}m)"
            f" [{s.category or 'other'}] " + " | ".join(bits)
        )
        if s.summary:
            lines.append(f"    summary: {s.summary}")
        elif s.text_excerpt:
            lines.append(f"    excerpt: {s.text_excerpt[:200]}")

    if timeline.calendar:
        lines.append("")
        lines.append("== CALENDAR ==")
        for r in timeline.calendar:
            lines.append(f"- {hhmm(r.start)}-{hhmm(r.end)} {r.title or r.text[:80]}")

    if timeline.github:
        lines.append("")
        lines.append("== GITHUB ==")
        for r in timeline.github:
            repo = r.meta.get("repo", "")
            lines.append(f"- {hhmm(r.start)} {repo}: {r.title or r.text[:100]}")

    if timeline.meetings:
        lines.append("")
        lines.append("== MEETINGS (transcripts / notes, may be truncated) ==")
        for r in timeline.meetings:
            lines.append(f"- [{r.source}/{r.kind}] {r.title or ''}")
            if r.text:
                lines.append("    " + r.text[:600].replace("\n", "\n    "))

    lines.append("")
    lines.append("== GOALS ==")
    for g in goals:
        tgt = f" (target {g.target_pct}% of active time)" if g.target_pct is not None else ""
        desc = " ".join(g.description.split())[:220]
        lines.append(f"- {g.name}{tgt}: {desc}")

    if alignments:
        lines.append("")
        lines.append("== PRE-COMPUTED ALIGNMENT (keyword-based; refine, do not just repeat) ==")
        for a in alignments:
            tgt = f" / target {a.target_pct}%" if a.target_pct is not None else ""
            lines.append(
                f"- {a.goal_name}: {round(a.minutes)}m, {a.pct_time:.1f}% of active time"
                f"{tgt}, on_track={a.on_track}"
            )

    task = {
        "eod": (
            "Write an end-of-day review: what actually happened vs the goals, where time "
            "leaked, what deserves credit. Score the day 0-100 for goal alignment."
        ),
        "morning": (
            "Write a short, concrete morning plan for today: top 3 blocks to schedule, "
            "keyed to the goals and what yesterday's timeline shows."
        ),
        "weekly": (
            "Write a weekly synthesis: trends across days, wins, recurring drift patterns, "
            "and ONE system-level tweak to try next week."
        ),
    }.get(kind, "Analyze the activity against the goals.")

    lines.append("")
    lines.append("== TASK ==")
    lines.append(task)
    lines.append(
        "Respond with ONLY a JSON object, no markdown fences, exactly this shape: "
        '{"narrative": "<2-6 sentences>", "overall_score": <int 0-100>, '
        '"drift_flags": ["<short flag>", ...], "suggestions": ["<short action>", ...]}'
    )
    return "\n".join(lines)


class AnalysisBackend(ABC):
    """Interface every analysis backend implements.

    Attributes:
        name:  short backend id, e.g. "gemini" or "ollama" (goes in Report.backend)
        model: model identifier (goes in Report.model and pricing lookups)
    """

    name: str = "base"
    model: str = ""

    @abstractmethod
    def analyze(
        self,
        timeline: DayTimeline,
        goals: list[Goal],
        kind: str,
        alignments: list[GoalAlignment],
    ) -> Report:
        """Run one analysis over `timeline` and return a fully populated Report
        (narrative, overall_score, drift_flags, suggestions, tokens_in/out,
        cost_usd, latency_s, raw). Must use build_prompt() for the prompt."""
        raise NotImplementedError
