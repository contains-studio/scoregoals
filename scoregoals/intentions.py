"""Daily intentions — up to three things Michael means to do today.

State lives in data/intentions/<date>.json:
    {"date": "YYYY-MM-DD", "set_at": "ISO|null",
     "items": [{"id": str, "text": str, "goal_id": str|null,
                "done": bool, "created_at": "ISO"}]}

Each item is best-effort auto-linked to a goal — exact keyword match first (the
same matcher as the day score), then a fuzzy fallback that bridges typos and
synonyms (so "ship screengoals" links to ``ship-scoregoals``). `today --json` /
`status` then attribute today's tracked minutes and apps back to the intention
using the RESOLVED verdicts (label > rule > keyword > llm) — not a raw keyword
false-match — plus any session the local LLM linked to the intention by id.
Everything degrades gracefully: no goals, no timeline, or a missing file all
yield an empty-but-valid block.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from .config import Config
from .models import iso_now

MAX_ITEMS = 3
HISTORY_DAYS = 7


def _today() -> str:
    return _date.today().isoformat()


def _prev_day(date: str) -> str:
    try:
        return (_date.fromisoformat(date) - timedelta(days=1)).isoformat()
    except ValueError:
        return date


def _dir(config: Config) -> Path:
    return Path(config.data_dir) / "intentions"


def _path(config: Config, date: str) -> Path:
    return _dir(config) / f"{date}.json"


def _warn(msg: str) -> None:
    print(f"[scoregoals.intentions] {msg}", file=sys.stderr)


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _empty(date: str) -> dict:
    return {"date": date, "set_at": None, "items": []}


def load(config: Config, date: str) -> dict:
    """Read the raw intentions record for `date` (never raises)."""
    path = _path(config, date)
    if not path.is_file():
        return _empty(date)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(f"ignoring bad {path} ({exc})")
        return _empty(date)
    if not isinstance(data, dict):
        return _empty(date)
    items = data.get("items")
    clean_items: list[dict] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            clean_items.append(
                {
                    "id": str(it.get("id") or _new_id()),
                    "text": str(it.get("text") or "").strip(),
                    "goal_id": it.get("goal_id"),
                    "done": bool(it.get("done")),
                    "created_at": it.get("created_at") or iso_now(),
                    "carried_from": it.get("carried_from"),
                }
            )
    return {
        "date": data.get("date") or date,
        "set_at": data.get("set_at"),
        "items": clean_items,
    }


def _save(config: Config, date: str, record: dict) -> dict:
    path = _path(config, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def _load_goals(config: Config, goals):
    if goals is not None:
        return goals
    from .compare import align

    return align.load_goals(config)


import difflib
import re as _re

_FUZZY_THRESHOLD = 0.8


def _norm_tokens(s: str | None) -> list[str]:
    """Lowercased alphanumeric tokens (>= 3 chars) for fuzzy matching."""
    return [t for t in _re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 3]


def _fuzzy_link(text: str, goals) -> str | None:
    """Fuzzy fallback for intention→goal linking: match text tokens against each
    goal's id/name/keyword tokens by edit-distance ratio, so a typo like
    "ship screengoals" still links to the ``ship-scoregoals`` goal. Returns the
    best goal id at/above _FUZZY_THRESHOLD, else None."""
    ttoks = _norm_tokens(text)
    if not ttoks:
        return None
    best_gid: str | None = None
    best_score = 0.0
    for g in goals:
        gtoks = set(_norm_tokens(g.id)) | set(_norm_tokens(g.name))
        for kw in g.keywords:
            gtoks |= set(_norm_tokens(kw))
        if not gtoks:
            continue
        score = 0.0
        for tt in ttoks:
            r = max(
                (difflib.SequenceMatcher(None, tt, gt).ratio() for gt in gtoks),
                default=0.0,
            )
            score = max(score, r)
        if score > best_score:
            best_gid, best_score = g.id, score
    return best_gid if best_score >= _FUZZY_THRESHOLD else None


def _link(text: str, goals) -> str | None:
    """Auto-link an intention to a goal: exact keyword match first (identical to
    the day-score matcher), then a fuzzy fallback that bridges typos/synonyms."""
    from .compare import align

    return align.match_text(text, goals) or _fuzzy_link(text, goals)


def relink(config: Config, date: str, goals=None, record: dict | None = None) -> dict:
    """Fill ``goal_id`` on today's still-UNLINKED items via fuzzy matching and
    persist if anything changed. Never overwrites an existing link. Returns the
    (possibly updated) record. This is how "ship screengoals" (set before the
    fuzzy linker existed) heals into ``ship-scoregoals`` on the next read."""
    goals = _load_goals(config, goals)
    if record is None:
        record = load(config, date)
    changed = False
    for it in record["items"]:
        if not it.get("goal_id"):
            gid = _link(it.get("text", ""), goals)
            if gid:
                it["goal_id"] = gid
                changed = True
    if changed:
        _save(config, date, record)
    return record


def set_items(config: Config, date: str, texts: list[str], goals=None) -> dict:
    """Replace the day's intentions with up to MAX_ITEMS non-empty `texts`,
    auto-linking each to a goal. Resets set_at to now."""
    goals = _load_goals(config, goals)
    items: list[dict] = []
    for text in texts:
        text = (text or "").strip()
        if not text:
            continue
        items.append(
            {
                "id": _new_id(),
                "text": text,
                "goal_id": _link(text, goals),
                "done": False,
                "created_at": iso_now(),
                "carried_from": None,
            }
        )
        if len(items) >= MAX_ITEMS:
            break
    return _save(config, date, {"date": date, "set_at": iso_now(), "items": items})


def add_item(config: Config, date: str, text: str, goal_id: str | None = None, goals=None) -> dict:
    """Append one intention (capped at MAX_ITEMS). An explicit goal_id wins;
    otherwise the text is auto-linked."""
    text = (text or "").strip()
    if not text:
        raise ValueError("intention text is empty")
    record = load(config, date)
    if len(record["items"]) >= MAX_ITEMS:
        raise ValueError(f"already at the {MAX_ITEMS}-intention limit — clear or toggle first")
    goals = _load_goals(config, goals)
    record["items"].append(
        {
            "id": _new_id(),
            "text": text,
            "goal_id": goal_id or _link(text, goals),
            "done": False,
            "created_at": iso_now(),
            "carried_from": None,
        }
    )
    if not record.get("set_at"):
        record["set_at"] = iso_now()
    return _save(config, date, record)


def toggle(config: Config, date: str, id_or_index: str) -> dict | None:
    """Flip done on the item whose id equals `id_or_index`, or (fallback) the
    1-based index into the current list. Returns the toggled item, or None if
    nothing matched."""
    record = load(config, date)
    items = record["items"]
    target: dict | None = None
    for it in items:
        if it["id"] == str(id_or_index):
            target = it
            break
    if target is None:
        try:
            idx = int(id_or_index)
        except (TypeError, ValueError):
            idx = 0
        if 1 <= idx <= len(items):
            target = items[idx - 1]
    if target is None:
        return None
    target["done"] = not target["done"]
    _save(config, date, record)
    return target


def clear(config: Config, date: str) -> dict:
    """Remove all intentions for the day."""
    return _save(config, date, _empty(date))


def _carryover_items(config: Config, date: str, goals) -> list[dict]:
    """Yesterday's UNDONE intentions, rebuilt as fresh items for `date` and
    tagged with meta `carried_from` = the previous day. Empty when yesterday had
    no undone work (or no file)."""
    prev = _prev_day(date)
    out: list[dict] = []
    for it in load(config, prev)["items"]:
        text = (it.get("text") or "").strip()
        if it.get("done") or not text:
            continue
        out.append(
            {
                "id": _new_id(),
                "text": text,
                "goal_id": it.get("goal_id") or _link(text, goals),
                "done": False,
                "created_at": iso_now(),
                "carried_from": prev,
            }
        )
    return out


def prefill(config: Config, date: str, texts: list[str], goals=None) -> dict:
    """Seed up to MAX_ITEMS intentions ONLY when the day has none yet (used by
    the morning plan). Yesterday's UNDONE items are carried over FIRST (tagged
    with `carried_from`), then `texts` fill any remaining slots. Text-level
    de-dup means a suggestion already carried over is never added twice.
    Returns the resulting record either way."""
    record = load(config, date)
    if record["items"]:
        return record

    goals = _load_goals(config, goals)
    items: list[dict] = []
    seen: set[str] = set()
    for item in _carryover_items(config, date, goals):
        key = item["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    for text in texts:
        text = (text or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        items.append(
            {
                "id": _new_id(),
                "text": text,
                "goal_id": _link(text, goals),
                "done": False,
                "created_at": iso_now(),
                "carried_from": None,
            }
        )
        if len(items) >= MAX_ITEMS:
            break

    return _save(config, date, {"date": date, "set_at": iso_now(), "items": items})


def _attribute(config: Config, date: str, timeline, goals, items: list[dict],
               llm_verdicts: dict | None) -> dict[str, dict]:
    """Honest per-intention attribution keyed by intention id -> {minutes, apps}.

    A session counts toward AT MOST ONE intention:
      1. If its ``llm`` verdict carries an ``intention_id`` for one of today's
         intentions, the WHOLE session goes to that intention (an explicit
         semantic link — it wins over goal-share).
      2. Otherwise, if its RESOLVED verdict (label > rule > keyword > llm) names
         a goal an intention is linked to, its minutes are split evenly across
         the intentions sharing that goal (so their sum equals the goal's real
         minutes rather than double-counting).
    This replaces the old raw token-matching path: minutes now follow the same
    corrections-aware verdict the score uses, not a keyword false-match.
    """
    from . import align as align_mod
    from . import labels as labels_mod
    from . import learn as learn_mod

    out: dict[str, dict] = {it["id"]: {"minutes": 0.0, "apps": []} for it in items}
    if timeline is None or not items:
        return out

    if llm_verdicts is None:
        from . import classify as classify_mod

        llm_verdicts = classify_mod.load_verdicts(config)

    all_labels = labels_mod.load_labels(config)
    labels_by_id = labels_mod.labels_by_session(config, labels=all_labels)
    labels_by_fp = labels_mod.labels_by_fingerprint(config, labels=all_labels)
    rules = learn_mod.active_rules(config)

    intent_ids = {it["id"] for it in items}
    goal_to_intents: dict[str, list[str]] = {}
    for it in items:
        gid = it.get("goal_id")
        if gid:
            goal_to_intents.setdefault(gid, []).append(it["id"])

    def _add(iid: str, minutes: float, app) -> None:
        entry = out[iid]
        entry["minutes"] += minutes
        if app and app not in entry["apps"]:
            entry["apps"].append(app)

    for s in timeline.sessions:
        r = align_mod.resolve_session(
            s, goals, labels_by_id, rules, date=date,
            labels_by_fp=labels_by_fp, llm_verdicts=llm_verdicts,
        )
        mins = float(s.minutes)
        app = s.app
        # 1) explicit llm intention link wins (whole session, no goal-share).
        cached = llm_verdicts.get(r["session_id"]) if llm_verdicts else None
        cand = cached.get("intention_id") if isinstance(cached, dict) else None
        if cand in intent_ids:
            _add(cand, mins, app)
            continue
        # 2) goal-share via the resolved (active-goal) verdict.
        gid = r["goal_id"]
        sharers = goal_to_intents.get(gid) if gid else None
        if sharers:
            share = len(sharers)
            for iid in sharers:
                _add(iid, mins / share, app)

    for entry in out.values():
        entry["minutes"] = round(entry["minutes"], 1)
    return out


def block(config: Config, date: str, timeline=None, goals=None,
          llm_verdicts: dict | None = None) -> dict:
    """The enriched intentions block for `today --json` / `status`: each item
    gains goal_name, attributed_minutes, and the apps that earned that time
    today.

    Attribution is honest: an intention's minutes are the RESOLVED verdicts of
    today's sessions (label > rule > keyword > llm), plus any session the local
    LLM linked to that intention by id — see ``_attribute``. Unlinked items are
    fuzzily relinked first (so a typo'd intention self-heals), then attributed.
    """
    record = load(config, date)
    goals = _load_goals(config, goals)
    goals_by_id = {g.id: g for g in goals}

    # Heal typo'd/unlinked intentions before attributing (persists if changed).
    try:
        record = relink(config, date, goals=goals, record=record)
    except Exception as exc:  # never let relinking break the block
        _warn(f"relink failed ({exc})")

    if timeline is None:
        from .store import load_timeline

        timeline = load_timeline(config, date)

    attribution: dict[str, dict] = {}
    try:
        attribution = _attribute(config, date, timeline, goals, record["items"], llm_verdicts)
    except Exception as exc:  # never let attribution math break the block
        _warn(f"attribution failed ({exc})")
        attribution = {it["id"]: {"minutes": 0.0, "apps": []} for it in record["items"]}

    items_out: list[dict] = []
    for it in record["items"]:
        gid = it.get("goal_id")
        goal = goals_by_id.get(gid) if gid else None
        attr = attribution.get(it["id"], {})
        items_out.append(
            {
                "id": it["id"],
                "text": it["text"],
                "goal_id": gid,
                "goal_name": goal.name if goal else None,
                "done": bool(it["done"]),
                "attributed_minutes": round(float(attr.get("minutes", 0.0)), 1),
                "apps": list(attr.get("apps", [])),
                "carried_from": it.get("carried_from"),
            }
        )
    return {
        "date": record["date"],
        "set_at": record.get("set_at"),
        "items": items_out,
        "history_summary": history_summary(config, days=HISTORY_DAYS, end_date=date),
    }


def history_summary(config: Config, days: int = HISTORY_DAYS, end_date: str | None = None) -> dict:
    """Cheap completion-rate rollup over the last `days` (file reads only, no
    timeline/attribution). completion_rate = done_items / total_items across the
    window (0.0 when the window has no items)."""
    end = end_date or _today()
    try:
        base = _date.fromisoformat(end)
    except ValueError:
        base = _date.today()
    total = 0
    done = 0
    for i in range(max(1, days)):
        rec = load(config, (base - timedelta(days=i)).isoformat())
        for it in rec["items"]:
            total += 1
            if it.get("done"):
                done += 1
    rate = round(done / total, 3) if total else 0.0
    return {"days": days, "completion_rate": rate}


def history(config: Config, days: int = HISTORY_DAYS, end_date: str | None = None, goals=None) -> dict:
    """The intentions history for the last `days` ending at `end_date` (default
    today), newest day first. Each day carries its enriched items (text, done,
    attributed_minutes, carried_from) plus n_done/n_total, and the block ends
    with an overall completion-rate summary.
    """
    end = end_date or _today()
    try:
        base = _date.fromisoformat(end)
    except ValueError:
        base = _date.today()
    goals = _load_goals(config, goals)

    days_list: list[dict] = []
    total = 0
    done = 0
    for i in range(max(1, days)):
        d = (base - timedelta(days=i)).isoformat()
        blk = block(config, d, goals=goals)
        items = [
            {
                "id": it["id"],
                "text": it["text"],
                "done": it["done"],
                "attributed_minutes": it["attributed_minutes"],
                "goal_name": it.get("goal_name"),
                "carried_from": it.get("carried_from"),
            }
            for it in blk["items"]
        ]
        n_total = len(items)
        n_done = sum(1 for it in items if it["done"])
        total += n_total
        done += n_done
        days_list.append(
            {
                "date": d,
                "set_at": blk.get("set_at"),
                "n_done": n_done,
                "n_total": n_total,
                "items": items,
            }
        )

    return {
        "days": days,
        "end_date": end,
        "items_total": total,
        "items_done": done,
        "completion_rate": round(done / total, 3) if total else 0.0,
        "days_list": days_list,
    }
