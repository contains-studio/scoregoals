"""scoregoals.classify — batched local-LLM session classification (the "llm" tier).

The deterministic authority order is: label > rule > keyword > **llm** > none
(see docs/PLAN-experience-and-learning.md and align.py). Keyword matching is
blind to meaning: a session titled "plan botme week" of real Claude planning
resolves to ``none`` because no goal keyword appears verbatim, and a personal
Messages chat can false-match a goal keyword. This module fills that gap with a
single, batched call to the local Ollama model — the same zero-cost, private
backend the reports use — classifying ONLY the sessions the deterministic tiers
left unresolved (or resolved with a low-confidence keyword collision).

Contract (``classify_unresolved``):
  in : timeline, goals, intentions block, cfg (+ optional labels/rules so the
       "already resolved" test matches align.py exactly)
  out: {session_id: {"verdict": goal_id|"off_track"|"not_work"|None,
                     "intention_id": str|None, "confidence": float}}
       for every candidate session that now has a cached verdict.

Rules that keep it safe and fast:
* Only sessions whose current resolution SOURCE is ``none`` OR whose confidence
  is <= the keyword-collision bar (0.4) are candidates. Label/implicit/system/
  rule/solid-keyword verdicts are authoritative and NEVER second-guessed.
* One batched ``/api/generate`` call (format json, low temperature) per
  invocation — never one call per session, never inside a tight loop.
* CACHE (``data/llm_verdicts.json``): a cached session is NEVER re-asked, so
  status polls stay fast and deterministic between captures. The cache is
  additive and corruption-tolerant.
* The pipeline must never block on the model: an unreachable Ollama, a
  disabled ``llm_classify`` setting, or an unparseable reply all yield ``{}``
  (with at most one stderr line), and the caller carries on with the
  deterministic score.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import Config
from .labels import NOT_WORK, OFF_TRACK, session_id_for
from .models import DayTimeline, Goal

__all__ = [
    "VERDICTS_FILENAME",
    "verdicts_path",
    "load_verdicts",
    "save_verdicts",
    "unresolved_session_ids",
    "classify_unresolved",
    "verdicts_for",
]

VERDICTS_FILENAME = "llm_verdicts.json"

# How many candidate sessions we ever put in one prompt. A day rarely has this
# many unresolved sessions; the cap is a guard against a pathological timeline
# blowing the context window (and it keeps the single call bounded).
_MAX_SESSIONS = 40
# Characters of redacted excerpt per session handed to the model.
_EXCERPT_CHARS = 200
_SPECIAL = frozenset({OFF_TRACK, NOT_WORK})


def _warn(msg: str) -> None:
    print(f"[scoregoals.classify] {msg}", file=sys.stderr)


# --- cache -------------------------------------------------------------------


def verdicts_path(config: Config) -> Path:
    return Path(config.data_dir) / VERDICTS_FILENAME


def load_verdicts(config: Config) -> dict[str, dict]:
    """Read data/llm_verdicts.json -> {session_id: {verdict, intention_id,
    confidence, model, ts}}. A missing or malformed file yields ``{}`` (one
    stderr line), so a corrupt cache can never break alignment or status."""
    path = verdicts_path(config)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(f"ignoring bad {path.name} ({exc})")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for sid, rec in data.items():
        if isinstance(rec, dict):
            out[str(sid)] = rec
    return out


def save_verdicts(config: Config, verdicts: dict[str, dict]) -> None:
    """Persist the verdict cache (additive; callers merge before saving)."""
    path = verdicts_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(verdicts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --- candidate selection -----------------------------------------------------


def unresolved_session_ids(
    timeline: DayTimeline,
    goals: list[Goal],
    labels_by_id: dict[str, dict],
    rules: list[dict],
    labels_by_fp: dict | None = None,
) -> dict[str, object]:
    """{session_id: session} for the sessions the deterministic tiers left
    unresolved — resolution source ``none`` or a low-confidence keyword
    collision (confidence <= 0.4). These are exactly what the llm tier may fill;
    everything else is already authoritative and is skipped."""
    from . import align as align_mod

    out: dict[str, object] = {}
    for s in timeline.sessions:
        r = align_mod.resolve_session(
            s, goals, labels_by_id, rules, date=timeline.date, labels_by_fp=labels_by_fp
        )
        if r["source"] == "none" or (
            r["source"] == "keyword" and r["confidence"] <= align_mod.CONF_KEYWORD_COLLISION
        ):
            out[str(r["session_id"])] = s
    return out


# --- prompt ------------------------------------------------------------------


def build_prompt(
    goals: list[Goal], intentions: list[dict], sessions: list[tuple[str, object]]
) -> str:
    """The classification prompt. Lists the ACTIVE goals, today's intentions
    (so the model can bridge typos/synonyms — "ship screengoals" -> the
    ship-scoregoals goal), and the candidate sessions, and asks for strict JSON.
    `sessions` is a list of (session_id, Session)."""
    from .aggregate.redact import redact_text

    lines: list[str] = []
    lines.append(
        "You are ScoreGoals' session classifier for Michael. Each session below is a "
        "block of real captured activity that the deterministic keyword matcher could "
        "NOT confidently assign to a goal. Decide, from the app/title/on-screen text, "
        "which GOAL (if any) each session advances."
    )
    lines.append("")
    lines.append("== GOALS (assign a session to one of these ids when it fits) ==")
    for g in goals:
        kws = ", ".join(g.keywords)
        desc = " ".join((g.description or "").split())[:200]
        lines.append(f"- id={g.id} | name={g.name}")
        if desc:
            lines.append(f"    about: {desc}")
        if kws:
            lines.append(f"    keywords: {kws}")
    lines.append("")
    lines.append(
        "== TODAY'S INTENTIONS (link a session to one when it is the work that "
        "intention names; bridge typos/synonyms, e.g. 'ship screengoals' == the "
        "ScoreGoals goal) =="
    )
    if intentions:
        for it in intentions:
            lines.append(f"- intention_id={it.get('id')} | text={it.get('text')}")
    else:
        lines.append("- (none set today)")
    lines.append("")
    lines.append("== SESSIONS TO CLASSIFY ==")
    for sid, s in sessions:
        app = getattr(s, "app", None) or "?"
        title = (getattr(s, "title", None) or "").strip()
        mins = round(float(getattr(s, "minutes", 0.0)), 1)
        excerpt = redact_text((getattr(s, "text_excerpt", "") or ""))[:_EXCERPT_CHARS]
        excerpt = " ".join(excerpt.split())
        lines.append(f"- session_id={sid} | app={app} | minutes={mins}")
        if title:
            lines.append(f"    title: {title}")
        if excerpt:
            lines.append(f"    text: {excerpt}")
    lines.append("")
    lines.append("== TASK ==")
    lines.append(
        "For EVERY session_id above, output exactly one assignment. Choose `verdict`:"
    )
    lines.append(
        "  - a goal id from the GOALS list, when the session advances that goal;"
    )
    lines.append(
        '  - "off_track" when it is real work but advances NO listed goal;'
    )
    lines.append(
        '  - "not_work" when it is personal / out of scope (e.g. a personal chat,'
        " a login screen, settings);"
    )
    lines.append(
        '  - "none" when you genuinely cannot tell.'
    )
    lines.append(
        "Set `intention_id` to a today's-intention id when the session is that "
        "intention's work, else null. `confidence` is 0.0-1.0 (your certainty)."
    )
    lines.append(
        "Respond with ONLY a JSON object, no markdown fences, exactly this shape: "
        '{"assignments": [{"session_id": "<id>", "verdict": "<goal-id|off_track|'
        'not_work|none>", "intention_id": "<id-or-null>", "confidence": <0-1>}]}'
    )
    return "\n".join(lines)


# --- classification ----------------------------------------------------------


def _normalize_verdict(raw: object, goal_ids: set[str]) -> object:
    """Map a model verdict string to a stored verdict: a known goal id, a
    special verdict, or None (for none/unknown/invalid). Returns the sentinel
    ``False`` when the string is non-empty but names an UNKNOWN goal id — the
    caller drops that row rather than inventing an assignment."""
    if raw is None:
        return None
    v = str(raw).strip()
    low = v.lower()
    if low in ("", "none", "null", "unknown", "unsure"):
        return None
    if low in _SPECIAL:
        return low
    if v in goal_ids:
        return v
    if low in goal_ids:
        return low
    return False  # non-empty but not a known id -> invalid, drop


def _coerce_assignments(
    parsed: dict,
    candidate_ids: set[str],
    goal_ids: set[str],
    intention_ids: set[str],
    model: str,
    ts: str,
) -> dict[str, dict]:
    """Validate the model's assignments into cache entries. Every row is checked
    against the known session/goal/intention ids; invalid rows are dropped."""
    rows = parsed.get("assignments") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "").strip()
        if sid not in candidate_ids or sid in out:
            continue
        verdict = _normalize_verdict(row.get("verdict"), goal_ids)
        if verdict is False:
            continue  # named an unknown goal id — drop
        intention_id = row.get("intention_id")
        if intention_id is not None:
            intention_id = str(intention_id).strip()
            if intention_id.lower() in ("", "null", "none"):
                intention_id = None
            elif intention_id not in intention_ids:
                intention_id = None  # unknown intention id -> just no link
        try:
            confidence = float(row.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        out[sid] = {
            "verdict": verdict,
            "intention_id": intention_id,
            "confidence": round(confidence, 2),
            "model": model,
            "ts": ts,
        }
    return out


def classify_unresolved(
    timeline: DayTimeline,
    goals: list[Goal],
    intentions: list[dict],
    cfg: Config,
    labels_by_id: dict[str, dict] | None = None,
    rules: list[dict] | None = None,
    labels_by_fp: dict | None = None,
) -> dict[str, dict]:
    """Classify the day's UNRESOLVED sessions with one batched local-LLM call.

    Returns {session_id: {verdict, intention_id, confidence}} for the candidate
    sessions that now carry a cached verdict (cache hits + anything new this
    call). Cached sessions are never re-asked. Honors ``cfg.llm_classify``;
    returns ``{}`` gracefully (one stderr line) when the setting is off, Ollama
    is unreachable, or the reply is unusable — the pipeline never blocks.
    """
    if not getattr(cfg, "llm_classify", True):
        return {}
    if labels_by_id is None or rules is None:
        from . import labels as labels_mod
        from . import learn as learn_mod

        all_labels = labels_mod.load_labels(cfg)
        labels_by_id = labels_mod.labels_by_session(cfg, labels=all_labels)
        labels_by_fp = labels_mod.labels_by_fingerprint(cfg, labels=all_labels)
        rules = learn_mod.active_rules(cfg)

    candidates = unresolved_session_ids(
        timeline, goals, labels_by_id, rules, labels_by_fp=labels_by_fp
    )
    candidate_ids = set(candidates)
    if not candidate_ids:
        return {}

    cache = load_verdicts(cfg)
    todo = {sid: s for sid, s in candidates.items() if sid not in cache}

    # Anything already cached is answered from the cache — never re-asked.
    result: dict[str, dict] = {
        sid: _public(cache[sid]) for sid in candidate_ids if sid in cache
    }
    if not todo:
        return result

    session_items = list(todo.items())[:_MAX_SESSIONS]
    goal_ids = {g.id for g in goals}
    intention_ids = {str(it.get("id")) for it in intentions if it.get("id")}
    prompt = build_prompt(goals, intentions, session_items)

    parsed = _call_ollama(prompt, cfg)
    if parsed is None:
        return result  # unreachable / unparseable — deterministic score stands

    from .models import iso_now

    asked_ids = {sid for sid, _ in session_items}
    ts = iso_now()
    new_rows = _coerce_assignments(parsed, asked_ids, goal_ids, intention_ids,
                                   model=cfg.ollama_model, ts=ts)
    # Cache a null verdict for any asked session the model didn't answer (or
    # answered invalidly), so it is NEVER re-asked — that keeps status polls
    # deterministic and free of repeat Ollama calls between captures.
    for sid in asked_ids:
        new_rows.setdefault(
            sid, {"verdict": None, "intention_id": None, "confidence": 0.0,
                  "model": cfg.ollama_model, "ts": ts}
        )

    cache.update(new_rows)  # additive: only fills sessions we just asked about
    save_verdicts(cfg, cache)
    for sid, rec in new_rows.items():
        result[sid] = _public(rec)
    return result


def verdicts_for(
    cfg: Config,
    timeline: DayTimeline,
    goals: list[Goal],
    labels_by_id: dict[str, dict],
    rules: list[dict],
    labels_by_fp: dict | None = None,
    intentions: list[dict] | None = None,
) -> dict[str, dict]:
    """Self-healing accessor used by the live surfaces (status/review/capture):
    make sure the day's unresolved sessions are classified (at most one batched
    call; a complete cache is a no-op), then return the full verdict cache to
    feed align.score_day / resolve_day. Never raises — any failure falls back to
    whatever is already cached so the deterministic score always renders."""
    try:
        classify_unresolved(
            timeline, goals, intentions or [], cfg,
            labels_by_id=labels_by_id, rules=rules, labels_by_fp=labels_by_fp,
        )
    except Exception as exc:  # never let the model block the pipeline
        _warn(f"classification skipped ({exc})")
    return load_verdicts(cfg)


def _public(rec: dict) -> dict:
    """The caller-facing view of a cache entry (drops model/ts bookkeeping)."""
    return {
        "verdict": rec.get("verdict"),
        "intention_id": rec.get("intention_id"),
        "confidence": rec.get("confidence", 0.0),
    }


def _call_ollama(prompt: str, cfg: Config) -> dict | None:
    """One batched /api/generate call (format json, low temperature). Returns
    the parsed JSON object, or None when Ollama is unreachable / the reply has
    no JSON object (one stderr line). Reuses ollama.py's extraction so
    think-tags and fences are handled identically to the report backend."""
    from .analyze.ollama import _extract_json, _post_json

    url = f"{cfg.ollama_url}/api/generate"
    payload = {
        "model": cfg.ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_ctx": int(cfg.raw.get("ollama_num_ctx", 8192)),
        },
    }
    context = f"ollama at {cfg.ollama_url}"
    try:
        status, body = _post_json(url, payload, context)
    except RuntimeError as exc:
        _warn(f"skipping llm tier — {exc}")
        return None
    if status != 200:
        _warn(f"skipping llm tier — ollama HTTP {status}")
        return None
    try:
        data = json.loads(body)
    except ValueError:
        _warn("skipping llm tier — non-JSON envelope from ollama")
        return None
    parsed = _extract_json(str(data.get("response") or ""))
    if parsed is None:
        _warn("skipping llm tier — no JSON object in model reply")
        return None
    return parsed
