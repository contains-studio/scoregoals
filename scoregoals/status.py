"""scoregoals.status — assemble the live snapshot the menu bar app polls.

`scoregoals status --json` prints ONE JSON object (schema_version=1, documented in
docs/STATUS_SCHEMA.md) to stdout. Design rules:

* Never crash. Every section is guarded; a failure appends a string to
  `warnings` (and a line to stderr) and falls back to nulls/zeros. The command
  always exits 0 with valid JSON.
* Fast. Prefer the cached timeline; probe external services with short
  timeouts; read a short recent screenpipe window (not the whole day) for
  "now".
* Deterministic where it can be: the day/week scores reuse compare.align, so
  the number here matches the end-of-day report exactly.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .models import DayTimeline

SCHEMA_VERSION = 1
# Day/week "on track" cutoff for the boolean flags and on_track_days count.
ON_TRACK_SCORE = 60
# How far back "now" looks for current activity.
NOW_WINDOW_MIN = 10
WEEK_DAYS = 7
_SPARK = "▁▂▃▄▅▆▇█"
_SPARK_GAP = "·"  # a day with no data


def _warn(warnings: list[str], msg: str) -> None:
    warnings.append(msg)
    print(f"[scoregoals.status] {msg}", file=sys.stderr)


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_ts(ts: str | None) -> datetime | None:
    """ISO string -> naive local datetime (aware inputs converted); None on junk."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# --- external probes ---------------------------------------------------------


def _http_ok(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 localhost
            return 200 <= int(resp.status) < 500
    except Exception:
        return False


def _probe_screenpipe(config: Config) -> tuple[bool, str]:
    url = f"{config.screenpipe_url}/health"
    if _http_ok(url, timeout=1.5):
        return True, f"reachable at {config.screenpipe_url}"
    return False, f"not reachable at {config.screenpipe_url} (mock mode still works)"


def _probe_ollama(config: Config) -> tuple[bool, float | None]:
    """(ok, latency_s). latency is None when unreachable."""
    url = f"{config.ollama_url}/api/tags"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=2.5) as resp:  # noqa: S310 localhost
            resp.read(1)
            ok = 200 <= int(resp.status) < 500
    except Exception:
        return False, None
    return ok, round(time.monotonic() - t0, 3)


def _gemini_mode(config: Config) -> str:
    """'key' (GEMINI_API_KEY set), else 'cli' (gemini CLI on PATH), else 'off'."""
    if config.gemini_api_key:
        return "key"
    import shutil

    if shutil.which("gemini"):
        return "cli"
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if (Path(d) / "gemini").exists():
            return "cli"
    return "off"


# --- filesystem-derived health ----------------------------------------------


def _data_dir_mb(config: Config) -> float:
    total = 0
    root = Path(config.data_dir)
    if root.is_dir():
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    return round(total / (1024 * 1024), 2)


