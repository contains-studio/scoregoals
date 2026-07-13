"""scoregoals.agentapi — data-gathering for the agent-facing CLI surface.

Michael's "check on me" agent shells out to ``scoregoals <cmd> --json`` and must
be able to reach EVERY piece of stored data. The commands here are pure readers
(no mutation) that assemble clean JSON payloads; the thin ``cmd_*`` handlers in
cli.py print what these functions return. Warnings go to stderr; these functions
never print to stdout.

Design rules mirror the rest of the CLI:
* Reuse the existing engine (store.load_timeline heal path, align.score_day,
  labels/learn parsers, compare.align.load_goals) so numbers here can never
  disagree with ``status`` / ``review`` / the EOD report.
* Redact every screen-captured text field through aggregate.redact.redact_text
  before it leaves the process (search).
* Degrade gracefully: a missing/unreachable dependency yields an empty result
  and (where relevant) an ``error`` string, never a traceback.
"""

from __future__ import annotations

import csv
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .models import to_dict


# --- shared date helpers -----------------------------------------------------


def _today() -> str:
    return _date.today().isoformat()


def _parse_date(s: str | None) -> _date | None:
    try:
        return _date.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


def _window(days: int, end: str | None) -> tuple[str, str]:
    """(from_iso, to_iso) for the `days`-long window ending `end` (inclusive)."""
    end_d = _parse_date(end) or _date.today()
    start_d = end_d - timedelta(days=max(1, days) - 1)
    return start_d.isoformat(), end_d.isoformat()


# --- timeline ----------------------------------------------------------------


def timeline_payload(cfg: Config, date: str) -> dict:
    """The full stored DayTimeline for `date` (sessions incl. ids, calendar,
    github, meetings, stats), read through the store heal path. A date with no
    capture yields ``{"date": D, "exists": false, ...}`` (still exit 0)."""
    from .store import load_timeline

    tl = load_timeline(cfg, date)
    if tl is None:
        return {
            "date": date,
            "exists": False,
            "sessions": [],
            "calendar": [],
            "github": [],
            "meetings": [],
            "stats": {},
        }
    out = to_dict(tl)
    out["exists"] = True
    out["date"] = tl.date or date
    return out


# --- search (screenpipe proxy, redacted) -------------------------------------


def search_payload(
    cfg: Config,
    query: str,
    from_iso: str | None,
    to_iso: str | None,
    limit: int,
    type_: str,
) -> dict:
    """Proxy screenpipe /search, redacting every text field. Unreachable
    screenpipe -> ``{"error": "screenpipe unreachable", ..., "results": []}``
    (still exit 0). `type_` is ocr|audio|all."""
    from .aggregate.redact import redact_text
    from .sources import screenpipe

    type_map: dict[str, tuple[str, ...]] = {
        "ocr": ("ocr",),
        "audio": ("audio",),
        "all": ("ocr", "audio", "ui"),
    }
    types = type_map.get(type_, ("ocr", "audio", "ui"))

    res = screenpipe.search(
        query, cfg, start_iso=from_iso, end_iso=to_iso, types=types, limit=limit
    )
    records = res.get("records") or []
    results = []
    for rec in records:
        meta = dict(rec.meta or {})
        speaker = meta.get("speaker")
        results.append(
            {
                "type": rec.kind,
                "timestamp": rec.start or None,
                "end": rec.end,
                "app": rec.app,
                "title": redact_text(rec.title) if rec.title else rec.title,
                "text": redact_text(rec.text or ""),
                "frame_id": meta.get("frame_id"),
                "speaker": redact_text(str(speaker)) if speaker is not None else None,
            }
        )

    payload: dict = {
        "query": query,
        "from": from_iso,
        "to": to_iso,
        "type": type_,
        "limit": limit,
        "count": len(results),
        "results": results,
    }
    if res.get("error"):
        payload["error"] = res["error"]
        if payload["error"] == "screenpipe unreachable" or "unreachable" in payload["error"]:
            payload["error"] = "screenpipe unreachable"
    return payload


# --- labels (corrections log) ------------------------------------------------


def labels_payload(cfg: Config, date: str | None, days: int | None) -> dict:
    """Parsed labels.jsonl entries (newest first). Optional filter: `date` (one
    day) or `days` (N-day window ending today). Unfiltered returns every entry."""
    from . import labels as labels_mod

    records = labels_mod.load_labels(cfg)

    from_iso: str | None = None
    to_iso: str | None = None
    if date:
        from_iso = to_iso = date
    elif days:
        from_iso, to_iso = _window(days, None)

    def _in_window(rec: dict) -> bool:
        if from_iso is None:
            return True
        d = labels_mod._label_day(rec)
        if d is None:
            return False
        return _date.fromisoformat(from_iso) <= d <= _date.fromisoformat(to_iso)

    filtered = [r for r in records if _in_window(r)]
    filtered.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return {
        "from": from_iso,
        "to": to_iso,
        "count": len(filtered),
        "labels": filtered,
    }


