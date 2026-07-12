"""scoregoals.learn — rule mining v1 (learning without the user).

A fingerprint pattern that Michael corrected/confirmed the **same way >= 3
times with zero contradictions** is promoted to a deterministic rule in
``data/learned_rules.json``. Rules apply before any keyword/LLM guess (see
align.py), cite the labels that created them, and **retire automatically** when
a later label contradicts them or when their goal is archived/removed.

The mined pattern is ``(app, dominant title token) -> verdict``. The title
token MUST be a real discriminating token: an app-only pattern (empty
``title_token``) is refused, because a rule with no token matches EVERY session
of that app and would rewrite a whole app's time from a handful of windowless
labels (e.g. 3 Chrome ``not_work`` labels deleting all Chrome active minutes).
Windowless sessions therefore never mint a rule; they still need per-session
labels.

File shape::

    {
      "rules": [
        {"rule": {"app": "Dayloop", "title_token": "settings",
                  "verdict": "ship-scoregoals"},
         "created_from": [{"session_id": "...", "ts": "..."}, ...],
         "created_at": "2026-07-11T22:00:00-07:00"}
      ],
      "retired": [
        {"rule": {...}, "created_from": [...], "created_at": "...",
         "reason": "contradicted" | "archived-goal",
         "retired_at": "2026-07-18T09:00:00-07:00"}
      ]
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import Config
from .labels import NOT_WORK, OFF_TRACK, load_labels
from .models import Goal, iso_now

__all__ = [
    "RULES_FILENAME",
    "MIN_SUPPORT",
    "rules_path",
    "load_rules",
    "active_rules",
    "mine",
]

RULES_FILENAME = "learned_rules.json"
MIN_SUPPORT = 3  # a pattern needs this many consistent labels to promote
_SPECIAL = (OFF_TRACK, NOT_WORK)


def _warn(msg: str) -> None:
    print(f"[scoregoals.learn] warning: {msg}", file=sys.stderr)


def rules_path(config: Config) -> Path:
    return Path(config.data_dir) / RULES_FILENAME


def load_rules(config: Config) -> dict:
    """Load learned_rules.json -> {"rules": [...], "retired": [...]}. A missing
    or malformed file yields empty lists (with a one-line warning) so a corrupt
    file can never break alignment."""
    path = rules_path(config)
    if not path.is_file():
        return {"rules": [], "retired": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(f"ignoring bad {path.name} ({exc})")
        return {"rules": [], "retired": []}
    if not isinstance(data, dict):
        return {"rules": [], "retired": []}
    rules = data.get("rules") if isinstance(data.get("rules"), list) else []
    retired = data.get("retired") if isinstance(data.get("retired"), list) else []
    return {"rules": [r for r in rules if _valid_rule(r)], "retired": retired}


def active_rules(config: Config) -> list[dict]:
    """The active learned rules (what align.py applies)."""
    return load_rules(config)["rules"]


def _valid_rule(r: object) -> bool:
    if not isinstance(r, dict) or not isinstance(r.get("rule"), dict):
        return False
    pat = r["rule"]
    return bool(pat.get("app")) and bool(pat.get("verdict"))


def _key_of(pattern: dict) -> str:
    return f"{str(pattern.get('app') or '').lower()}|{str(pattern.get('title_token') or '').lower()}"


def _dominant_token(label: dict) -> str:
    fp = label.get("fingerprint") if isinstance(label, dict) else None
    toks = fp.get("title_tokens") if isinstance(fp, dict) else None
    if isinstance(toks, list) and toks:
        return str(toks[0]).lower()
    return ""


def _pattern_key(label: dict) -> tuple[str, str, str] | None:
    """(app_lower, dominant_token, app_display) for a label, or None when the
    app is empty (nothing to key on)."""
    fp = label.get("fingerprint") if isinstance(label, dict) else None
    app_display = str((fp or {}).get("app") or "").strip()
    if not app_display:
        return None
    return app_display.lower(), _dominant_token(label), app_display


def _retire(rule: dict, reason: str) -> dict:
    out = dict(rule)
    out["reason"] = reason
    out["retired_at"] = iso_now()
    return out


def mine(config: Config, goals: list[Goal]) -> dict:
    """Mine user labels into rules; persist learned_rules.json.

    Returns {"promoted": [...], "retired": [...], "rules": [...active...]}.
    Only ``source == "user"`` labels drive mining (implicit labels are weak).
    A pattern promotes when its labels are >= MIN_SUPPORT, unanimous on one
    verdict, and — for goal verdicts — that goal is still active. Any active
    rule contradicted by the labels retires ("contradicted"); any whose goal is
    no longer active retires ("archived-goal")."""
    # Collapse to the LATEST user label per session (a later correction supersedes
    # an earlier one — a changed mind is not a contradiction). One session = one
    # vote toward its fingerprint pattern.
    latest_by_session: dict[str, dict] = {}
    for l in load_labels(config):
        if l.get("source") == "user":
            latest_by_session[str(l.get("session_id"))] = l
    labels = list(latest_by_session.values())
    active_goal_ids = {g.id for g in goals}  # load_goals returns active goals only

    # Group user labels by mined pattern key.
    groups: dict[str, dict] = {}
    for l in labels:
        key = _pattern_key(l)
        if key is None:
            continue
        app_lower, token, app_display = key
        ks = f"{app_lower}|{token}"
        g = groups.setdefault(ks, {"app": app_display, "token": token, "items": []})
        g["items"].append(l)

    existing = load_rules(config)
    active_by_key = {_key_of(r["rule"]): r for r in existing["rules"]}
    retired_log = list(existing.get("retired", []))

    new_active: dict[str, dict] = {}
    promoted: list[dict] = []
    newly_retired: list[dict] = []

    def _goal_ok(verdict: str) -> bool:
        return verdict in _SPECIAL or verdict in active_goal_ids

    for ks, g in groups.items():
        items = g["items"]
        verdicts = {str(i.get("verdict")) for i in items}
        prior = active_by_key.get(ks)

        if not str(g["token"]).strip():
            # App-only pattern (no discriminating title token): never a rule —
            # it would match every session of the app. Retire any that linger.
            if prior is not None:
                newly_retired.append(_retire(prior, "app-only-too-broad"))
            continue

        if len(verdicts) > 1:
            # Contradicted pattern: never a rule; retire any active one.
            if prior is not None:
                newly_retired.append(_retire(prior, "contradicted"))
            continue

        verdict = next(iter(verdicts))
        if not _goal_ok(verdict):
            if prior is not None:
                newly_retired.append(_retire(prior, "archived-goal"))
            continue

        if prior is not None:
            new_active[ks] = prior  # already promoted, still consistent — keep
        elif len(items) >= MIN_SUPPORT:
            rule = {
                "rule": {"app": g["app"], "title_token": g["token"], "verdict": verdict},
                "created_from": [
                    {"session_id": i.get("session_id"), "ts": i.get("ts")} for i in items
                ],
                "created_at": iso_now(),
            }
            new_active[ks] = rule
            promoted.append(rule)
        # else: single verdict but < MIN_SUPPORT and not yet a rule -> wait.

    # Active rules with no labels this run: keep, unless their goal went inactive.
    for ks, prior in active_by_key.items():
        if ks in new_active or any(_key_of(r["rule"]) == ks for r in newly_retired):
            continue
        if not str(prior["rule"].get("title_token") or "").strip():
            # A pre-existing app-only rule with no labels this run — retire it
            # (the empty-token policy applies to legacy rules too).
            newly_retired.append(_retire(prior, "app-only-too-broad"))
            continue
        if _goal_ok(str(prior["rule"].get("verdict"))):
            new_active[ks] = prior
        else:
            newly_retired.append(_retire(prior, "archived-goal"))

    retired_log.extend(newly_retired)
    out = {"rules": list(new_active.values()), "retired": retired_log}

    path = rules_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {"promoted": promoted, "retired": newly_retired, "rules": out["rules"]}