def _last_capture(config: Config) -> str | None:
    """generated_at of the most-recently-written timeline file (mtime picks the
    file; generated_at is read from it, falling back to the file's mtime)."""
    tdir = Path(config.timeline_dir)
    if not tdir.is_dir():
        return None
    files = [p for p in tdir.glob("*.json") if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
        ga = data.get("generated_at")
        if isinstance(ga, str) and ga:
            return ga
    except (OSError, ValueError):
        pass
    return datetime.fromtimestamp(newest.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def _gemini_cost_today(config: Config, date: str) -> float:
    """Sum cost_usd for today's gemini rows in benchmarks/compare.csv."""
    import csv

    path = Path(config.benchmarks_dir) / "compare.csv"
    if not path.is_file():
        return 0.0
    total = 0.0
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("date") == date and str(row.get("backend", "")).startswith("gemini"):
                    try:
                        total += float(row.get("cost_usd") or 0.0)
                    except (TypeError, ValueError):
                        continue
    except OSError:
        return 0.0
    return round(total, 6)


# --- sparkline ---------------------------------------------------------------


def _sparkline(scores: list[int | None]) -> str:
    out = []
    for s in scores:
        if s is None:
            out.append(_SPARK_GAP)
        else:
            idx = min(len(_SPARK) - 1, max(0, int(s / 100 * len(_SPARK))))
            out.append(_SPARK[idx])
    return "".join(out)


# --- timeline loading --------------------------------------------------------


def _load_or_build_timeline(config: Config, date: str, warnings: list[str]) -> DayTimeline:
    from .store import load_timeline

    try:
        tl = load_timeline(config, date)
        if tl is not None:
            return tl
    except Exception as exc:
        _warn(warnings, f"could not load timeline for {date} ({exc})")

    # No cache yet — build a partial one (sources that are down just return []).
    try:
        from .aggregate import timeline as timeline_mod

        return timeline_mod.build(date, config)
    except Exception as exc:
        _warn(warnings, f"could not build timeline for {date} ({exc}); using empty day")
        return DayTimeline(date=date)


# --- section builders --------------------------------------------------------


def _build_now(config: Config, goals, goals_by_id, warnings: list[str]) -> dict:
    default = {
        "app": None, "title": None, "goal_id": None, "goal_name": None,
        "on_task": False, "category": None, "since": None, "minutes": 0.0,
        "source": "unknown",
    }
    try:
        from .aggregate.segment import segment
        from .compare import align
        from .sources import screenpipe

        end = datetime.now().astimezone()
        start = end - timedelta(minutes=NOW_WINDOW_MIN)
        records = screenpipe.fetch(
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
            config,
        )
        sessions = segment(records)
        if sessions:
            cur = max(sessions, key=lambda s: s.minutes)
            hay = " ".join(p for p in (cur.app, cur.title, cur.category, cur.project) if p)
            gid = align.match_text(hay, goals)
            goal = goals_by_id.get(gid)
            return {
                "app": cur.app,
                "title": cur.title,
                "goal_id": gid,
                "goal_name": goal.name if goal else None,
                "on_task": gid is not None,
                "category": cur.category,
                "since": cur.start or None,
                "minutes": round(float(cur.minutes), 1),
                "source": "screenpipe",
            }
        # No recent activity: distinguish "reachable but idle" from "down".
        ok, _ = _probe_screenpipe(config)
        out = dict(default)
        out["source"] = "idle" if ok else "unknown"
        return out
    except Exception as exc:
        _warn(warnings, f"now-section failed ({exc})")
        return dict(default)


def _build_next_event(config: Config, timeline: DayTimeline, date: str, warnings: list[str]):
    try:
        from .sources import calendar as calendar_mod

        events = []
        try:
            events = list(calendar_mod.fetch(date, config) or [])
        except Exception:
            events = []
        if not events:
            events = list(timeline.calendar or [])

        now_dt = datetime.now()
        best = None
        best_dt = None
        for ev in events:
            sdt = _parse_ts(getattr(ev, "start", None))
            if sdt is None or sdt < now_dt:
                continue
            if best_dt is None or sdt < best_dt:
                best, best_dt = ev, sdt
        if best is None or best_dt is None:
            return None
        return {
            "title": getattr(best, "title", None) or (getattr(best, "text", "") or "")[:80] or "event",
            "start": getattr(best, "start", None),
            "minutes_until": round((best_dt - now_dt).total_seconds() / 60),
        }
    except Exception as exc:
        _warn(warnings, f"next-event failed ({exc})")
        return None


def _build_week(config: Config, goals, date: str, today_tl: DayTimeline,
                labels_by_id: dict, rules: list, labels_by_fp: dict,
                warnings: list[str], llm_verdicts: dict | None = None) -> dict:
    """Per-day scores for the trailing WEEK_DAYS, using the SAME corrections-aware,
    min-data-guarded path as the headline (align.score_day) so a day's week cell
    can never contradict its own score. A day below MIN_ACTIVE_MINUTES (or with
    no timeline) is None — an empty/short day is unknown, not a zero."""
    from . import align as align_mod
    from .store import load_timeline

    scores: list[int | None] = []
    try:
        base = _date.fromisoformat(date)
    except ValueError:
        base = _date.today()
    for i in range(WEEK_DAYS - 1, -1, -1):  # oldest -> newest
        d = (base - timedelta(days=i)).isoformat()
        try:
            tl = today_tl if (d == date and today_tl.sessions) else load_timeline(config, d)
            if tl is None:
                scores.append(None)
                continue
            day = align_mod.score_day(tl, goals, labels_by_id, rules,
                                      labels_by_fp=labels_by_fp, llm_verdicts=llm_verdicts)
            scores.append(day["overall"] if day["scored"] else None)
        except Exception as exc:
            _warn(warnings, f"week score for {d} failed ({exc})")
            scores.append(None)
    on_track_days = sum(1 for s in scores if s is not None and s >= ON_TRACK_SCORE)
    return {"scores": scores, "on_track_days": on_track_days, "sparkline": _sparkline(scores)}


def _build_health(config: Config, date: str, warnings: list[str]) -> dict:
    sp_ok, sp_detail = _probe_screenpipe(config)
    ol_ok, ol_latency = _probe_ollama(config)
    return {
        "screenpipe": {"ok": sp_ok, "detail": sp_detail},
        "backend": {
            "default": config.default_backend,
            "ollama_ok": ol_ok,
            "ollama_latency_s": ol_latency,
            "gemini": _gemini_mode(config),
        },
        "last_capture": _last_capture(config),
        "gemini_cost_today_usd": _gemini_cost_today(config, date),
        "data_dir_mb": _data_dir_mb(config),
        "capture_paused": config.capture_paused,
        "nudges_enabled": config.nudges_enabled,
    }


# --- top-level ---------------------------------------------------------------


def build(config: Config, date: str) -> dict:
    """Assemble the full status dict for `date`. Never raises."""
    warnings: list[str] = []

    from .compare import align

    try:
        goals = align.load_goals(config)
    except Exception as exc:
        _warn(warnings, f"goals not loaded ({exc})")
        goals = []
    goals_by_id = {g.id: g for g in goals}

    timeline = _load_or_build_timeline(config, date, warnings)

    # Corrected scoring: user labels + learned rules override keyword matches,
    # not_work sessions are excluded, and below MIN_ACTIVE_MINUTES the day is
    # unscored (overall=null, scored=false) — honest uncertainty.
    try:
        from . import labels as labels_mod

        all_labels = labels_mod.load_labels(config)
        labels_by_id = labels_mod.labels_by_session(config, labels=all_labels)
        labels_by_fp = labels_mod.labels_by_fingerprint(config, labels=all_labels)
    except Exception as exc:
        _warn(warnings, f"labels not loaded ({exc})")
        all_labels, labels_by_id, labels_by_fp = [], {}, {}

    try:
        from . import learn as learn_mod

        rules = learn_mod.active_rules(config)
    except Exception as exc:
        _warn(warnings, f"learned rules not loaded ({exc})")
        rules = []

    # local-LLM classification tier: status is a hot polling path (the menu bar
    # app polls every ~30s with a 5s timeout), so it must NEVER call the model —
    # it only READS the verdict cache. Fresh classification runs in the
    # background capture job (cmd_capture) and repopulates the cache for the
    # next poll. A missing cache just means those sessions read as unmatched
    # until the next capture; the number is honest and the call stays instant.
    try:
        from . import classify as classify_mod

        llm_verdicts = classify_mod.load_verdicts(config)
    except Exception as exc:
        _warn(warnings, f"llm verdict cache skipped ({exc})")
        llm_verdicts = {}

    from . import align as align_mod

    scored = True
    stats = timeline.stats if isinstance(timeline.stats, dict) else {}
    try:
        day = align_mod.score_day(timeline, goals, labels_by_id, rules,
                                  labels_by_fp=labels_by_fp, llm_verdicts=llm_verdicts)
        alignments = day["alignments"]
        overall = day["overall"]
        scored = day["scored"]
        active_minutes = day["active_minutes"]
        projects_out = day.get("projects", [])
        project_minutes = day.get("project_minutes", 0.0)
    except Exception as exc:
        _warn(warnings, f"scoring failed ({exc})")
        alignments, overall, scored = [], None, False
        projects_out = []
        project_minutes = 0.0
        try:
            active_minutes = round(float(stats.get("total_active_minutes", 0) or 0), 1)
        except (TypeError, ValueError):
            active_minutes = 0.0

    goals_out = [
        {
            "goal_id": a.goal_id,
            "goal_name": a.goal_name,
            "minutes": a.minutes,
            "pct_time": a.pct_time,
            "target_pct": a.target_pct,
            "on_track": a.on_track,
        }
        for a in alignments
    ]

    try:
        review_rows = align_mod.resolve_day(timeline, goals, labels_by_id, rules,
                                            labels_by_fp=labels_by_fp,
                                            llm_verdicts=llm_verdicts)
        needs_review = sum(1 for r in review_rows if r["needs_review"])
    except Exception as exc:
        _warn(warnings, f"review summary failed ({exc})")
        needs_review = 0

    try:
        corrections_week = labels_mod.corrections_in_week(all_labels, date)
        corr_by_week = labels_mod.corrections_by_week(all_labels)
    except Exception as exc:
        _warn(warnings, f"corrections rollup failed ({exc})")
        corrections_week, corr_by_week = 0, []

    try:
        drift = align.drift_flags(timeline, goals, alignments)
    except Exception as exc:
        _warn(warnings, f"drift flags failed ({exc})")
        drift = []

    now = _build_now(config, goals, goals_by_id, warnings)

    try:
        from . import intentions

        intentions_block = intentions.block(config, date, timeline=timeline, goals=goals,
                                            llm_verdicts=llm_verdicts)
    except Exception as exc:
        _warn(warnings, f"intentions failed ({exc})")
        intentions_block = {"date": date, "set_at": None, "items": []}

    try:
        from . import focus as focus_mod

        focus_block = focus_mod.load(config)
    except Exception as exc:
        _warn(warnings, f"focus failed ({exc})")
        focus_block = {"active": False, "goal_id": None, "goal_name": None,
                       "started_at": None, "until": None}

    next_event = _build_next_event(config, timeline, date, warnings)
    week = _build_week(config, goals, date, timeline, labels_by_id, rules,
                       labels_by_fp, warnings, llm_verdicts=llm_verdicts)

    try:
        health = _build_health(config, date, warnings)
    except Exception as exc:
        _warn(warnings, f"health failed ({exc})")
        health = {}

    return {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at": _iso_now(),
        "now": now,
        "score": {
            "overall": overall,
            "scored": scored,
            "on_track": bool(scored and overall is not None and overall >= ON_TRACK_SCORE),
            "active_minutes": active_minutes,
            "project_minutes": project_minutes,
        },
        "goals": goals_out,
        # Tracked projects (name + minutes only — no target, no judgment). A
        # separate top-level key so goals[] stays goals-only for the menu bar.
        "projects": projects_out,
        "drift_flags": drift,
        "review": {"needs_review": needs_review},
        "corrections_this_week": corrections_week,
        "learning": {"active_rules": len(rules), "corrections_by_week": corr_by_week},
        "intentions": intentions_block,
        "focus": focus_block,
        "next_event": next_event,
        "week": week,
        "health": health,
        "warnings": warnings,
    }


def build_json(config: Config, date: str) -> str:
    """build() -> pretty JSON string. Falls back to a minimal valid object if
    even the assembly (or serialization) fails, so stdout is always JSON."""
    try:
        snapshot = build(config, date)
        return json.dumps(snapshot, indent=2, ensure_ascii=False)
    except Exception as exc:  # last-ditch: still emit valid JSON
        print(f"[scoregoals.status] fatal ({exc})", file=sys.stderr)
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "date": date,
                "generated_at": _iso_now(),
                "warnings": [f"fatal: {exc}"],
            },
            indent=2,
            ensure_ascii=False,
        )
