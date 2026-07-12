"""Daily intentions — up to three things Michael means to do today.

State lives in data/intentions/<date>.json:
    {"date": "YYYY-MM-DD", "set_at": "ISO|null",
     "items": [{"id": str, "text": str, "goal_id": str|null,
                "done": bool, "created_at": "ISO"}]}

Each item is best-effort auto-linked to a goal (same keyword logic as the day
score), which lets `today --json` / `status` attribute today's tracked minutes
and apps back to the intention. Everything degrades gracefully: no goals, no
timeline, or a missing file all yield an empty-but-valid block.
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
    print(f"[dayloop.intentions] {msg}", file=sys.stderr)


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


def _link(text: str, goals) -> str | None:
    from .compare import align

    return align.match_text(text, goals)


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


def block(config: Config, date: str, timeline=None, goals=None) -> dict:
    """The enriched intentions block for `today --json` / `status`: each item
    gains goal_name, attributed_minutes, and the apps that earned that time
    today (by matching its goal_id to today's aligned sessions).

    A goal's tracked minutes are split **evenly** across the intentions that
    share its goal_id, so two intentions auto-linked to the same goal each show
    half its time rather than both claiming the full total (their sum stays
    equal to the goal's real minutes instead of double-counting). Apps stay
    shared — the same distinct apps earned that goal's time regardless of how
    many intentions point at it.
    """
    from collections import Counter

    from .compare import align

    record = load(config, date)
    goals = _load_goals(config, goals)
    goals_by_id = {g.id: g for g in goals}

    if timeline is None:
        from .store import load_timeline

        timeline = load_timeline(config, date)

    attribution: dict[str, dict] = {}
    if timeline is not None:
        try:
            attribution = align.attribute_sessions(timeline, goals)
        except Exception as exc:  # never let attribution math break the block
            _warn(f"attribution failed ({exc})")

    # How many intentions share each goal_id, so we can divide (not duplicate)
    # that goal's attributed minutes across them.
    share_counts = Counter(it.get("goal_id") for it in record["items"] if it.get("goal_id"))

    items_out: list[dict] = []
    for it in record["items"]:
        gid = it.get("goal_id")
        attr = attribution.get(gid, {}) if gid else {}
        goal = goals_by_id.get(gid) if gid else None
        share = share_counts.get(gid, 1) or 1
        items_out.append(
            {
                "id": it["id"],
                "text": it["text"],
                "goal_id": gid,
                "goal_name": goal.name if goal else None,
                "done": bool(it["done"]),
                "attributed_minutes": round(float(attr.get("minutes", 0.0)) / share, 1),
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