# --- rules (learned_rules.json) ----------------------------------------------


def rules_payload(cfg: Config) -> dict:
    """Active + retired learned rules, each annotated with its created_from
    count (how many labels minted it)."""
    from . import learn as learn_mod

    data = learn_mod.load_rules(cfg)
    # load_rules filters `rules` to valid ones; read `retired` from the file too.
    raw = _read_json(learn_mod.rules_path(cfg))
    retired = raw.get("retired") if isinstance(raw, dict) else []
    if not isinstance(retired, list):
        retired = []

    def _annotate(rule: dict) -> dict:
        out = dict(rule)
        cf = rule.get("created_from")
        out["created_from_count"] = len(cf) if isinstance(cf, list) else 0
        return out

    active = [_annotate(r) for r in data.get("rules", []) if isinstance(r, dict)]
    retired_out = [_annotate(r) for r in retired if isinstance(r, dict)]
    return {
        "active_count": len(active),
        "retired_count": len(retired_out),
        "active": active,
        "retired": retired_out,
    }


def _read_json(path: Path):
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --- bench (compare.csv rows) ------------------------------------------------

_CSV_INT = ("tokens_in", "tokens_out")
_CSV_FLOAT = ("cost_usd", "latency_s")


def bench_payload(cfg: Config, days: int | None) -> dict:
    """Parsed benchmarks/compare.csv rows (one per backend run), newest first.
    Optional `days` filters to the N-day window ending today. Typed values;
    the documented -1 overall_score sentinel (insufficient-data day) is passed
    through verbatim."""
    path = Path(cfg.benchmarks_dir) / "compare.csv"
    rows: list[dict] = []
    from_iso: str | None = None
    to_iso: str | None = None
    if days:
        from_iso, to_iso = _window(days, None)

    if path.is_file():
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                for raw in csv.DictReader(fh):
                    d = str(raw.get("date") or "")
                    if from_iso is not None:
                        dd = _parse_date(d)
                        if dd is None or not (
                            _date.fromisoformat(from_iso) <= dd <= _date.fromisoformat(to_iso)
                        ):
                            continue
                    row = dict(raw)
                    for k in _CSV_INT:
                        row[k] = _to_int(raw.get(k))
                    for k in _CSV_FLOAT:
                        row[k] = _to_float(raw.get(k))
                    row["overall_score"] = _to_int(raw.get("overall_score"))
                    rows.append(row)
        except OSError:
            pass

    rows.sort(key=lambda r: str(r.get("generated_at") or ""), reverse=True)
    return {"from": from_iso, "to": to_iso, "count": len(rows), "rows": rows}


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --- reports (db + md) -------------------------------------------------------

_MD_KINDS = ("eod", "weekly", "morning")


def _md_path(cfg: Config, date: str, kind: str) -> Path:
    return Path(cfg.reports_dir) / f"{date}-{kind}.md"


def _db_reports(cfg: Config) -> list[dict]:
    """Latest stored Report per (date, kind, backend), newest id last. Each dict
    carries the parsed Report json under 'report'."""
    import json

    from .store import connect

    latest: dict[tuple, dict] = {}
    try:
        with connect(cfg) as conn:
            cur = conn.execute(
                "SELECT id, date, kind, backend, model, json, generated_at"
                " FROM reports ORDER BY id"
            )
            for rid, date, kind, backend, model, blob, gen in cur.fetchall():
                try:
                    rep = json.loads(blob)
                except ValueError:
                    rep = {}
                latest[(date, kind, backend)] = {
                    "id": rid,
                    "date": date,
                    "kind": kind,
                    "backend": backend,
                    "model": model,
                    "generated_at": gen,
                    "report": rep,
                }
    except Exception:
        return []
    return list(latest.values())


def reports_list_payload(cfg: Config) -> dict:
    """Every available report: latest DB row per (date, kind, backend) plus any
    markdown-only reports (weekly/morning) on disk. Newest date first."""
    items: list[dict] = []
    seen_md: set = set()
    for r in _db_reports(cfg):
        rep = r.get("report") or {}
        mdp = _md_path(cfg, r["date"], r["kind"])
        has_md = mdp.is_file()
        if has_md:
            seen_md.add((r["date"], r["kind"]))
        items.append(
            {
                "date": r["date"],
                "kind": r["kind"],
                "backend": r["backend"],
                "model": r["model"],
                "overall_score": rep.get("overall_score"),
                "scored": rep.get("scored"),
                "generated_at": r["generated_at"],
                "has_markdown": has_md,
                "md_path": str(mdp) if has_md else None,
            }
        )

    # Markdown-only reports (e.g. weekly/morning have no DB row).
    rdir = Path(cfg.reports_dir)
    if rdir.is_dir():
        for p in sorted(rdir.glob("*.md")):
            stem = p.stem  # <date>-<kind>
            if "-" not in stem:
                continue
            date, _, kind = stem.rpartition("-")
            if kind not in _MD_KINDS or (date, kind) in seen_md:
                continue
            items.append(
                {
                    "date": date,
                    "kind": kind,
                    "backend": None,
                    "model": None,
                    "overall_score": None,
                    "scored": None,
                    "generated_at": None,
                    "has_markdown": True,
                    "md_path": str(p),
                }
            )

    items.sort(key=lambda i: (i["date"], i["kind"], i.get("backend") or ""), reverse=True)
    return {"count": len(items), "reports": items}


