"""Benchmark harness: run multiple backends over identical input, record
cost/latency/quality so Michael can pick a backend with data.

Persistence note: the CLI (`dayloop analyze`, frozen) calls store.save_report
and store.save_benchmark on every Report returned by run(), so run() itself
does NOT write to sqlite — doing both would double-insert rows. run() only
executes the backends; append_csv() maintains data/benchmarks/compare.csv.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from ..config import Config
from ..models import DayTimeline, Goal, GoalAlignment, Report

# compare.csv column order — keep in sync with append_csv().
CSV_COLUMNS = [
    "date",
    "kind",
    "backend",
    "model",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "latency_s",
    "overall_score",
    "generated_at",
]


def run(
    timeline: DayTimeline,
    goals: list[Goal],
    alignments: list[GoalAlignment],
    backends: list,
    kind: str,
    config: Config,
) -> list[Report]:
    """Run each AnalysisBackend in `backends` over the SAME
    (timeline, goals, kind, alignments).

    Per-backend failures (no key, server down, bad JSON) are caught and warned
    about on one stderr line; the failed backend yields a placeholder Report
    (narrative notes the error, zero tokens/cost/latency/score) so the run is
    visible in the comparison, and never aborts the other backends. Each
    Report's alignments are filled from `alignments` if the backend did not.

    The overall_score + drift_flags are overridden with the deterministic
    values from align.py so the compare.csv `overall_score` column is the same
    reproducible math for every backend (the comparison should differ only in
    narrative/cost/latency, never in the underlying score). The LLM's
    self-reported values are preserved in raw for transparency.
    Returns the Reports in backend order.
    """
    from ..compare import align

    det_score = align.overall_score(alignments)
    det_flags = align.drift_flags(timeline, goals, alignments)

    reports: list[Report] = []
    for backend in backends:
        name = getattr(backend, "name", backend.__class__.__name__)
        model = getattr(backend, "model", "")
        try:
            rpt = backend.analyze(timeline, goals, kind, alignments)
        except Exception as exc:
            print(f"warning: backend '{name}' failed: {exc}", file=sys.stderr)
            rpt = Report(
                date=timeline.date,
                kind=kind,
                backend=name,
                model=model,
                narrative=f"[backend error] {name} failed: {exc}",
                alignments=list(alignments),
                raw={"error": str(exc), "error_type": exc.__class__.__name__},
            )
        if not rpt.alignments:
            rpt.alignments = list(alignments)
        rpt.raw = dict(rpt.raw or {})
        rpt.raw["llm_overall_score"] = rpt.overall_score
        rpt.raw["llm_drift_flags"] = list(rpt.drift_flags)
        rpt.overall_score = det_score
        rpt.drift_flags = list(det_flags)
        reports.append(rpt)
    return reports


def append_csv(reports: list[Report], path: str) -> None:
    """Append one row per Report to the CSV at `path` (create with header if
    new/empty). Columns, in order:
    date,kind,backend,model,tokens_in,tokens_out,cost_usd,latency_s,overall_score,generated_at
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_header = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(CSV_COLUMNS)
        for r in reports:
            writer.writerow(
                [
                    r.date,
                    r.kind,
                    r.backend,
                    r.model,
                    r.tokens_in,
                    r.tokens_out,
                    f"{r.cost_usd:.6f}",
                    f"{r.latency_s:.3f}",
                    r.overall_score,
                    r.generated_at,
                ]
            )
