"""scoregoals.audit — the localhost evidence room.

``scoregoals audit`` serves a small, self-contained web app on 127.0.0.1 that
shows, for one day, EXACTLY why every session resolved to the verdict it did:
the full authority chain (label > rule > keyword > llm > none), which keyword
tokens hit, the local-LLM cache row (even when a higher tier overrode it),
whether an app is a known system-noise surface, the goal + project rollup, and
the intention-attribution math. It also surfaces the day's real data problems —
user labels that point at archived/removed goals — with one-click re-filing, and
lets you re-label any session (the same code path as ``scoregoals label``) and
watch the score move.

Design constraints (HARD):
* stdlib only (``http.server``) — no new dependencies.
* bound to 127.0.0.1; non-loopback clients are rejected even so.
* no auth — this is a single-user LOCAL debugging tool. It never writes anything
  except through the exact ``record_label`` + rescore + mine path the CLI uses.
* frames: screenpipe 0.4.25 keeps frames inside rolling .mp4 chunks (and, for
  newer event-driven captures, a direct snapshot .jpg) and does NOT expose a
  frame-image HTTP route. So ``/api/frames`` reads screenpipe's OWN sqlite
  (``~/.screenpipe/db.sqlite``, opened read-only) to find the ``frames`` rows in a
  session's UTC span, and ``/frame/<id>.jpg`` extracts that exact frame with
  ffmpeg — a chunk frame at ``select=eq(n,offset_index)`` (offset_index is the
  frame's index within its chunk; validated empirically against the row's OCR),
  or the snapshot jpg directly. Extractions cache to ``data/frame_cache`` (~500MB
  LRU cap). When a frame's chunk has rolled out of retention it is skipped; if
  none resolve, ``/api/frames`` falls back — HONESTLY — to the per-session OCR
  text timeline with a note. It never fakes an image. Frames are RAW screen
  pixels, served only on 127.0.0.1 — this is the point of a local audit page.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import date as _date
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config

# --- resolution-chain assembly ----------------------------------------------


def _iso_hm(ts: str | None) -> str:
    return (ts or "")[11:16]


def _span(start: str | None, end: str | None) -> str:
    return f"{_iso_hm(start)}-{_iso_hm(end)}"


def _rule_match_detail(session, rules: list[dict]):
    """Replicate align._apply_rules but ALSO return the matched pattern, so the
    audit can show which learned rule fired (align._apply_rules returns only the
    verdict). Returns (verdict, pattern_dict) or (None, None)."""
    from .labels import _tokens

    app = (getattr(session, "app", None) or "").strip().lower()
    if not app:
        return None, None
    title_tokens = set(_tokens(getattr(session, "title", None), 12))
    for rule in rules:
        pat = rule.get("rule") if isinstance(rule, dict) else None
        if not isinstance(pat, dict):
            continue
        r_app = str(pat.get("app") or "").strip().lower()
        r_tok = str(pat.get("title_token") or "").strip().lower()
        verdict = pat.get("verdict")
        if not r_app or not verdict or not r_tok:
            continue
        if r_app == app and r_tok in title_tokens:
            return str(verdict), pat
    return None, None


def build_day(cfg: Config, date: str) -> dict:
    """The full evidence payload for one day (never raises — degrades to an
    empty-but-valid day). Every session carries its resolution chain."""
    from . import align as A
    from . import classify as classify_mod
    from . import intentions as intentions_mod
    from . import labels as L
    from . import learn as learn_mod
    from .aggregate.redact import redact_text
    from .compare import align as kw
    from .labels import NOT_WORK, OFF_TRACK, match_label_by_fingerprint, session_id_for
    from .store import load_timeline

    special = (OFF_TRACK, NOT_WORK)
    warnings: list[str] = []

    goals = kw.load_goals(cfg)                       # active goals + projects
    goals_all = kw.load_goals(cfg, include_archived=True)
    name_by_id = {g.id: g.name for g in goals_all}
    kind_by_id = {g.id: g.kind for g in goals_all}
    archived_ids = {g.id for g in goals_all if g.archived}
    active_ids = {g.id for g in goals}               # goals AND projects, active only

    tl = load_timeline(cfg, date)
    if tl is None:
        return {
            "date": date, "has_timeline": False,
            "score": {"overall": None, "scored": False, "active_minutes": 0.0},
            "goals": [], "projects": [], "sessions": [], "archived_label_warnings": [],
            "intentions": {"items": []}, "label_counts": {}, "resolution_counts": {},
            "goal_options": _goal_options(goals_all), "warnings": ["no timeline captured"],
        }

    labels_all = L.load_labels(cfg)
    lbi = L.labels_by_session(cfg, labels=labels_all)
    lbf = L.labels_by_fingerprint(cfg, labels=labels_all)
    rules = learn_mod.active_rules(cfg)
    intents = intentions_mod.load(cfg, date)["items"]

    # Self-heal the llm tier (at most one batched call; a complete cache no-ops),
    # then load the full cache for the chain view.
    try:
        llm = classify_mod.verdicts_for(cfg, tl, goals, lbi, rules,
                                        labels_by_fp=lbf, intentions=intents)
    except Exception as exc:  # never let the model block the evidence room
        warnings.append(f"llm classification skipped ({exc})")
        llm = classify_mod.load_verdicts(cfg)

    day = A.score_day(tl, goals, lbi, rules, labels_by_fp=lbf, llm_verdicts=llm)

    # Per-session llm intention links, for the attribution view.
    intent_by_session: dict[str, str] = {}
    for sid, rec in (llm or {}).items():
        iid = rec.get("intention_id") if isinstance(rec, dict) else None
        if iid:
            intent_by_session[str(sid)] = str(iid)

    sessions_out: list[dict] = []
    archived_warnings: list[dict] = []
    resolution_counts: dict[str, int] = {}

    for s in sorted(tl.sessions, key=lambda x: (x.start or "")):
        sid = session_id_for(s, date)
        r = A.resolve_session(s, goals, lbi, rules, date=date,
                              labels_by_fp=lbf, llm_verdicts=llm)
        final_source = r["source"]
        resolution_counts[final_source] = resolution_counts.get(final_source, 0) + 1

        # --- label tier -----------------------------------------------------
        label = lbi.get(sid)
        matched_by = "id" if label is not None else None
        if label is None:
            label = match_label_by_fingerprint(s, lbf)
            if label is not None:
                matched_by = "fingerprint"
        label_info = None
        if label is not None:
            lv = str(label.get("verdict"))
            names_archived = lv not in special and lv not in active_ids
            label_info = {
                "verdict": lv,
                "verdict_name": name_by_id.get(lv, lv),
                "source": label.get("source"),
                "date": label.get("date"),
                "matched_by": matched_by,
                # True when the label points at a goal that is archived or gone —
                # the exact stale-reference problem this page exists to surface.
                "archived_goal": names_archived,
                "archived_known": lv in archived_ids,
            }

        # --- rule tier ------------------------------------------------------
        rule_verdict, rule_pat = _rule_match_detail(s, rules)
        rule_info = None
        if rule_verdict is not None:
            rule_info = {
                "verdict": rule_verdict,
                "verdict_name": name_by_id.get(rule_verdict, rule_verdict),
                "pattern": {"app": rule_pat.get("app"), "title_token": rule_pat.get("title_token")},
            }

        # --- keyword tier ---------------------------------------------------
        kw_detail = kw.keyword_hits_detail(goals, s)
        kw_winner, kw_collision = A._keyword_verdict(s, goals)
        keyword_info = {
            # {id: [matched tokens]} with each id's name + kind for rendering.
            "hits": [
                {"id": gid, "name": name_by_id.get(gid, gid),
                 "kind": kind_by_id.get(gid, "goal"), "tokens": toks}
                for gid, toks in kw_detail.items()
            ],
            "winner": kw_winner,
            "winner_name": name_by_id.get(kw_winner, kw_winner) if kw_winner else None,
            "collision": kw_collision,
        }

        # --- llm tier -------------------------------------------------------
        llm_row = llm.get(sid) if isinstance(llm, dict) else None
        llm_info = None
        if isinstance(llm_row, dict):
            lv = llm_row.get("verdict")
            llm_info = {
                "verdict": lv,
                "verdict_name": name_by_id.get(lv, lv) if lv else None,
                "intention_id": llm_row.get("intention_id"),
                "confidence": llm_row.get("confidence"),
                "model": llm_row.get("model"),
                "ts": llm_row.get("ts"),
                "used": final_source == "llm",
                # cached a real guess but a higher tier won:
                "overridden": lv is not None and final_source != "llm",
            }

        system_noise = (s.app or "").strip() in A.SYSTEM_NOISE_APPS

        excerpt = redact_text((s.text_excerpt or ""))[:300]
        excerpt = " ".join(excerpt.split())

        sess = {
            "id": sid,
            "start": s.start,
            "end": s.end,
            "span": _span(s.start, s.end),
            "app": s.app,
            "title": s.title,
            "category": s.category,
            "minutes": round(float(s.minutes), 1),
            "text_excerpt": excerpt,
            "final": {
                "verdict": r["verdict"],
                "verdict_name": r["goal_name"] or (name_by_id.get(r["verdict"]) if r["verdict"] else None),
                "kind": r.get("kind"),
                "source": final_source,
                "confidence": r["confidence"],
                "needs_review": r["needs_review"],
            },
            "chain": {
                "label": label_info,
                "system_noise": system_noise,
                "rule": rule_info,
                "keyword": keyword_info,
                "llm": llm_info,
            },
            "intention_id": intent_by_session.get(sid),
        }
        sessions_out.append(sess)

        if label_info and label_info["archived_goal"]:
            archived_warnings.append({
                "session_id": sid,
                "app": s.app,
                "title": s.title,
                "minutes": round(float(s.minutes), 1),
                "verdict": label_info["verdict"],
                "verdict_name": label_info["verdict_name"],
                "label_source": label_info["source"],
                "archived_known": label_info["archived_known"],
            })

    # --- day meta -----------------------------------------------------------
    label_counts: dict[str, int] = {}
    for rec in labels_all:
        if str(rec.get("date")) == date:
            src = str(rec.get("source") or "?")
            label_counts[src] = label_counts.get(src, 0) + 1

    try:
        intentions_block = intentions_mod.block(cfg, date, timeline=tl, goals=goals,
                                                llm_verdicts=llm)
    except Exception as exc:
        warnings.append(f"intentions failed ({exc})")
        intentions_block = {"items": []}

    goals_out = [
        {"goal_id": a.goal_id, "goal_name": a.goal_name, "minutes": a.minutes,
         "pct_time": a.pct_time, "target_pct": a.target_pct, "on_track": a.on_track}
        for a in day["alignments"]
    ]

    return {
        "date": date,
        "has_timeline": True,
        "score": {
            "overall": day["overall"], "scored": day["scored"],
            "active_minutes": day["active_minutes"],
            "project_minutes": day.get("project_minutes", 0.0),
        },
        "goals": goals_out,
        "projects": day.get("projects", []),
        "sessions": sessions_out,
        "archived_label_warnings": archived_warnings,
        "intentions": intentions_block,
        "label_counts": label_counts,
        "resolution_counts": resolution_counts,
        "goal_options": _goal_options(goals_all),
        "warnings": warnings,
    }


def _goal_options(goals_all: list) -> list[dict]:
    """The label-picker options: active goals, then active projects (archived
    excluded — you never re-file ONTO an archived goal)."""
    opts = []
    for g in goals_all:
        if g.archived:
            continue
        opts.append({"id": g.id, "name": g.name, "kind": g.kind})
    return opts


def available_dates(cfg: Config) -> list[str]:
    """Dates (newest first) that have a stored timeline — the day picker."""
    from pathlib import Path

    tdir = Path(cfg.timeline_dir)
    if not tdir.is_dir():
        return []
    dates = []
    for p in tdir.glob("*.json"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
        if m:
            dates.append(m.group(1))
    return sorted(set(dates), reverse=True)


# --- frames: extract REAL screenshots from screenpipe's local video store ----
#
# screenpipe 0.4.25 has no frame-image HTTP route. It DOES keep a sqlite index
# (~/.screenpipe/db.sqlite) whose ``frames`` rows point into rolling ``.mp4``
# chunks: frames(id, video_chunk_id, offset_index, timestamp, snapshot_path, …),
# video_chunks(id, file_path, fps, …). offset_index is the frame's index WITHIN
# its chunk video — validated empirically (a chunk's max offset_index +1 equals
# ffprobe's frame count, and extracting select=eq(n,offset_index) yields an image
# whose content matches that row's OCR full_text). Newer event-driven captures
# instead carry a direct ``snapshot_path`` jpg. We read that db READ-ONLY and
# extract single frames with ffmpeg, caching the jpegs under data/frame_cache.

_SP_DB = Path.home() / ".screenpipe" / "db.sqlite"
_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_CACHE_CAP_BYTES = 500 * 1024 * 1024   # ~500MB LRU cap on the extracted-frame cache
_FRAME_MAX = 8                          # thumbnails per session strip
_THUMB_W = 960                          # max thumbnail width (px)


def _sp_connect():
    """Open screenpipe's sqlite READ-ONLY. screenpipe writes to it constantly, so
    use ``mode=ro`` + a short busy timeout and never hold the handle open."""
    import sqlite3

    if not _SP_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{_SP_DB}?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA busy_timeout=2000")
        return conn
    except Exception:
        return None


def _local_to_utc(naive_local) -> str | None:
    """Timeline sessions store NAIVE LOCAL timestamps ('2026-07-12T07:53:44');
    screenpipe stores UTC. Convert (assuming the system timezone) and return a
    lexically-comparable 'YYYY-MM-DDTHH:MM:SS' UTC string."""
    try:
        dt = datetime.fromisoformat(str(naive_local))
    except Exception:
        return None
    # astimezone() presumes local for a naive dt, then we normalise to UTC.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _utc_to_local_hm(utc_ts) -> str:
    """screenpipe UTC ts -> local 'HH:MM', to match the session span display."""
    try:
        dt = datetime.fromisoformat(str(utc_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return _iso_hm(utc_ts)


def _even_pick(items: list, k: int) -> list:
    """Up to k items evenly spaced across the list (endpoints included)."""
    n = len(items)
    if n <= k:
        return list(items)
    return [items[round(i * (n - 1) / (k - 1))] for i in range(k)]


def _prune_cache(cache_dir: Path, cap_bytes: int = _CACHE_CAP_BYTES) -> None:
    """Keep the frame cache under cap_bytes by deleting oldest files first."""
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in cache_dir.glob("*.jpg")]
    except Exception:
        return
    total = sum(sz for _, sz, _ in files)
    if total <= cap_bytes:
        return
    for _mtime, sz, p in sorted(files):     # oldest mtime first
        if total <= cap_bytes:
            break
        try:
            p.unlink()
            total -= sz
        except Exception:
            pass


def build_frames(cfg: Config, date: str, session_id: str) -> dict:
    """Real frame thumbnails for a session's span, read from screenpipe's own
    sqlite. Picks up to 8 evenly-spaced frames whose backing file still exists
    (rolling retention); returns ``frames: [{frame_id, ts}]``. The redacted OCR
    text timeline is ALWAYS included below as the text evidence, and becomes the
    honest fallback when no frames resolve (db missing / retention gap)."""
    from .aggregate.redact import redact_text
    from .labels import session_id_for
    from .store import load_timeline

    tl = load_timeline(cfg, date)
    session = None
    if tl is not None:
        for s in tl.sessions:
            if session_id_for(s, date) == session_id or session_id_for(s, date).startswith(session_id):
                session = s
                break
    if session is None:
        return {"session": session_id, "error": "session not found",
                "frames_available": False, "frames": [], "ocr_timeline": []}

    frames_out: list[dict] = []
    note = ""
    lo = _local_to_utc(session.start)
    hi = _local_to_utc(session.end or session.start)
    conn = _sp_connect()
    if conn is None:
        note = ("screenpipe db not found at ~/.screenpipe/db.sqlite — "
                "showing the OCR text timeline instead.")
    elif not (lo and hi):
        conn.close()
        note = "could not resolve the session's time span — showing the OCR timeline."
    else:
        rows: list = []
        try:
            # '~' > any digit/'.'/'+', so 'HH:MM:SS~' is an inclusive upper bound
            # over screenpipe's fractional+offset timestamps within that second.
            rows = conn.execute(
                "SELECT f.id, f.timestamp, f.snapshot_path, vc.file_path "
                "FROM frames f LEFT JOIN video_chunks vc ON vc.id = f.video_chunk_id "
                "WHERE f.timestamp >= ? AND f.timestamp <= ? "
                "  AND (f.video_chunk_id IS NOT NULL OR f.snapshot_path IS NOT NULL) "
                "ORDER BY f.timestamp",
                (lo, hi + "~"),
            ).fetchall()
        except Exception as exc:
            note = f"screenpipe db query failed ({exc}) — showing the OCR timeline."
        finally:
            conn.close()
        # Keep only frames whose backing file still exists (rolling retention).
        resolvable = []
        for fid, ts, snap, chunk_path in rows:
            path = snap if snap else chunk_path
            if path and os.path.exists(path):
                resolvable.append((fid, ts))
        picked = _even_pick(resolvable, _FRAME_MAX)
        frames_out = [{"frame_id": fid, "ts": _utc_to_local_hm(ts)} for fid, ts in picked]
        if frames_out:
            note = (f"{len(frames_out)} of {len(resolvable)} available frames from "
                    f"screenpipe's local video store ({len(rows)} indexed for this span).")
        elif rows and not resolvable:
            note = (f"all {len(rows)} frames for this span have rolled out of "
                    "screenpipe's retention window — showing the OCR text timeline.")
        elif not rows:
            note = ("no screenpipe frames indexed for this span — "
                    "showing the OCR text timeline.")

    ocr_timeline: list[dict] = []
    # Try live screenpipe OCR for the span; on any failure fall back to the
    # session's own stored (already-redacted) excerpt.
    try:
        from .sources import screenpipe as sp

        recs = sp.fetch(session.start, session.end or session.start, cfg)
        for rec in recs:
            if rec.kind not in ("ocr", "ui"):
                continue
            txt = " ".join(redact_text(rec.text or "").split())[:200]
            if not txt:
                continue
            ocr_timeline.append({
                "time": _iso_hm(rec.start), "app": rec.app, "title": rec.title, "text": txt,
            })
    except Exception:
        pass
    if not ocr_timeline:
        txt = " ".join(redact_text(session.text_excerpt or "").split())[:400]
        ocr_timeline = [{"time": _iso_hm(session.start), "app": session.app,
                         "title": session.title, "text": txt or "(no OCR text captured)"}]

    return {
        "session": session_id,
        "span": _span(session.start, session.end),
        "app": session.app,
        "frames_available": bool(frames_out),
        "frames": frames_out,
        "note": note,
        "ocr_timeline": ocr_timeline[:60],
    }


def extract_frame(cfg: Config, frame_id: str, full: bool = False) -> tuple[int, str, bytes]:
    """Extract ONE frame as JPEG from screenpipe's local store; cache the result.

    Returns (status, content_type, body). A chunk-backed frame is extracted with
    ``ffmpeg -vf select=eq(n,offset_index)`` (thumbnail: also scaled to
    ``_THUMB_W``; ``full=True``: original size). An event-driven ``snapshot_path``
    jpg is scaled for the thumbnail or served as-is when full. Extractions cache
    to ``data/frame_cache/<id>[_full].jpg`` (gitignored, ~500MB LRU). 404 (tiny
    JSON) when the frame is unknown or its chunk rolled out of retention."""
    try:
        fid = int(str(frame_id).strip())
    except Exception:
        return 400, "application/json", json.dumps({"error": "bad frame id"}).encode()

    cache_dir = Path(cfg.data_dir) / "frame_cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    cache_file = cache_dir / f"{fid}{'_full' if full else ''}.jpg"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return 200, "image/jpeg", cache_file.read_bytes()   # CACHE HIT

    conn = _sp_connect()
    if conn is None:
        return 404, "application/json", json.dumps({"error": "screenpipe db unavailable"}).encode()
    try:
        row = conn.execute(
            "SELECT f.offset_index, f.snapshot_path, vc.file_path "
            "FROM frames f LEFT JOIN video_chunks vc ON vc.id = f.video_chunk_id "
            "WHERE f.id = ?", (fid,)).fetchone()
    except Exception as exc:
        return 404, "application/json", json.dumps({"error": f"db query failed: {exc}"}).encode()
    finally:
        conn.close()
    if not row:
        return 404, "application/json", json.dumps({"error": "frame not found"}).encode()

    offset, snapshot_path, chunk_path = row
    scale = f"scale='min({_THUMB_W},iw)':-2"

    if snapshot_path and os.path.exists(snapshot_path):
        if full:
            # Already a jpg — serve it directly (and cache a copy).
            try:
                data = Path(snapshot_path).read_bytes()
                cache_file.write_bytes(data)
                _prune_cache(cache_dir)
                return 200, "image/jpeg", data
            except Exception as exc:
                return 404, "application/json", json.dumps({"error": str(exc)}).encode()
        src, vf = snapshot_path, scale
    elif chunk_path and os.path.exists(chunk_path):
        sel = f"select=eq(n\\,{int(offset or 0)})"
        src, vf = chunk_path, (sel if full else f"{sel},{scale}")
    else:
        return 404, "application/json", json.dumps(
            {"error": "frame's video chunk rolled out of retention"}).encode()

    cmd = [_FFMPEG, "-nostdin", "-v", "error", "-i", src]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", "-q:v", "4", str(cache_file), "-y"]
    try:
        subprocess.run(cmd, timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return 404, "application/json", json.dumps({"error": f"ffmpeg failed: {exc}"}).encode()
    if not (cache_file.exists() and cache_file.stat().st_size > 0):
        return 404, "application/json", json.dumps({"error": "extraction produced no image"}).encode()
    _prune_cache(cache_dir)
    return 200, "image/jpeg", cache_file.read_bytes()


# --- label write (same path as cmd_label) -----------------------------------


def apply_label(cfg: Config, date: str, session_ref: str, verdict: str) -> dict:
    """Record a correction via the EXACT CLI path (record_label + rescore +
    mine), then return the fresh day payload. Returns {"error": ...} on a bad
    request rather than raising."""
    from . import labels as labels_mod
    from . import learn as learn_mod
    from .compare import align as kw
    from .labels import NOT_WORK, OFF_TRACK
    from .store import load_timeline

    tl = load_timeline(cfg, date)
    if tl is None:
        return {"error": f"no timeline for {date}"}

    session = None
    sid = None
    exact = [s for s in tl.sessions if labels_mod.session_id_for(s, date) == session_ref]
    if exact:
        session, sid = exact[0], session_ref
    else:
        pref = [s for s in tl.sessions if labels_mod.session_id_for(s, date).startswith(session_ref)]
        if len(pref) == 1:
            session = pref[0]
            sid = labels_mod.session_id_for(session, date)
    if session is None:
        return {"error": f"no session matching {session_ref!r} on {date}"}

    goals = kw.load_goals(cfg)  # active goals + projects — both are valid verdicts
    goal_ids = {g.id for g in goals}
    v = str(verdict)
    if v == OFF_TRACK or v == "off-track":
        v = OFF_TRACK
    elif v == NOT_WORK or v == "not-work":
        v = NOT_WORK
    elif v not in goal_ids:
        return {"error": f"unknown verdict {verdict!r}; known ids: {', '.join(sorted(goal_ids))}"}

    labels_mod.record_label(cfg, sid, date, labels_mod.fingerprint_for_session(session),
                            v, source="user")
    # Self-improvement, exactly like cmd_label (never fatal).
    try:
        learn_mod.mine(cfg, goals)
    except Exception:
        pass
    out = build_day(cfg, date)
    out["labeled"] = {"session_id": sid, "verdict": v}
    return out


# --- HTTP server ------------------------------------------------------------


def _make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "scoregoals-audit"

        def log_message(self, fmt, *args):  # quieter than the default
            sys.stderr.write("[audit] " + (fmt % args) + "\n")

        def _guard(self) -> bool:
            """Reject anything not from loopback (belt-and-suspenders — we also
            bind to 127.0.0.1)."""
            host = self.client_address[0]
            if host not in ("127.0.0.1", "::1", "localhost"):
                self._json({"error": "forbidden"}, status=403)
                return False
            return True

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str, status=200):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            try:
                if path == "/":
                    self._html(PAGE_HTML)
                elif path == "/api/dates":
                    self._json({"dates": available_dates(cfg)})
                elif path == "/api/day":
                    d = (qs.get("date") or [_date.today().isoformat()])[0]
                    self._json(build_day(cfg, d))
                elif path == "/api/frames":
                    d = (qs.get("date") or [_date.today().isoformat()])[0]
                    sess = (qs.get("session") or [""])[0]
                    self._json(build_frames(cfg, d, sess))
                elif path == "/api/feedback":
                    from . import annotations as ann
                    d = (qs.get("date") or [None])[0]
                    new_only = (qs.get("status") or [""])[0] == "new"
                    self._json(ann.aggregate(cfg, date=d, new_only=new_only))
                elif path.startswith("/frame/"):
                    fid = path[len("/frame/"):]
                    if fid.endswith(".jpg"):
                        fid = fid[:-4]
                    full = (qs.get("full") or ["0"])[0] in ("1", "true", "yes")
                    status, ctype, body = extract_frame(cfg, fid, full=full)
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    if status == 200 and ctype.startswith("image/"):
                        # local perf: frames are immutable once extracted.
                        self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._json({"error": "not found"}, status=404)
            except Exception as exc:  # never crash the server on one bad request
                self._json({"error": str(exc)}, status=500)

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            if parsed.path not in ("/api/label", "/api/comment"):
                self._json({"error": "not found"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw or b"{}")
                date = str(data.get("date") or _date.today().isoformat())
                if parsed.path == "/api/label":
                    sess = str(data.get("session_id") or "")
                    verdict = str(data.get("verdict") or "")
                    if not sess or not verdict:
                        self._json({"error": "session_id and verdict required"}, status=400)
                        return
                    self._json(apply_label(cfg, date, sess, verdict))
                    return
                # /api/comment — file a structured feedback note for Claude.
                from . import annotations as ann
                kind = str(data.get("kind") or "idea")
                comment = str(data.get("comment") or "")
                session_id = data.get("session_id")
                if not comment.strip():
                    self._json({"error": "comment is empty"}, status=400)
                    return
                entry = ann.append_comment(
                    cfg, date, kind, comment,
                    session_id=str(session_id) if session_id else None,
                )
                self._json({"ok": True, "entry": entry})
            except Exception as exc:
                self._json({"error": str(exc)}, status=500)

    return Handler


def serve(cfg: Config, date: str, port: int = 5030, open_browser: bool = True) -> int:
    """Run the audit server (blocking) until Ctrl-C.

    Binds 127.0.0.1 only. On a bind failure (port already in use — another
    instance, or a stale one) we log ONE clear line to stderr, sleep 5s so a
    KeepAlive launchd relaunch can't spin into a tight crash loop, and exit 1.
    The date defaults to today dynamically PER REQUEST inside the handlers, so a
    long-lived (always-on) process never gets stuck showing the day it started
    on — this `date` only seeds the printed URL / the optional browser open."""
    import time

    handler = _make_handler(cfg)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        sys.stderr.write(
            f"[audit] cannot bind 127.0.0.1:{port} ({exc}); another instance is "
            "likely already serving. Exiting (KeepAlive will retry).\n"
        )
        time.sleep(5)  # avoid a tight relaunch loop under launchd KeepAlive
        return 1
    url = f"http://127.0.0.1:{port}/?date={date}"
    print(f"scoregoals audit — http://127.0.0.1:{port}/  (starts on {date})")
    print("  the evidence room: every session's resolution chain, live re-labeling,")
    print("  and structured feedback for Claude. Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0


# --- the page (inline HTML/CSS/JS, self-contained, dark navy/mint) ----------

PAGE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ScoreGoals · audit</title>
<style>
  :root{
    --navy:#0b1220; --navy2:#111c30; --panel:#152238; --line:#22344f;
    --ink:#e8eef7; --muted:#8aa0bf; --mint:#4fe3c1; --mint2:#39c9a8;
    --amber:#f0b054; --red:#f06b6b; --violet:#9d8cff; --chip:#1c2c46;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--navy);color:var(--ink);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  a{color:var(--mint)}
  header{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,var(--navy2),var(--navy));
    border-bottom:1px solid var(--line);padding:14px 20px}
  .row{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  h1{font-size:15px;margin:0;letter-spacing:.3px;font-weight:700}
  h1 .mint{color:var(--mint)}
  select{background:var(--panel);color:var(--ink);border:1px solid var(--line);
    border-radius:8px;padding:6px 10px;font:inherit}
  .score{font-size:30px;font-weight:800;font-variant-numeric:tabular-nums}
  .score.na{color:var(--muted);font-size:18px}
  .meta{color:var(--muted);font-size:12.5px}
  .pill{display:inline-block;background:var(--chip);border:1px solid var(--line);
    border-radius:999px;padding:2px 9px;margin:2px 4px 2px 0;font-size:12px}
  .pill.mint{color:var(--mint);border-color:var(--mint2)}
  main{padding:18px 20px;max-width:1180px;margin:0 auto}
  .banner{background:rgba(240,107,107,.09);border:1px solid rgba(240,107,107,.4);
    border-radius:12px;padding:12px 14px;margin-bottom:16px}
  .banner h3{margin:0 0 6px;color:var(--red);font-size:13px}
  .warnrow{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--line);flex-wrap:wrap}
  .warnrow:first-of-type{border-top:none}
  .rollups{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;flex:1;min-width:220px}
  .card h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;color:var(--muted);text-transform:uppercase}
  .bar{height:7px;background:var(--chip);border-radius:5px;overflow:hidden;margin-top:3px}
  .bar > i{display:block;height:100%;background:var(--mint)}
  .glabel{display:flex;justify-content:space-between;font-size:12.5px;margin-top:8px}
  .glabel .n{color:var(--muted)}
  table{width:100%;border-collapse:collapse}
  th{ text-align:left;color:var(--muted);font-size:11px;letter-spacing:.5px;text-transform:uppercase;
    padding:8px;border-bottom:1px solid var(--line)}
  td{padding:0;border-bottom:1px solid var(--line);vertical-align:top}
  .srow{cursor:pointer}
  .srow:hover{background:var(--navy2)}
  .cell{padding:9px 8px}
  .mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .src{font-size:11px;padding:1px 7px;border-radius:6px;border:1px solid var(--line);background:var(--chip)}
  .src.label{color:var(--mint);border-color:var(--mint2)}
  .src.rule{color:var(--violet);border-color:#5b4fb0}
  .src.keyword{color:#7cc0ff;border-color:#3d6da8}
  .src.llm{color:var(--amber);border-color:#8a6a2f}
  .src.system{color:var(--muted)}
  .src.implicit{color:#7cc0ff}
  .src.none{color:var(--muted)}
  .need{color:var(--amber)}
  .detail{background:var(--navy2);padding:0 8px}
  .tiers{display:grid;grid-template-columns:110px 1fr;gap:0;margin:10px 0}
  .tier{display:contents}
  .tier > .tname{padding:8px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
  .tier > .tbody{padding:8px;border-top:1px solid var(--line);font-size:12.5px}
  .tier.win > .tname{color:var(--mint);font-weight:700}
  .tier.win > .tbody{background:rgba(79,227,193,.06)}
  .tier.over > .tbody{opacity:.72}
  .kchip{display:inline-block;background:var(--chip);border:1px solid var(--line);border-radius:6px;
    padding:1px 7px;margin:2px 4px 2px 0;font-size:11.5px}
  .kchip b{color:var(--mint)}
  .excerpt{color:var(--muted);font-size:12px;padding:6px 8px;border-top:1px solid var(--line);
    font-family:ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
  .controls{display:flex;gap:6px;flex-wrap:wrap;padding:8px;border-top:1px solid var(--line);align-items:center}
  button{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;
    padding:5px 10px;font:inherit;font-size:12px;cursor:pointer}
  button:hover{border-color:var(--mint2);color:var(--mint)}
  button.mini{padding:3px 8px;font-size:11px}
  .flash{color:var(--mint);font-size:12px}
  .frames{padding:8px;border-top:1px solid var(--line)}
  .frameline{font-size:12px;color:var(--muted);padding:3px 0;border-top:1px dotted var(--line)}
  .frameline .t{color:var(--mint);font-family:ui-monospace,Menlo,monospace;margin-right:8px}
  .note{color:var(--amber);font-size:12px;margin:4px 0}
  .arch{color:var(--red)}
  .empty{color:var(--muted);padding:40px;text-align:center}
  /* feedback-for-Claude affordances */
  .fbcounter{cursor:pointer;background:var(--chip);border:1px solid var(--line);
    border-radius:999px;padding:3px 11px;font-size:12.5px;color:var(--violet);white-space:nowrap}
  .fbcounter:hover{border-color:var(--violet);color:var(--ink)}
  .fbcounter.zero{color:var(--muted)}
  .daynotes{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:12px 14px;margin-bottom:16px}
  .daynotes h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;color:var(--muted);text-transform:uppercase}
  .cbox{border-top:1px solid var(--line);padding:8px;background:var(--navy2)}
  textarea.ct{width:100%;background:var(--navy);color:var(--ink);border:1px solid var(--line);
    border-radius:8px;padding:7px 9px;font:inherit;font-size:12.5px;resize:vertical;min-height:46px}
  textarea.ct:focus{outline:none;border-color:var(--violet)}
  .crow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px}
  .chint{color:var(--muted);font-size:11px}
  .csaved{color:var(--mint);font-size:11.5px}
  .existing{margin:4px 0 2px}
  .cnote{border-left:2px solid var(--violet);padding:3px 0 3px 9px;margin:5px 0;font-size:12.5px}
  .cnote .cmeta{color:var(--muted);font-size:11px}
  .cnote.acked{border-left-color:var(--line);opacity:.6}
  button.cment{color:var(--violet)}
  button.cment.has{border-color:var(--violet)}
  /* feedback review drawer */
  .drawer{position:fixed;top:0;right:0;bottom:0;width:min(560px,92vw);z-index:60;
    background:var(--navy2);border-left:1px solid var(--line);box-shadow:-16px 0 50px rgba(0,0,0,.5);
    display:flex;flex-direction:column}
  .drawer header{position:static;background:var(--panel);border-bottom:1px solid var(--line)}
  .drawer .dbody{overflow-y:auto;padding:14px 16px;flex:1}
  .drawer .claudetip{background:rgba(157,140,255,.09);border:1px solid rgba(157,140,255,.4);
    border-radius:10px;padding:9px 11px;margin-bottom:12px;font-size:12.5px;color:var(--ink)}
  .drawer .claudetip code{color:var(--mint);font-family:ui-monospace,Menlo,monospace}
  .dentry{border:1px solid var(--line);border-radius:10px;padding:9px 11px;margin-bottom:10px;background:var(--navy)}
  .dentry .cmeta{color:var(--muted);font-size:11px;margin-bottom:3px}
  .dscrim{position:fixed;inset:0;z-index:55;background:rgba(6,10,18,.55)}
  /* real-frame thumbnail strip + lightbox */
  .fstrip{display:flex;gap:8px;overflow-x:auto;padding:2px 0 8px}
  .fthumb{position:relative;flex:0 0 auto;width:150px;height:97px;border:1px solid var(--line);
    border-radius:8px;overflow:hidden;cursor:pointer;background:var(--chip);
    display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px}
  .fthumb:hover{border-color:var(--mint2)}
  .fthumb img{width:100%;height:100%;object-fit:cover;display:block}
  .fthumb.ferr{color:var(--red);font-size:20px;cursor:default}
  .fskel{width:150px;height:97px;border-radius:8px;flex:0 0 auto;
    background:linear-gradient(100deg,var(--chip) 30%,var(--navy2) 50%,var(--chip) 70%);
    background-size:200% 100%;animation:shimmer 1.2s linear infinite}
  @keyframes shimmer{to{background-position:-200% 0}}
  .fcap{position:absolute;left:0;bottom:0;background:rgba(11,18,32,.78);color:var(--mint);
    font:11px ui-monospace,Menlo,monospace;padding:1px 6px;border-top-right-radius:6px}
  .ftimeline{margin-top:4px}
  .lightbox{position:fixed;inset:0;z-index:50;background:rgba(6,10,18,.93);
    display:flex;align-items:center;justify-content:center}
  .lbimg{max-width:92vw;max-height:82vh;border:1px solid var(--line);border-radius:8px;
    box-shadow:0 20px 60px rgba(0,0,0,.6);background:var(--navy2)}
  .lbcap{position:fixed;bottom:16px;left:0;right:0;text-align:center;color:var(--ink);
    font:12.5px ui-monospace,Menlo,monospace}
  .lbnav,.lbclose{position:fixed;background:var(--panel);color:var(--ink);border:1px solid var(--line);
    border-radius:10px;cursor:pointer;line-height:1;padding:8px 15px;font-size:24px}
  .lbnav:hover,.lbclose:hover{border-color:var(--mint2);color:var(--mint)}
  .lbprev{left:18px;top:50%;transform:translateY(-50%)}
  .lbnext{right:18px;top:50%;transform:translateY(-50%)}
  .lbclose{top:16px;right:18px;font-size:16px}
  footer{color:var(--muted);font-size:11.5px;text-align:center;padding:26px 20px 34px;
    max-width:1180px;margin:0 auto;border-top:1px solid var(--line)}
  footer .warn{color:var(--amber)}
</style>
</head><body>
<header>
  <div class="row">
    <h1><span class="mint">ScoreGoals</span> · audit — <span class="meta">the evidence room</span></h1>
    <select id="datepick" title="days with a captured timeline"></select>
    <div id="scorebox"></div>
    <div class="meta" id="metabox"></div>
    <div style="flex:1"></div>
    <div id="fbcounter" class="fbcounter zero" title="feedback notes for Claude — click to review">✎ 0 notes for Claude</div>
  </div>
</header>
<main id="main"><div class="empty">loading…</div></main>
<footer>
  the evidence room · 127.0.0.1 only · <span class="warn">frames are raw, unredacted screen pixels — local-only, never uploaded</span>
</footer>
<script>
const qs=new URLSearchParams(location.search);
let DATE=qs.get("date")||new Date().toISOString().slice(0,10);
let DAY=null;
let FB={entries:[],new_count:0};  // feedback for the current date + global new-count

function el(t,c,txt){const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e;}
function fmtMin(m){return (Math.round(m*10)/10)+"m";}

async function loadDates(){
  const r=await fetch("/api/dates");const j=await r.json();
  const sel=document.getElementById("datepick");sel.innerHTML="";
  (j.dates||[]).forEach(d=>{const o=el("option",null,d);o.value=d;if(d===DATE)o.selected=true;sel.appendChild(o);});
  if(!(j.dates||[]).includes(DATE)&&j.dates&&j.dates.length){DATE=j.dates[0];sel.value=DATE;}
  sel.onchange=()=>{DATE=sel.value;history.replaceState(null,"","?date="+DATE);load();};
}
async function load(){
  document.getElementById("main").innerHTML='<div class="empty">loading '+DATE+'…</div>';
  const r=await fetch("/api/day?date="+encodeURIComponent(DATE));DAY=await r.json();
  await loadFeedback();
  render();
}
async function loadFeedback(){
  // entries for the current date (both new + acked, for the inline history) plus
  // the GLOBAL new-count for the header counter.
  try{
    const r=await fetch("/api/feedback?date="+encodeURIComponent(DATE));
    FB=await r.json();
  }catch(e){FB={entries:[],new_count:0};}
}
function fbForSession(sid){
  return (FB.entries||[]).filter(e=>e.kind==="session"&&e.session_id&&
    (e.session_id===sid||sid.startsWith(e.session_id)||e.session_id.startsWith(sid)));
}
function fbForDay(){
  return (FB.entries||[]).filter(e=>e.kind==="day"||e.kind==="idea");
}
function renderCounter(){
  const c=document.getElementById("fbcounter");if(!c)return;
  const n=FB.new_count||0;
  c.textContent="✎ "+n+" note"+(n===1?"":"s")+" for Claude";
  c.className="fbcounter"+(n?"":" zero");
  c.onclick=openDrawer;
}
async function postComment(payload){
  const r=await fetch("/api/comment",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)});
  const j=await r.json();
  if(j.error)throw new Error(j.error);
  await loadFeedback();
  renderCounter();
  return j.entry;
}
// A reusable comment editor: existing notes + textarea + save (S / ⌘Enter).
function commentEditor(kind, sessionId, opts){
  opts=opts||{};
  const wrap=el("div","cbox");
  const existing=el("div","existing");
  const notes=kind==="session"?fbForSession(sessionId):fbForDay();
  notes.forEach(n=>existing.appendChild(renderNote(n)));
  if(notes.length)wrap.appendChild(existing);
  const ta=el("textarea","ct");ta.placeholder=opts.placeholder||"a note for Claude — ⌘Enter or Save";
  ta.onclick=(e)=>e.stopPropagation();
  wrap.appendChild(ta);
  const row=el("div","crow");
  let kindSel=null;
  if(opts.allowIdea){
    kindSel=el("select");
    [["day","about this day"],["idea","general idea"]].forEach(x=>{const o=el("option",null,x[1]);o.value=x[0];kindSel.appendChild(o);});
    kindSel.onclick=(e)=>e.stopPropagation();
    row.appendChild(kindSel);
  }
  const save=el("button","mini","💬 Save");
  const saved=el("span","csaved");
  async function doSave(e){
    if(e)e.stopPropagation();
    const text=ta.value.trim();if(!text){ta.focus();return;}
    save.disabled=true;saved.textContent="…";
    try{
      const k=kindSel?kindSel.value:kind;
      await postComment({date:DATE,kind:k,session_id:sessionId||null,comment:text});
      ta.value="";saved.textContent="✓ saved — Claude reads these on 'check my feedback'";
      // refresh the existing-notes list in place
      const fresh=k==="session"?fbForSession(sessionId):fbForDay();
      existing.innerHTML="";fresh.forEach(n=>existing.appendChild(renderNote(n)));
      if(fresh.length&&!existing.parentElement)wrap.insertBefore(existing,ta);
    }catch(err){saved.textContent="✕ "+err.message;}
    save.disabled=false;
    setTimeout(()=>{if(saved.textContent.startsWith("✓"))saved.textContent="";},3500);
  }
  save.onclick=doSave;
  ta.addEventListener("keydown",(e)=>{
    if((e.metaKey||e.ctrlKey)&&e.key==="Enter"){e.preventDefault();doSave(e);}
    e.stopPropagation();
  });
  row.appendChild(save);
  row.appendChild(el("span","chint","⌘Enter"));
  row.appendChild(saved);
  wrap.appendChild(row);
  return wrap;
}
function renderNote(n){
  const d=el("div","cnote"+(n.status==="acked"?" acked":""));
  d.appendChild(document.createTextNode(n.comment||""));
  const meta=el("div","cmeta");
  let m=(n.ts||"").slice(0,16).replace("T"," ")+" · "+(n.status||"new");
  if(n.context&&n.context.verdict)m+=" · was "+n.context.verdict;
  meta.textContent=m;
  d.appendChild(meta);
  return d;
}
function render(){
  const d=DAY;
  // header score
  const sb=document.getElementById("scorebox");sb.innerHTML="";
  if(d.score && d.score.scored){sb.appendChild(el("div","score",String(d.score.overall)));}
  else{sb.appendChild(el("div","score na","insufficient data"));}
  const mb=document.getElementById("metabox");
  const lc=Object.entries(d.label_counts||{}).map(([k,v])=>k+":"+v).join("  ")||"none";
  const rc=Object.entries(d.resolution_counts||{}).map(([k,v])=>k+":"+v).join("  ");
  mb.innerHTML="active "+Math.round((d.score||{}).active_minutes||0)+"m · project "+
    Math.round((d.score||{}).project_minutes||0)+"m · "+(d.sessions||[]).length+" sessions<br>"+
    "<span style='color:var(--muted)'>labels "+lc+" · resolved "+rc+"</span>";

  renderCounter();
  const m=document.getElementById("main");m.innerHTML="";
  if(!d.has_timeline){
    m.appendChild(el("div","empty","no timeline captured for "+DATE));
    // day/idea notes still make sense on an empty day (a change request for Claude)
    const dn0=el("div","daynotes");dn0.appendChild(el("h4","","✎ Notes for Claude"));
    dn0.appendChild(commentEditor("day",null,{allowIdea:true,placeholder:"a thought or change request for Claude — ⌘Enter or Save"}));
    m.appendChild(dn0);
    return;
  }

  // archived-label banner
  if((d.archived_label_warnings||[]).length){
    const b=el("div","banner");
    b.appendChild(el("h3","","⚠ "+d.archived_label_warnings.length+" user label(s) point at an archived / removed goal — re-file them:"));
    d.archived_label_warnings.forEach(w=>{
      const row=el("div","warnrow");
      row.appendChild(el("span","mono",fmtMin(w.minutes)));
      row.appendChild(el("span",null,(w.app||"?")+(w.title?" · "+w.title:"")));
      const tag=el("span","arch","→ "+(w.verdict_name||w.verdict)+(w.archived_known?" (archived)":" (unknown)"));
      row.appendChild(tag);
      const sel=el("select");sel.appendChild(el("option",null,"re-file to…"));
      (d.goal_options||[]).forEach(g=>{const o=el("option",null,g.name+(g.kind==="project"?" (project)":""));o.value=g.id;sel.appendChild(o);});
      sel.onchange=()=>{if(sel.value)doLabel(w.session_id,sel.value);};
      row.appendChild(sel);
      b.appendChild(row);
    });
    m.appendChild(b);
  }

  // day notes — a persistent box for general thoughts / change requests for Claude
  const dn=el("div","daynotes");
  dn.appendChild(el("h4","","✎ Notes for Claude — this day"));
  dn.appendChild(commentEditor("day",null,{allowIdea:true,placeholder:"a general thought or change request for Claude — ⌘Enter or Save"}));
  m.appendChild(dn);

  // rollups
  const roll=el("div","rollups");
  const gc=el("div","card");gc.appendChild(el("h4","","Goals (scored)"));
  (d.goals||[]).forEach(g=>{
    if(g.goal_id==="unaligned"){gc.appendChild(gline("Unaligned",g.minutes,g.pct_time,null,true));return;}
    gc.appendChild(gline(g.goal_name,g.minutes,g.pct_time,g.target_pct,false,g.on_track));
  });
  roll.appendChild(gc);
  const pc=el("div","card");pc.appendChild(el("h4","","Projects (tracked, not judged)"));
  if((d.projects||[]).length){d.projects.forEach(p=>pc.appendChild(gline(p.project_name,p.minutes,p.pct_time,null,false)));}
  else{pc.appendChild(el("div","meta","no projects"));}
  roll.appendChild(pc);
  const ic=el("div","card");ic.appendChild(el("h4","","Intentions"));
  const items=((d.intentions||{}).items)||[];
  if(items.length){items.forEach(it=>{
    const l=el("div","glabel");l.appendChild(el("span",null,(it.done?"✓ ":"")+it.text));
    l.appendChild(el("span","n",fmtMin(it.attributed_minutes||0)));ic.appendChild(l);
  });}else{ic.appendChild(el("div","meta","none set"));}
  roll.appendChild(ic);
  m.appendChild(roll);

  // session table
  const tbl=el("table");
  const thead=el("thead");const htr=el("tr");
  ["","span","min","app / title","resolved","src","conf"].forEach(h=>htr.appendChild(el("th",null,h)));
  thead.appendChild(htr);tbl.appendChild(thead);
  const tb=el("tbody");
  (d.sessions||[]).forEach(s=>{
    const tr=el("tr","srow");
    const c0=el("td");const c0d=el("div","cell");c0d.textContent=s.final.needs_review?"▸ !":"▸";
    if(s.final.needs_review)c0d.className="cell need";c0.appendChild(c0d);tr.appendChild(c0);
    tr.appendChild(tdc(s.span,"mono"));
    tr.appendChild(tdc(fmtMin(s.minutes),"mono"));
    const who=(s.app||"?")+(s.title?" · "+s.title:"");
    tr.appendChild(tdc(who));
    tr.appendChild(tdc(s.final.verdict_name||s.final.verdict||"—"));
    const srcTd=el("td");const sd=el("div","cell");sd.appendChild(el("span","src "+s.final.source,s.final.source));srcTd.appendChild(sd);tr.appendChild(srcTd);
    tr.appendChild(tdc(Number(s.final.confidence).toFixed(2),"mono"));
    const dtr=el("tr");const dtd=el("td");dtd.colSpan=7;dtd.className="detail";dtd.appendChild(detail(s));dtd.style.display="none";dtr.appendChild(dtd);
    tr.onclick=()=>{dtd.style.display=dtd.style.display==="none"?"":"none";};
    tb.appendChild(tr);tb.appendChild(dtr);
  });
  tbl.appendChild(tb);m.appendChild(tbl);
}
function gline(name,min,pct,target,muted,ontrack){
  const w=el("div");const l=el("div","glabel");
  const nm=el("span",null,name);if(muted)nm.style.color="var(--muted)";
  if(ontrack===false)nm.style.color="var(--amber)";
  l.appendChild(nm);
  l.appendChild(el("span","n",fmtMin(min)+" · "+Math.round(pct)+"%"+(target?" / "+Math.round(target)+"%":"")));
  w.appendChild(l);
  const bar=el("div","bar");const i=el("i");i.style.width=Math.min(100,target?(pct/target*100):pct)+"%";
  if(ontrack===false)i.style.background="var(--amber)";bar.appendChild(i);w.appendChild(bar);
  return w;
}
function tdc(txt,cls){const td=el("td");const d=el("div","cell "+(cls||""));d.textContent=txt;td.appendChild(d);return td;}

function detail(s){
  const wrap=el("div");
  const src=s.final.source;
  const tiers=el("div","tiers");
  // label
  addTier(tiers,"label",src==="label"||src==="implicit",false,labelBody(s.chain.label,src));
  // system
  if(s.chain.system_noise)addTier(tiers,"system",src==="system",false,el("span",null,"macOS system surface → not_work (no review)"));
  // rule
  addTier(tiers,"rule",src==="rule",s.chain.rule&&src!=="rule",ruleBody(s.chain.rule));
  // keyword
  addTier(tiers,"keyword",src==="keyword",s.chain.keyword.winner&&src!=="keyword"&&src!=="rule"&&src!=="label"&&src!=="implicit",kwBody(s.chain.keyword));
  // llm
  addTier(tiers,"llm",src==="llm",s.chain.llm&&s.chain.llm.overridden,llmBody(s.chain.llm));
  wrap.appendChild(tiers);
  if(s.text_excerpt){const ex=el("div","excerpt",s.text_excerpt);wrap.appendChild(ex);}
  // controls
  const ctr=el("div","controls");
  const sel=el("select");sel.appendChild(el("option",null,"assign to…"));
  (DAY.goal_options||[]).forEach(g=>{const o=el("option",null,g.name+(g.kind==="project"?" (project)":""));o.value=g.id;sel.appendChild(o);});
  sel.onchange=(e)=>{e.stopPropagation();if(sel.value)doLabel(s.id,sel.value);};
  sel.onclick=(e)=>e.stopPropagation();
  ctr.appendChild(sel);
  ["off_track|Off-track","not_work|Not work"].forEach(x=>{
    const [v,lab]=x.split("|");const b=el("button","mini",lab);
    b.onclick=(e)=>{e.stopPropagation();doLabel(s.id,v);};ctr.appendChild(b);});
  const cf=el("button","mini","✓ confirm");cf.onclick=(e)=>{e.stopPropagation();if(s.final.verdict)doLabel(s.id,s.final.verdict);};
  if(!s.final.verdict)cf.disabled=true;ctr.appendChild(cf);
  const fr=el("button","mini","frames / OCR");fr.onclick=(e)=>{e.stopPropagation();showFrames(s.id,ctr);};ctr.appendChild(fr);
  const nSess=fbForSession(s.id).length;
  const cm=el("button","mini cment"+(nSess?" has":""),"💬 comment"+(nSess?" ("+nSess+")":""));
  cm.onclick=(e)=>{e.stopPropagation();toggleComment(s,ctr);};ctr.appendChild(cm);
  const flash=el("span","flash");flash.id="flash-"+s.id;ctr.appendChild(flash);
  wrap.appendChild(ctr);
  return wrap;
}
function addTier(tiers,name,win,over,body){
  const t=el("div","tier"+(win?" win":"")+(over?" over":""));
  t.appendChild(el("div","tname",name+(win?" ◄ winner":over?" (overridden)":"")));
  const b=el("div","tbody");if(typeof body==="string")b.textContent=body;else b.appendChild(body);
  t.appendChild(b);tiers.appendChild(t);
}
function labelBody(l,src){
  if(!l)return el("span",null,"— no stored label");
  const w=el("div");
  const s=el("span",null,(l.source||"user")+" label ("+(l.date||"?")+", via "+(l.matched_by||"id")+") → ");
  const v=el("span",null,l.verdict_name||l.verdict);if(l.archived_goal)v.className="arch";
  s.appendChild(v);w.appendChild(s);
  if(l.archived_goal)w.appendChild(el("div","note","⚠ this label names "+(l.archived_known?"an ARCHIVED":"an UNKNOWN")+" goal — re-file above"));
  return w;
}
function ruleBody(r){if(!r)return el("span",null,"— no rule matched");
  const w=el("div");w.appendChild(el("span",null,"pattern "+r.pattern.app+" · title~"+r.pattern.title_token+" → "+(r.verdict_name||r.verdict)));return w;}
function kwBody(k){
  const w=el("div");
  if(!k.hits.length){w.appendChild(el("span",null,"— no keyword hit"));return w;}
  k.hits.forEach(h=>{
    const chip=el("span","kchip");chip.innerHTML="<b>"+h.name+"</b>"+(h.kind==="project"?" (proj)":"")+": "+h.tokens.join(", ");
    w.appendChild(chip);
  });
  if(k.winner)w.appendChild(el("div","meta","winner: "+(k.winner_name||k.winner)+(k.collision?" (collision — tie)":"")));
  return w;
}
function llmBody(l){
  if(!l)return el("span",null,"— not in llm cache");
  const w=el("div");
  const line=(l.verdict||"none")+" · conf "+l.confidence+" · "+(l.model||"?");
  w.appendChild(el("span",null,line));
  if(l.intention_id)w.appendChild(el("div","meta","linked intention: "+l.intention_id));
  if(l.overridden)w.appendChild(el("div","note","cached a guess but a higher tier won → not used"));
  else if(l.used)w.appendChild(el("div","meta","this guess was used"));
  return w;
}
function toggleComment(s,ctr){
  let box=ctr.parentElement.querySelector(".cbox");
  if(box){box.remove();return;}
  const ed=commentEditor("session",s.id,{placeholder:"a note for Claude about this session — ⌘Enter or Save"});
  ctr.parentElement.appendChild(ed);
  const ta=ed.querySelector("textarea");if(ta)ta.focus();
}
function openDrawer(){
  closeDrawer();
  const scrim=el("div","dscrim");scrim.id="dscrim";scrim.onclick=closeDrawer;
  document.body.appendChild(scrim);
  const dr=el("div","drawer");dr.id="drawer";
  const hd=el("header");const hr=el("div","row");
  hr.appendChild(el("h1","","✎ Notes for Claude"));
  const sp=el("div");sp.style.flex="1";hr.appendChild(sp);
  const copy=el("button","mini","copy JSON");
  const cls=el("button","mini","✕ close");cls.onclick=closeDrawer;
  hr.appendChild(copy);hr.appendChild(cls);hd.appendChild(hr);dr.appendChild(hd);
  const body=el("div","dbody");
  const tip=el("div","claudetip");
  tip.innerHTML="Claude reads these automatically — just tell it <b>“check my feedback”</b>. "+
    "Under the hood it runs <code>scoregoals feedback --json --new-only</code>, acts, then <code>scoregoals feedback ack</code>.";
  body.appendChild(tip);
  dr.appendChild(body);document.body.appendChild(dr);
  // load ALL new entries across dates for the review list
  fetch("/api/feedback?status=new").then(r=>r.json()).then(j=>{
    const entries=j.entries||[];
    copy.onclick=()=>{navigator.clipboard.writeText(JSON.stringify(j,null,2)).then(
      ()=>{copy.textContent="✓ copied";setTimeout(()=>copy.textContent="copy JSON",1500);});};
    if(!entries.length){body.appendChild(el("div","meta","no new notes — everything's been acked."));return;}
    entries.forEach(e=>{
      const d=el("div","dentry");
      const meta=el("div","cmeta");
      let m=(e.ts||"").slice(0,16).replace("T"," ")+" · "+e.kind+" · "+(e.date||"");
      if(e.context){m+=" · "+(e.context.app||"")+" "+(e.context.span||"");if(e.context.verdict)m+=" ("+e.context.verdict+")";}
      meta.textContent=m;d.appendChild(meta);
      d.appendChild(document.createTextNode(e.comment||""));
      body.appendChild(d);
    });
  }).catch(err=>{body.appendChild(el("div","note","could not load: "+err));});
}
function closeDrawer(){
  const d=document.getElementById("drawer");if(d)d.remove();
  const s=document.getElementById("dscrim");if(s)s.remove();
}
async function showFrames(sid,ctr){
  let box=ctr.parentElement.querySelector(".frames");
  if(box){box.remove();return;}
  box=el("div","frames");
  ctr.parentElement.appendChild(box);
  // skeleton strip while we resolve real frames
  const skel=el("div","fstrip");
  for(let i=0;i<5;i++)skel.appendChild(el("div","fskel"));
  box.appendChild(skel);
  let j;
  try{
    const r=await fetch("/api/frames?date="+DATE+"&session="+encodeURIComponent(sid));j=await r.json();
  }catch(e){box.innerHTML="";box.appendChild(el("div","note","could not load frames: "+e));return;}
  box.innerHTML="";
  const frames=j.frames||[];
  if(frames.length){
    const strip=el("div","fstrip");
    frames.forEach((f,i)=>{
      const cell=el("div","fthumb");
      const img=el("img");img.loading="lazy";img.src="/frame/"+f.frame_id+".jpg";
      img.alt="frame "+f.frame_id+" @"+(f.ts||"");
      img.onerror=()=>{cell.classList.add("ferr");cell.textContent="✕";cell.title="frame unavailable";};
      cell.appendChild(img);cell.appendChild(el("span","fcap",f.ts||""));
      cell.onclick=(e)=>{e.stopPropagation();openLightbox(frames,i);};
      strip.appendChild(cell);
    });
    box.appendChild(strip);
  }
  if(j.note)box.appendChild(el("div","note",j.note));
  // OCR text timeline stays BELOW as the text evidence
  const otl=el("div","ftimeline");
  (j.ocr_timeline||[]).forEach(f=>{
    const line=el("div","frameline");const t=el("span","t",(f.time||"")+" ");
    const app=el("b",null,f.app||"");line.appendChild(t);line.appendChild(app);
    line.appendChild(document.createTextNode(" "+(f.text||"")));otl.appendChild(line);
  });
  box.appendChild(otl);
}
let LB=null;
function openLightbox(frames,idx){
  closeLightbox();
  const ov=el("div","lightbox");ov.id="lightbox";
  const img=el("img","lbimg");
  const cap=el("div","lbcap");
  const prev=el("button","lbnav lbprev","‹");
  const next=el("button","lbnav lbnext","›");
  const close=el("button","lbclose","✕");
  LB={frames:frames,idx:idx,img:img,cap:cap};
  prev.onclick=(e)=>{e.stopPropagation();lbStep(-1);};
  next.onclick=(e)=>{e.stopPropagation();lbStep(1);};
  close.onclick=(e)=>{e.stopPropagation();closeLightbox();};
  ov.onclick=()=>closeLightbox();
  img.onclick=(e)=>e.stopPropagation();
  ov.appendChild(close);ov.appendChild(prev);ov.appendChild(img);ov.appendChild(next);ov.appendChild(cap);
  document.body.appendChild(ov);
  document.addEventListener("keydown",lbKey);
  lbShow();
}
function lbShow(){
  if(!LB)return;const f=LB.frames[LB.idx];
  LB.img.src="/frame/"+f.frame_id+".jpg?full=1";
  LB.cap.textContent="frame "+f.frame_id+" · "+(f.ts||"")+"   ("+(LB.idx+1)+"/"+LB.frames.length+")  — raw, local-only";
}
function lbStep(d){if(!LB)return;LB.idx=(LB.idx+d+LB.frames.length)%LB.frames.length;lbShow();}
function lbKey(e){
  if(!LB)return;
  if(e.key==="Escape")closeLightbox();
  else if(e.key==="ArrowLeft")lbStep(-1);
  else if(e.key==="ArrowRight")lbStep(1);
}
function closeLightbox(){
  const ov=document.getElementById("lightbox");if(ov)ov.remove();
  document.removeEventListener("keydown",lbKey);LB=null;
}
async function doLabel(sid,verdict){
  const flash=document.getElementById("flash-"+sid);
  const before=(DAY.score&&DAY.score.scored)?DAY.score.overall:null;
  if(flash)flash.textContent="…";
  const r=await fetch("/api/label",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({date:DATE,session_id:sid,verdict:verdict})});
  const j=await r.json();
  if(j.error){if(flash)flash.textContent="✕ "+j.error;return;}
  DAY=j;
  const after=(DAY.score&&DAY.score.scored)?DAY.score.overall:null;
  render();
  // brief toast in the header
  const mb=document.getElementById("metabox");
  const t=el("div","note");t.textContent="labeled "+sid.slice(0,8)+" → "+verdict+"  (score "+(before==null?"—":before)+" → "+(after==null?"—":after)+")";
  mb.appendChild(t);setTimeout(()=>t.remove(),4000);
}
loadDates().then(load);
</script>
</body></html>
"""
