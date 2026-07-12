"""Benchmark harness: run multiple backends over identical input, record
cost/latency/quality so Michael can pick a backend with data.

Persistence note: the CLI (`scoregoals analyze`, frozen) calls store.save_report
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

# Sentinel written to the compare.csv overall_score column when a day is below
# the min-data threshold (Report.scored is False). Documented in STATUS_SCHEMA.md.
INSUFFICIENT_DATA_SCORE = -1

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
    from .. import align as align_mod
    from .. import labels as labels_mod
    from .. import learn as learn_mod
    from ..compare import align

    det_flags = align.drift_flags(timeline, goals, alignments)

    # Score with the SAME corrections-aware, min-data-guarded path the menu bar
    # and the EOD report use (align.score_day) — not the raw keyword alignment.
    # score_day excludes not_work minutes from the active-minutes total and the
    # goal math, so the compare.csv `overall_score` column can't disagree with
    # the app. Below MIN_ACTIVE_MINUTES the day is unscored and the column
    # carries the documented sentinel -1 (see docs/STATUS_SCHEMA.md).
    labels_by_id = labels_mod.labels_by_session(config)
    labels_by_fp = labels_mod.labels_by_fingerprint(config)
    rules = learn_mod.active_rules(config)
    day = align_mod.score_day(timeline, goals, labels_by_id, rules,
                              labels_by_fp=labels_by_fp)
    day_scored = day["scored"]
    csv_score = day["overall"] if day_scored else INSUFFICIENT_DATA_SCORE

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
        rpt.overall_score = csv_score
        rpt.scored = day_scored
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