def report_show_payload(cfg: Config, date: str, kind: str) -> dict:
    """The stored report for (date, kind): the latest structured Report from the
    DB (narrative, score, alignments, drift/suggestions, cost/latency) merged
    with the markdown path + text when present. Missing -> exists:false."""
    candidates = [
        r for r in _db_reports(cfg) if r["date"] == date and r["kind"] == kind
    ]
    mdp = _md_path(cfg, date, kind)
    md_text = None
    if mdp.is_file():
        try:
            md_text = mdp.read_text(encoding="utf-8")
        except OSError:
            md_text = None

    if not candidates and md_text is None:
        return {"date": date, "kind": kind, "exists": False}

    out: dict = {"date": date, "kind": kind, "exists": True}
    if candidates:
        chosen = max(candidates, key=lambda r: r["id"])
        rep = chosen.get("report") or {}
        out.update(
            {
                "backend": chosen["backend"],
                "model": chosen["model"],
                "generated_at": chosen["generated_at"],
                "overall_score": rep.get("overall_score"),
                "scored": rep.get("scored"),
                "narrative": rep.get("narrative"),
                "drift_flags": rep.get("drift_flags", []),
                "suggestions": rep.get("suggestions", []),
                "alignments": rep.get("alignments", []),
                "tokens_in": rep.get("tokens_in"),
                "tokens_out": rep.get("tokens_out"),
                "cost_usd": rep.get("cost_usd"),
                "latency_s": rep.get("latency_s"),
                "available_backends": sorted({c["backend"] for c in candidates}),
            }
        )
    else:
        out.update({"backend": None, "model": None, "generated_at": None})
    out["md_path"] = str(mdp) if md_text is not None else None
    out["markdown"] = md_text
    return out


# --- trend -------------------------------------------------------------------


def trend_payload(cfg: Config, days: int) -> dict:
    """Per-day rollup for the trailing `days` (oldest first): date, score
    (nullable), scored, active_minutes, per-goal minutes/pct, and the count of
    user corrections filed for that day. Reuses align.score_day so each day's
    number matches status/review/EOD exactly."""
    from . import align as align_mod
    from . import classify as classify_mod
    from . import labels as labels_mod
    from . import learn as learn_mod
    from .compare import align as kw_align
    from .store import load_timeline

    goals = kw_align.load_goals(cfg)
    all_labels = labels_mod.load_labels(cfg)
    labels_by_id = labels_mod.labels_by_session(cfg, labels=all_labels)
    labels_by_fp = labels_mod.labels_by_fingerprint(cfg, labels=all_labels)
    rules = learn_mod.active_rules(cfg)
    llm_verdicts = classify_mod.load_verdicts(cfg)

    # corrections filed per day (user labels only), keyed by the session's day.
    corrections: dict[str, int] = {}
    for rec in all_labels:
        if rec.get("source") != "user":
            continue
        d = labels_mod._label_day(rec)
        if d is not None:
            corrections[d.isoformat()] = corrections.get(d.isoformat(), 0) + 1

    from_iso, to_iso = _window(days, None)
    base = _date.fromisoformat(to_iso)
    out_days: list[dict] = []
    for i in range(max(1, days) - 1, -1, -1):  # oldest -> newest
        d = (base - timedelta(days=i)).isoformat()
        tl = load_timeline(cfg, d)
        if tl is None:
            out_days.append(
                {
                    "date": d,
                    "score": None,
                    "scored": False,
                    "active_minutes": 0.0,
                    "goals": [],
                    "corrections": corrections.get(d, 0),
                }
            )
            continue
        day = align_mod.score_day(tl, goals, labels_by_id, rules, labels_by_fp=labels_by_fp,
                                  llm_verdicts=llm_verdicts)
        out_days.append(
            {
                "date": d,
                "score": day["overall"],
                "scored": day["scored"],
                "active_minutes": day["active_minutes"],
                "goals": [
                    {
                        "goal_id": a.goal_id,
                        "goal_name": a.goal_name,
                        "minutes": a.minutes,
                        "pct_time": a.pct_time,
                        "target_pct": a.target_pct,
                    }
                    for a in day["alignments"]
                ],
                "corrections": corrections.get(d, 0),
            }
        )

    return {"days": max(1, days), "from": from_iso, "to": to_iso, "trend": out_days}
