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

    # Read the cached llm verdicts for the chain view. The audit server is
    # always-on and must load instantly, so it never calls the model — fresh
    # classification is a background `capture`-time concern.
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
_CACHE_CAP_BYTES = 2 * 1024 * 1024 * 1024   # ~2GB LRU cap on the extracted-frame cache
_PAGE_LIMIT = 48                        # frames per grid batch (default page size)
_THUMB_W = 960                          # max thumbnail width (px)

# --- extraction throughput controls -----------------------------------------
#
# The frame grid can request dozens of uncached frames as it scrolls. Two guards
# keep that from forking 100 ffmpeg processes:
#   * a semaphore caps CONCURRENT ffmpeg runs (a fast scroll can't stampede).
#   * a single-flight map ensures two requests for the SAME uncached frame share
#     one extraction instead of racing two ffmpeg runs at the same cache file.
_FFMPEG_LIMIT = 3
_EXTRACT_SEM = threading.Semaphore(_FFMPEG_LIMIT)
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Lock] = {}
# concurrency instrumentation (peak is observable for the throughput proof)
_FF_LOCK = threading.Lock()
_ff_active = 0
_ff_peak = 0

# --- perceptual "moment" collapsing -----------------------------------------
#
# screenpipe captures a frame every ~30s even when the screen is static, so a
# day is mostly near-identical repeats (the overnight permission dialog alone is
# ~800 frames). The old exact-dedup (same chunk+offset) can't collapse them
# because the menu-bar clock changes every minute — the frames aren't byte- or
# OCR-identical. So we group frames into MOMENTS with a perceptual difference
# hash (dHash), no new deps:
#   * emit a tiny 9x8 grayscale for a frame with ONE ffmpeg run (72 raw bytes),
#   * compute a 64-bit dHash in pure Python (each pixel vs its right neighbour),
#   * start a new moment when the app changes OR the dHash Hamming distance from
#     the current moment's representative exceeds a threshold.
# The 9x8 downscale washes out the menu-bar clock (consecutive idle frames hash
# to distance 0) while genuine screen changes (scroll, new panel, app switch)
# land 20+ bits apart — so idle collapses to ~1 moment yet an active work
# session stays MANY distinct moments. Hashes cache to data/frame_hashes.json
# (gitignored, additive, corruption-tolerant), keyed by screenpipe frame_id;
# frames are immutable so a hash is computed at most once, ever.
#
# THROUGHPUT: a full day would be thousands of ffmpeg runs if hashed naively.
# Two things keep it cheap: (1) screenpipe already stores a per-frame
# ``content_hash`` — consecutive frames with an IDENTICAL content_hash (and app)
# are certainly the same image, so they extend the moment with ZERO ffmpeg (the
# 800-frame idle block costs ONE hash); (2) dHash runs only on frames whose
# content actually changed, is capped by the shared ffmpeg semaphore(3), and is
# BUDGETED per request (at most _MOMENT_HASH_BUDGET new hashes) so mode=changes
# returns as-far-as-hashed with ``partial:true`` + ``next_offset`` and the deck
# pages the rest. The cache makes the second load instant.
_MOMENT_DHASH_THRESHOLD = 8     # Hamming-distance split point (tuned empirically)
_MOMENT_HASH_BUDGET = 120       # max NEW dHash ffmpeg runs per mode=changes request
_HASH_LOCK = threading.Lock()
_HASHES: dict[int, int] | None = None   # frame_id -> 64-bit dHash (lazy-loaded)


def _hash_store_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "frame_hashes.json"


def _load_hashes(cfg: Config) -> dict[int, int]:
    """The frame_id -> dHash cache, loaded once into memory. Corruption-tolerant:
    a missing/half-written file degrades to an empty cache (hashes just get
    recomputed) rather than raising."""
    global _HASHES
    with _HASH_LOCK:
        if _HASHES is None:
            _HASHES = {}
            p = _hash_store_path(cfg)
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    for k, v in raw.items():
                        try:
                            _HASHES[int(k)] = int(v)
                        except (TypeError, ValueError):
                            continue
                except Exception:
                    pass
        return _HASHES


def _save_hashes(cfg: Config) -> None:
    """Persist the in-memory hash cache atomically (temp file + os.replace)."""
    p = _hash_store_path(cfg)
    with _HASH_LOCK:
        data = {str(k): int(v) for k, v in (_HASHES or {}).items()}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def _ham(a: int, b: int) -> int:
    """Hamming distance between two 64-bit dHashes (popcount of the XOR)."""
    return bin(a ^ b).count("1")


def _dhash_from_gray(data: bytes) -> int | None:
    """A 64-bit dHash from a 9x8 grayscale bitmap (72 raw bytes, row-major):
    each of the 8 rows contributes 8 bits, one per (pixel > right-neighbour)."""
    if len(data) < 72:
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if data[base + col] > data[base + col + 1] else 0)
    return bits


def _compute_dhash(cfg: Config, fid: int, offset, snapshot_path, chunk_path) -> int | None:
    """Emit a 9x8 grayscale for one frame with a single ffmpeg run and reduce it
    to a 64-bit dHash. Reuses the ffmpeg semaphore + concurrency instrumentation
    so hashing shares the same 3-process cap as thumbnail extraction. Single-
    flight per frame_id so two moment walks can't hash the same frame twice."""
    lock = _inflight_lock(f"dhash-{fid}")
    with lock:
        cached = _load_hashes(cfg).get(fid)
        if cached is not None:
            return cached
        if snapshot_path and os.path.exists(snapshot_path):
            src, vf = snapshot_path, "scale=9:8,format=gray"
        elif chunk_path and os.path.exists(chunk_path):
            src = chunk_path
            vf = f"select=eq(n\\,{int(offset or 0)}),scale=9:8,format=gray"
        else:
            return None
        cmd = [_FFMPEG, "-nostdin", "-v", "error", "-i", src, "-vf", vf,
               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        with _EXTRACT_SEM:
            active = _ff_enter()
            sys.stderr.write(f"[audit] ffmpeg dhash frame={fid} concurrency={active}/{_FFMPEG_LIMIT}\n")
            try:
                out = subprocess.run(cmd, timeout=10, check=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
            except Exception:
                return None
            finally:
                _ff_leave()
        h = _dhash_from_gray(out[:72])
        if h is not None:
            with _HASH_LOCK:
                if _HASHES is not None:
                    _HASHES[fid] = h
        return h


# Frame columns the moment walk needs: identity, backing image, the app + the
# free exact-dup signal (content_hash) so an idle run costs one ffmpeg.
_MOMENT_SELECT = (
    "SELECT f.id, f.timestamp, f.app_name, f.content_hash, f.snapshot_path, "
    "vc.file_path, f.offset_index, f.video_chunk_id "
    "FROM frames f LEFT JOIN video_chunks vc ON vc.id = f.video_chunk_id "
    "WHERE f.timestamp >= ? AND f.timestamp <= ? "
    "  AND (f.video_chunk_id IS NOT NULL OR f.snapshot_path IS NOT NULL) "
    "ORDER BY f.timestamp"
)


def _moment_rows(conn, lo: str, hi: str) -> list[dict]:
    """Every resolvable frame in [lo, hi] (UTC) as rich rows for the moment walk,
    with the SAME consecutive backing-image dedup as ``_query_frames`` (so a
    moment's raw ``count`` matches the raw deck) plus the app + content_hash the
    grouping needs."""
    rows = conn.execute(_MOMENT_SELECT, (lo, hi + "~")).fetchall()
    out: list[dict] = []
    last_key = None
    for fid, ts, app, chash, snap, chunk_path, off, vcid in rows:
        path = snap if snap else chunk_path
        if not (path and os.path.exists(path)):
            continue
        key = snap if snap else (vcid, off)
        if key == last_key:
            continue
        last_key = key
        out.append({
            "frame_id": int(fid), "utc": str(ts), "app": app,
            "content_hash": chash, "snap": snap, "chunk": chunk_path, "off": off,
        })
    return out


def _moment_minutes(a_utc: str, b_utc: str) -> float:
    """Held duration (minutes) between two screenpipe UTC timestamps."""
    try:
        da = datetime.fromisoformat(a_utc)
        db = datetime.fromisoformat(b_utc)
        return round(abs((db - da).total_seconds()) / 60.0, 1)
    except Exception:
        return 0.0


def _inflight_lock(key: str) -> threading.Lock:
    """The per-frame single-flight lock (created on first use)."""
    with _INFLIGHT_LOCK:
        lk = _INFLIGHT.get(key)
        if lk is None:
            lk = threading.Lock()
            _INFLIGHT[key] = lk
        return lk


def _ff_enter() -> int:
    global _ff_active, _ff_peak
    with _FF_LOCK:
        _ff_active += 1
        if _ff_active > _ff_peak:
            _ff_peak = _ff_active
        return _ff_active


def _ff_leave() -> None:
    global _ff_active
    with _FF_LOCK:
        _ff_active -= 1


def ffmpeg_peak() -> int:
    """Peak observed concurrent ffmpeg extractions (for the throughput proof)."""
    return _ff_peak


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


def _utc_to_local_hms(utc_ts) -> str:
    """screenpipe UTC ts -> local 'HH:MM:SS' (second precision for a frame stamp)."""
    try:
        dt = datetime.fromisoformat(str(utc_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return (str(utc_ts) or "")[11:19]


# Columns every frame query needs to resolve + dedupe + extract a row.
_FRAME_SELECT = (
    "SELECT f.id, f.timestamp, f.snapshot_path, vc.file_path, f.offset_index, f.video_chunk_id "
    "FROM frames f LEFT JOIN video_chunks vc ON vc.id = f.video_chunk_id "
    "WHERE f.timestamp >= ? AND f.timestamp <= ? "
    "  AND (f.video_chunk_id IS NOT NULL OR f.snapshot_path IS NOT NULL) "
    "ORDER BY f.timestamp"
)


def _query_frames(conn, lo: str, hi: str) -> list[tuple]:
    """Every RESOLVABLE frame in [lo, hi] (UTC), chronological, as (frame_id, ts).

    Keeps only frames whose backing file still exists (rolling retention), and
    de-dupes CONSECUTIVE frames that share the same backing image (same snapshot,
    or same chunk+offset_index) so a run of identical captures collapses to one.
    '~' > any digit/'.'/'+', so 'HH:MM:SS~' is an inclusive upper bound over
    screenpipe's fractional+offset timestamps within that second."""
    rows = conn.execute(_FRAME_SELECT, (lo, hi + "~")).fetchall()
    out: list[tuple] = []
    last_key = None
    for fid, ts, snap, chunk_path, off, vcid in rows:
        path = snap if snap else chunk_path
        if not (path and os.path.exists(path)):
            continue
        key = snap if snap else (vcid, off)
        if key == last_key:
            continue        # consecutive duplicate of the same backing image
        last_key = key
        out.append((fid, ts))
    return out


def frame_ocr(cfg: Config, frame_id, chars: int = 200) -> dict | None:
    """The frame's local timestamp + a redacted OCR snippet from screenpipe's
    ``frames.full_text`` (first ``chars`` chars — 200 for the stored snapshot the
    agent reads, longer for the deck's on-screen OCR panel). Returns None when the
    db/frame is gone."""
    from .aggregate.redact import redact_text

    conn = _sp_connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT timestamp, full_text FROM frames WHERE id = ?", (int(frame_id),)
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row:
        return None
    ts, full = row
    snippet = " ".join(redact_text(full or "").split())[:chars]
    return {"frame_ts": _utc_to_local_hms(ts), "utc": str(ts), "ocr_snippet": snippet}


def _frame_counts(cfg: Config, date: str) -> dict[int, int]:
    """Per-frame comment counts for the day (drives the 💬 badges without N+1)."""
    try:
        from . import annotations as ann

        return ann.frame_comment_counts(cfg, date)
    except Exception:
        return {}


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


def _ocr_timeline(cfg: Config, session) -> list[dict]:
    """The redacted OCR/ui text timeline for a session span (text evidence that
    sits below the image grid; the honest fallback when no frames resolve)."""
    from .aggregate.redact import redact_text

    ocr_timeline: list[dict] = []
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
    return ocr_timeline[:60]


def build_frames(cfg: Config, date: str, session_id: str,
                 offset: int = 0, limit: int = _PAGE_LIMIT) -> dict:
    """A PAGE of real frames for a session's span, read from screenpipe's own
    sqlite. Returns EVERY resolvable frame in the span (not an 8-sample) as
    ``frames: [{frame_id, ts, comments}]`` sliced to ``[offset:offset+limit]``,
    with ``total`` and ``has_more`` so the grid can walk the whole session. Each
    frame carries its per-frame comment count so 💬 badges render without N+1
    requests. The redacted OCR text timeline rides on the FIRST page only (kept
    off later pages so paging stays cheap); it is also the honest fallback when
    no frame resolves (db missing / retention gap)."""
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
                "frames_available": False, "frames": [], "total": 0,
                "offset": offset, "limit": limit, "has_more": False, "ocr_timeline": []}

    note = ""
    total = 0
    page: list[dict] = []
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
        resolvable: list[tuple] = []
        try:
            resolvable = _query_frames(conn, lo, hi)
        except Exception as exc:
            note = f"screenpipe db query failed ({exc}) — showing the OCR timeline."
        finally:
            conn.close()
        total = len(resolvable)
        counts = _frame_counts(cfg, date)
        page = [
            {"frame_id": fid, "ts": _utc_to_local_hms(ts), "comments": counts.get(int(fid), 0)}
            for fid, ts in resolvable[offset:offset + limit]
        ]
        if total:
            note = (f"{total} frame{'' if total == 1 else 's'} from screenpipe's "
                    "local video store for this span.")
        elif not note:
            note = ("no resolvable screenpipe frames for this span — "
                    "showing the OCR text timeline.")

    out = {
        "session": session_id,
        "span": _span(session.start, session.end),
        "app": session.app,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
        "frames_available": bool(page),
        "frames": page,
        "note": note,
    }
    # OCR text evidence only on the first page (keeps later pages a pure db slice)
    out["ocr_timeline"] = _ocr_timeline(cfg, session) if offset == 0 else []
    return out


def build_day_frames(cfg: Config, date: str,
                     offset: int = 0, limit: int = _PAGE_LIMIT) -> dict:
    """One continuous, chronological page of EVERY frame for the day across all
    sessions — the day-level "🖼 All images" grid. Frames are ordered session by
    session (each session's frames in time order), every frame tagged with its
    owning ``session_id`` + comment count, and a ``sessions`` map carries each
    session's ``{app, verdict, span}`` for the grid's section headers."""
    day = build_day(cfg, date)
    sessions = day.get("sessions", [])
    counts = _frame_counts(cfg, date)

    frames: list[dict] = []
    sessions_meta: dict[str, dict] = {}
    conn = _sp_connect()
    if conn is not None:
        try:
            for s in sessions:
                sid = str(s.get("id") or "")
                lo = _local_to_utc(s.get("start"))
                hi = _local_to_utc(s.get("end") or s.get("start"))
                if not (lo and hi):
                    continue
                try:
                    fr = _query_frames(conn, lo, hi)
                except Exception:
                    fr = []
                if not fr:
                    continue
                final = s.get("final") or {}
                sessions_meta[sid] = {
                    "app": s.get("app"),
                    "verdict": final.get("verdict_name") or final.get("verdict") or "—",
                    "span": s.get("span"),
                }
                for fid, ts in fr:
                    frames.append({
                        "frame_id": fid, "ts": _utc_to_local_hms(ts),
                        "session_id": sid, "comments": counts.get(int(fid), 0),
                    })
        finally:
            conn.close()

    total = len(frames)
    page = frames[offset:offset + limit]
    return {
        "day": True,
        "date": date,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
        "frames_available": bool(page),
        "frames": page,
        "sessions": sessions_meta,
    }


def build_moments(cfg: Config, date: str, session_id: str = "",
                  budget: int = _MOMENT_HASH_BUDGET,
                  threshold: int = _MOMENT_DHASH_THRESHOLD) -> dict:
    """Collapse a day's (or one session's) frames into MOMENTS — runs of visually
    unchanged frames folded into a single representative — so the deck walks
    CHANGES, not idle repeats.

    Walks frames chronologically (session by session for the whole day; each
    session is single-app, so a session boundary is an app boundary). A new
    moment starts when the app changes OR the dHash Hamming distance from the
    current moment's representative exceeds ``threshold``. Consecutive frames
    with an identical screenpipe ``content_hash`` extend the moment for free (no
    ffmpeg) — this is what makes the ~800-frame idle block cost a single hash.

    Progressive by design: at most ``budget`` NEW dHash runs per call. It always
    re-walks from the start (cheap: cached hashes + popcount) so the returned
    moment list is a growing prefix; when frames remain unhashed it returns
    ``partial:true`` + ``next_offset`` (raw frames decided so far) and the caller
    pages again. A moment is
    ``{frame_id (representative=first), start_ts, end_ts, start_utc, end_utc,
       count (raw frames held), app, session_id, held_minutes, verdict, span}``.
    """
    day = build_day(cfg, date)
    sessions = day.get("sessions", [])
    if session_id:
        sessions = [s for s in sessions
                    if str(s.get("id") or "") == session_id
                    or str(s.get("id") or "").startswith(session_id)]
    counts = _frame_counts(cfg, date)

    # 1) cheap sqlite pass: the ordered raw frames (+ app/content_hash) per session
    ordered: list[dict] = []
    sessions_meta: dict[str, dict] = {}
    conn = _sp_connect()
    if conn is not None:
        try:
            for s in sessions:
                sid = str(s.get("id") or "")
                lo = _local_to_utc(s.get("start"))
                hi = _local_to_utc(s.get("end") or s.get("start"))
                if not (lo and hi):
                    continue
                try:
                    rows = _moment_rows(conn, lo, hi)
                except Exception:
                    rows = []
                if not rows:
                    continue
                final = s.get("final") or {}
                sessions_meta[sid] = {
                    "app": s.get("app"),
                    "verdict": final.get("verdict_name") or final.get("verdict") or "—",
                    "span": s.get("span"),
                }
                for r in rows:
                    r["session_id"] = sid
                    ordered.append(r)
        finally:
            conn.close()

    total_raw = len(ordered)

    # 2) the moment walk — dHash grouping with the content_hash fast path
    hashes = _load_hashes(cfg)
    moments: list[dict] = []
    cur: dict | None = None
    computed = 0
    decided = 0            # raw frames we have a grouping decision for
    dirty = False
    budget_hit = False

    def _extend(m: dict, r: dict) -> None:
        m["count"] += 1
        m["end_utc"] = r["utc"]
        m["_last_chash"] = r["content_hash"]

    for r in ordered:
        sid = r["session_id"]
        app = r["app"]
        # FREE fast path: identical content_hash + same app => same image, no ffmpeg
        if (cur is not None and cur["session_id"] == sid and app == cur["app"]
                and r["content_hash"] is not None
                and r["content_hash"] == cur["_last_chash"]):
            _extend(cur, r)
            decided += 1
            continue
        # else we need a perceptual decision -> dHash (cached or computed, budgeted)
        h = hashes.get(r["frame_id"])
        if h is None:
            if computed >= budget:
                budget_hit = True
                break
            h = _compute_dhash(cfg, r["frame_id"], r["off"], r["snap"], r["chunk"])
            computed += 1
            dirty = True
        new_moment = (
            cur is None or cur["session_id"] != sid or app != cur["app"]
            or h is None or cur["_rep_hash"] is None
            or _ham(h, cur["_rep_hash"]) > threshold
        )
        if new_moment:
            meta = sessions_meta.get(sid, {})
            cur = {
                "frame_id": r["frame_id"],
                "session_id": sid,
                "app": app,
                "start_utc": r["utc"],
                "end_utc": r["utc"],
                "count": 1,
                "comments": counts.get(r["frame_id"], 0),
                "verdict": meta.get("verdict") or "—",
                "span": meta.get("span") or "",
                "_rep_hash": h,
                "_last_chash": r["content_hash"],
            }
            moments.append(cur)
        else:
            _extend(cur, r)
            cur["_last_chash"] = r["content_hash"]
        decided += 1

    if dirty:
        _save_hashes(cfg)

    out_moments = []
    for m in moments:
        out_moments.append({
            "frame_id": m["frame_id"],
            "session_id": m["session_id"],
            "app": m["app"],
            "verdict": m["verdict"],
            "span": m["span"],
            "count": m["count"],
            "comments": m["comments"],
            "start_ts": _utc_to_local_hms(m["start_utc"]),
            "end_ts": _utc_to_local_hms(m["end_utc"]),
            "held_minutes": _moment_minutes(m["start_utc"], m["end_utc"]),
        })

    partial = budget_hit and decided < total_raw
    return {
        "mode": "changes",
        "date": date,
        "session": session_id or None,
        "moments": out_moments,
        "total_moments": len(out_moments),
        "total_raw": total_raw,
        "decided": decided,
        "partial": partial,
        "next_offset": decided,
        "sessions": sessions_meta,
        "threshold": threshold,
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

    # SINGLE-FLIGHT: two requests for the same uncached frame share ONE
    # extraction. Whoever loses the lock re-checks the cache and reuses the
    # image the winner just wrote — no duplicate ffmpeg run at the same file.
    lock = _inflight_lock(f"{fid}{'_full' if full else ''}")
    with lock:
        if cache_file.exists() and cache_file.stat().st_size > 0:
            return 200, "image/jpeg", cache_file.read_bytes()  # won by another thread

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
        # SEMAPHORE: cap CONCURRENT ffmpeg runs so a fast scroll can't fork 100
        # processes. The peak is logged + observable via ffmpeg_peak().
        with _EXTRACT_SEM:
            active = _ff_enter()
            sys.stderr.write(f"[audit] ffmpeg extract frame={fid}{'_full' if full else ''} concurrency={active}/{_FFMPEG_LIMIT}\n")
            try:
                subprocess.run(cmd, timeout=10, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                return 404, "application/json", json.dumps({"error": f"ffmpeg failed: {exc}"}).encode()
            finally:
                _ff_leave()
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
                    try:
                        offset = max(0, int((qs.get("offset") or ["0"])[0]))
                    except ValueError:
                        offset = 0
                    try:
                        limit = int((qs.get("limit") or [str(_PAGE_LIMIT)])[0])
                    except ValueError:
                        limit = _PAGE_LIMIT
                    limit = max(1, min(limit, 200))
                    day_mode = (qs.get("day") or [""])[0] in ("1", "true", "yes")
                    mode = (qs.get("mode") or [""])[0]
                    if mode == "changes":
                        # moment-collapsed view (deck default). ``session`` scopes
                        # to one session; otherwise the whole day. ``budget`` caps
                        # new dHash runs so a first index doesn't block on the day.
                        try:
                            budget = int((qs.get("budget") or [str(_MOMENT_HASH_BUDGET)])[0])
                        except ValueError:
                            budget = _MOMENT_HASH_BUDGET
                        budget = max(1, min(budget, 2000))
                        self._json(build_moments(cfg, d, sess, budget=budget))
                    elif day_mode:
                        self._json(build_day_frames(cfg, d, offset, limit))
                    else:
                        self._json(build_frames(cfg, d, sess, offset, limit))
                elif path == "/api/frame":
                    fid = (qs.get("id") or [""])[0]
                    info = frame_ocr(cfg, fid, chars=4000) if fid else None
                    if info is None:
                        self._json({"error": "frame not found", "ocr_snippet": ""}, status=404)
                    else:
                        self._json({"frame_id": int(fid), **info})
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
                # /api/comment — file a structured feedback note for Claude
                # (session | day | idea | frame). A frame note is enriched with
                # frame_ts + ocr_snippet + the owning session's context.
                from . import annotations as ann
                kind = str(data.get("kind") or "idea")
                comment = str(data.get("comment") or "")
                session_id = data.get("session_id")
                frame_id = data.get("frame_id")
                if not comment.strip():
                    self._json({"error": "comment is empty"}, status=400)
                    return
                fid_int = None
                if frame_id is not None:
                    try:
                        fid_int = int(frame_id)
                    except (TypeError, ValueError):
                        self._json({"error": "bad frame_id"}, status=400)
                        return
                entry = ann.append_comment(
                    cfg, date, kind, comment,
                    session_id=str(session_id) if session_id else None,
                    frame_id=fid_int,
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
  /* real-frame GRID (every frame, lazy, paged) + lightbox */
  .fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:8px;padding:6px 0}
  .fsection{grid-column:1/-1;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    padding:10px 4px 3px;margin-top:4px;border-top:1px solid var(--line);
    color:var(--muted);font-size:12px}
  .fsection:first-child{border-top:none;margin-top:0}
  .fsection .app{color:var(--ink);font-weight:600}
  .fsection .verdict{color:var(--mint)}
  .fsection .sp{font-family:ui-monospace,Menlo,monospace}
  .fthumb{position:relative;height:100px;border:1px solid var(--line);
    border-radius:8px;overflow:hidden;cursor:pointer;background:var(--chip);
    display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px}
  .fthumb:hover{border-color:var(--mint2)}
  .fthumb img{width:100%;height:100%;object-fit:cover;display:block}
  .fthumb.ferr{color:var(--red);font-size:20px;cursor:default}
  .fcap{position:absolute;left:0;bottom:0;background:rgba(11,18,32,.78);color:var(--mint);
    font:11px ui-monospace,Menlo,monospace;padding:1px 6px;border-top-right-radius:6px}
  .fbadge{position:absolute;top:4px;right:4px;background:var(--violet);color:#0b1220;
    font-size:11px;font-weight:700;border-radius:999px;padding:1px 6px;line-height:1.35;
    box-shadow:0 1px 4px rgba(0,0,0,.4)}
  .fgridstatus{color:var(--muted);font-size:11.5px;padding:6px 2px}
  .fsentinel{height:1px}
  .ftimeline{margin-top:8px}
  .lightbox{position:fixed;inset:0;z-index:50;background:rgba(6,10,18,.93);
    display:flex;align-items:center;justify-content:center}
  .lbwrap{display:flex;gap:14px;align-items:flex-start;max-width:94vw;max-height:86vh;flex-wrap:wrap}
  .lbimg{max-width:min(64vw,1100px);max-height:82vh;border:1px solid var(--line);border-radius:8px;
    box-shadow:0 20px 60px rgba(0,0,0,.6);background:var(--navy2)}
  .lbcomments{width:320px;max-width:92vw;max-height:82vh;overflow-y:auto;background:var(--navy2);
    border:1px solid var(--line);border-radius:10px;padding:12px 13px}
  .lbctitle{color:var(--violet);font-size:12px;letter-spacing:.4px;text-transform:uppercase;
    margin-bottom:8px;font-weight:700}
  .lbclist{margin-bottom:10px}
  .lbcap{position:fixed;bottom:14px;left:0;right:0;text-align:center;color:var(--ink);
    font:12.5px ui-monospace,Menlo,monospace}
  .lbnav,.lbclose{position:fixed;background:var(--panel);color:var(--ink);border:1px solid var(--line);
    border-radius:10px;cursor:pointer;line-height:1;padding:8px 15px;font-size:24px}
  .lbnav:hover,.lbclose:hover{border-color:var(--mint2);color:var(--mint)}
  .lbprev{left:18px;top:50%;transform:translateY(-50%)}
  .lbnext{right:18px;top:50%;transform:translateY(-50%)}
  .lbclose{top:16px;right:18px;font-size:16px}
  /* mode segmented control */
  .modeseg{display:flex;gap:4px}
  .modeseg button.on{border-color:var(--mint2);color:var(--mint);background:rgba(79,227,193,.08)}
  /* the review DECK — large-image, keyboard-first, comment-and-advance */
  .deck{max-width:1180px;margin:0 auto}
  .deckhead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
  .deckhead .prog{font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,monospace;
    color:var(--mint);font-size:13px;white-space:nowrap}
  .deckhead .ctx{color:var(--muted);font-size:12.5px}
  .deckhead .ctx b{color:var(--ink)}
  .deckhead .ctx .v{color:var(--mint)}
  .deckhead .done{color:var(--mint);font-size:12px;border:1px solid var(--mint2);
    border-radius:999px;padding:1px 9px}
  .deckdivider{background:rgba(157,140,255,.10);border:1px solid rgba(157,140,255,.4);
    border-radius:10px;padding:6px 12px;margin-bottom:10px;color:var(--violet);font-size:12.5px}
  .deckdivider b{color:var(--ink)}
  .deckstage{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
  .deckimgwrap{position:relative;flex:1 1 640px;min-width:280px;background:var(--navy2);
    border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;
    align-items:center;justify-content:center;min-height:280px}
  .deckimg{width:100%;height:auto;max-height:74vh;object-fit:contain;display:block}
  .deckimg.loading{opacity:.35}
  .deckimgwrap .ferr{color:var(--red);padding:40px;font-size:13px}
  .deckside{flex:1 1 300px;min-width:260px;max-width:420px;display:flex;flex-direction:column;gap:10px}
  .deckcbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 12px}
  .deckcbox h4{margin:0 0 7px;font-size:11px;letter-spacing:.5px;color:var(--muted);text-transform:uppercase}
  .deckcbox textarea.ct{min-height:70px}
  .deckcrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:7px}
  .deckhint{color:var(--muted);font-size:11px}
  .deckexisting{margin-bottom:7px}
  .deckocr{background:var(--panel);border:1px solid var(--line);border-radius:10px}
  .deckocr summary{cursor:pointer;padding:9px 12px;font-size:12px;color:var(--muted);
    letter-spacing:.4px;text-transform:uppercase;user-select:none}
  .deckocr summary:hover{color:var(--ink)}
  .deckocr .ocrtext{padding:0 12px 11px;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;
    color:var(--muted);white-space:pre-wrap;word-break:break-word;max-height:34vh;overflow-y:auto}
  .decknav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .decknav .grow{flex:1}
  /* moment "held span · N frames · expand" line + skip-idle chip */
  .deckhead .held{color:var(--muted);font-size:12.5px;display:flex;align-items:center;flex-wrap:wrap;gap:4px}
  .deckhead .held b{color:var(--ink)}
  .idlechip{background:rgba(240,176,84,.10);border:1px solid rgba(240,176,84,.45);
    border-radius:999px;padding:2px 10px;font-size:12px;color:var(--amber);
    display:flex;align-items:center;gap:6px;white-space:nowrap}
  .idlechip button{padding:2px 7px;font-size:11px}
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
    <div class="modeseg" id="modeseg">
      <button id="mode-deck" class="mini" title="review every screenshot, large, keyboard-first">🎞 Deck</button>
      <button id="mode-grid" class="mini" title="scroll the whole day as thumbnails">🖼 Grid</button>
      <button id="mode-audit" class="mini" title="the resolution-chain evidence view">🔍 Audit</button>
    </div>
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
let MODE="deck";                  // "deck" (landing) | "grid" | "audit"
// the review-deck state: the whole day's frames, walked chronologically
let DECK={frames:[],total:null,sessions:{},order:[],idx:0,offset:0,
          loading:false,done:false,filter:null,seq:0};

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
  resetDeck();     // a fresh day → walk its frames from the top
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
function fbForFrame(fid){
  return (FB.entries||[]).filter(e=>e.kind==="frame"&&Number(e.frame_id)===Number(fid));
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
  updateModeButtons();
  const m=document.getElementById("main");m.innerHTML="";
  if(d.has_timeline && MODE==="deck"){renderDeck(m);return;}
  if(d.has_timeline && MODE==="grid"){renderAllImages(m);return;}
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
    tr.onclick=()=>{
      const opening=dtd.style.display==="none";
      dtd.style.display=opening?"":"none";
      // images-first: auto-load the first frame batch the first time a session opens
      if(opening&&!dtd._framesOpened){
        dtd._framesOpened=true;
        const ctr=dtd.querySelector(".controls");
        if(ctr)showFrames(s.id,ctr,{});
      }
    };
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
  const fr=el("button","mini","frames / OCR");fr.onclick=(e)=>{e.stopPropagation();showFrames(s.id,ctr,{toggle:true});};ctr.appendChild(fr);
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
// ---- shared infinite frame grid (batches of 48, IntersectionObserver) -------
function renderBadge(f){
  const b=f._badge;if(!b)return;
  const n=f.comments||0;
  if(n>0){b.style.display="";b.textContent="💬"+(n>1?n:"");b.title=n+" comment"+(n===1?"":"s");}
  else{b.style.display="none";}
}
function frameCell(f,framesArr,opts){
  const cell=el("div","fthumb");
  const img=el("img");img.loading="lazy";img.src="/frame/"+f.frame_id+".jpg";
  img.alt="frame "+f.frame_id+" @"+(f.ts||"");
  img.onerror=()=>{cell.classList.add("ferr");cell.textContent="✕";cell.title="frame unavailable";};
  cell.appendChild(img);cell.appendChild(el("span","fcap",f.ts||""));
  const badge=el("span","fbadge");f._badge=badge;cell.appendChild(badge);renderBadge(f);
  cell.onclick=(e)=>{e.stopPropagation();(opts.onOpen||openLightbox)(framesArr,f._idx,opts);};
  return cell;
}
function sectionHeader(meta){
  const h=el("div","fsection");
  h.appendChild(el("span","app",(meta&&meta.app)||"?"));
  h.appendChild(el("span","verdict",(meta&&meta.verdict)||"—"));
  h.appendChild(el("span","sp",(meta&&meta.span)||""));
  return h;
}
// container gets grid + post-area + status + a sentinel the observer watches.
function infiniteGrid(container,fetchPage,opts){
  opts=opts||{};
  const grid=el("div","fgrid");container.appendChild(grid);
  const post=el("div");container.appendChild(post);
  const status=el("div","fgridstatus","loading…");container.appendChild(status);
  const sentinel=el("div","fsentinel");container.appendChild(sentinel);
  const allFrames=[];let offset=0,total=null,loading=false,done=false,lastSession=null,first=true;
  async function loadMore(){
    if(loading||done)return;loading=true;status.textContent="loading…";
    let j;
    try{j=await fetchPage(offset,48);}catch(e){status.textContent="✕ "+e;loading=false;return;}
    const frames=j.frames||[];total=(j.total!=null?j.total:total);
    frames.forEach(f=>{
      if(opts.sessionId&&!f.session_id)f.session_id=opts.sessionId;
      if(opts.sections&&f.session_id!==lastSession){
        lastSession=f.session_id;grid.appendChild(sectionHeader((j.sessions||{})[f.session_id]));
      }
      f._idx=allFrames.length;allFrames.push(f);
      grid.appendChild(frameCell(f,allFrames,opts));
    });
    offset+=frames.length;
    if(first){first=false;if(opts.onFirstPage)opts.onFirstPage(j,post);}
    if(!j.has_more||frames.length===0){done=true;status.textContent=(total!=null?total+" frame"+(total===1?"":"s"):"")+" · end";}
    else{status.textContent=allFrames.length+" / "+(total!=null?total:"?")+" — scroll for more";}
    loading=false;
  }
  const io=new IntersectionObserver((ents)=>{ents.forEach(e=>{if(e.isIntersecting)loadMore();});},{rootMargin:"500px"});
  io.observe(sentinel);
  loadMore();
  return {frames:allFrames};
}
// ---- session grid (audit-mode session table expander) ----------------------
function showFrames(sid,ctr,opts){
  opts=opts||{};
  let box=ctr.parentElement.querySelector(".frames");
  if(box){if(opts.toggle)box.remove();return;}
  box=el("div","frames");ctr.parentElement.appendChild(box);
  infiniteGrid(box,
    (off,lim)=>fetch("/api/frames?date="+DATE+"&session="+encodeURIComponent(sid)+"&offset="+off+"&limit="+lim).then(r=>r.json()),
    {sessionId:sid,onFirstPage:(j,post)=>{
      if(j.note)post.appendChild(el("div","note",j.note));
      const otl=el("div","ftimeline");
      (j.ocr_timeline||[]).forEach(f=>{
        const line=el("div","frameline");line.appendChild(el("span","t",(f.time||"")+" "));
        line.appendChild(el("b",null,f.app||""));
        line.appendChild(document.createTextNode(" "+(f.text||"")));otl.appendChild(line);
      });
      post.appendChild(otl);
    }});
}
// ---- day GRID ("🖼 Grid") : click a thumb → jump the deck to that frame -----
function renderAllImages(m){
  const box=el("div","allimages");
  box.appendChild(el("div","meta","every captured frame for "+DATE+", chronological — scroll to load more, click any frame to open it in the deck"));
  infiniteGrid(box,
    (off,lim)=>fetch("/api/frames?day=1&date="+DATE+"&offset="+off+"&limit="+lim).then(r=>r.json()),
    {sections:true,onOpen:(arr,idx)=>jumpDeck(arr[idx].frame_id)});
  m.appendChild(box);
}
// ---- lightbox (used by the session grid) : image + per-frame comments -------
let LB=null;
function openLightbox(frames,idx,opts){
  opts=opts||{};closeLightbox();
  const ov=el("div","lightbox");ov.id="lightbox";
  const wrap=el("div","lbwrap");
  const img=el("img","lbimg");img.onclick=(e)=>e.stopPropagation();
  const panel=el("div","lbcomments");panel.onclick=(e)=>e.stopPropagation();
  wrap.appendChild(img);wrap.appendChild(panel);
  const cap=el("div","lbcap");
  const prev=el("button","lbnav lbprev","‹");
  const next=el("button","lbnav lbnext","›");
  const close=el("button","lbclose","✕");
  LB={frames:frames,idx:idx,img:img,cap:cap,panel:panel,opts:opts};
  prev.onclick=(e)=>{e.stopPropagation();lbStep(-1);};
  next.onclick=(e)=>{e.stopPropagation();lbStep(1);};
  close.onclick=(e)=>{e.stopPropagation();closeLightbox();};
  ov.onclick=()=>closeLightbox();
  ov.appendChild(close);ov.appendChild(prev);ov.appendChild(wrap);ov.appendChild(next);ov.appendChild(cap);
  document.body.appendChild(ov);
  document.addEventListener("keydown",lbKey);
  lbShow();
}
function lbShow(){
  if(!LB)return;const f=LB.frames[LB.idx];
  LB.img.src="/frame/"+f.frame_id+".jpg?full=1";
  LB.cap.textContent="frame "+f.frame_id+" · "+(f.ts||"")+"   ("+(LB.idx+1)+"/"+LB.frames.length+")  — raw, local-only";
  renderLbComments(f);
}
function renderLbComments(f){
  const p=LB.panel;p.innerHTML="";
  p.appendChild(el("div","lbctitle","💬 comments · frame "+f.frame_id));
  const list=el("div","lbclist");
  const notes=fbForFrame(f.frame_id);
  if(notes.length)notes.forEach(n=>list.appendChild(renderNote(n)));
  else list.appendChild(el("div","meta","no comments on this frame yet"));
  p.appendChild(list);
  const ta=el("textarea","ct");ta.placeholder="comment on this exact frame — ⌘Enter or Save";
  ta.onclick=(e)=>e.stopPropagation();
  const save=el("button","mini","💬 Save");const saved=el("span","csaved");
  async function doSave(e){
    if(e)e.stopPropagation();
    const text=ta.value.trim();if(!text){ta.focus();return;}
    save.disabled=true;saved.textContent="…";
    try{
      const sid=f.session_id||LB.opts.sessionId||null;
      await postComment({date:DATE,kind:"frame",frame_id:f.frame_id,session_id:sid,comment:text});
      ta.value="";f.comments=(f.comments||0)+1;renderBadge(f);renderLbComments(f);
    }catch(err){saved.textContent="✕ "+err.message;save.disabled=false;}
  }
  save.onclick=doSave;
  ta.addEventListener("keydown",(e)=>{if((e.metaKey||e.ctrlKey)&&e.key==="Enter"){e.preventDefault();doSave(e);}e.stopPropagation();});
  const row=el("div","crow");row.appendChild(save);row.appendChild(el("span","chint","⌘Enter"));row.appendChild(saved);
  p.appendChild(ta);p.appendChild(row);
}
function lbStep(d){if(!LB)return;LB.idx=(LB.idx+d+LB.frames.length)%LB.frames.length;lbShow();}
function lbKey(e){
  if(!LB)return;
  if(e.target&&e.target.tagName==="TEXTAREA"){if(e.key==="Escape")closeLightbox();return;}
  if(e.key==="Escape")closeLightbox();
  else if(e.key==="ArrowLeft")lbStep(-1);
  else if(e.key==="ArrowRight")lbStep(1);
}
function closeLightbox(){
  const ov=document.getElementById("lightbox");if(ov)ov.remove();
  document.removeEventListener("keydown",lbKey);LB=null;
}
// ---- the review DECK : large image, comment-and-advance, keyboard-first -----
function setMode(mode){MODE=mode;if(DAY)render();}
function updateModeButtons(){
  [["mode-deck","deck"],["mode-grid","grid"],["mode-audit","audit"]].forEach(x=>{
    const b=document.getElementById(x[0]);if(b)b.className="mini"+(MODE===x[1]?" on":"");
  });
}
function resetDeck(){
  DECK={mode:"changes",            // "changes" (moments, default) | "all" (raw frames)
        // moment view (mode=changes)
        moments:[],momIdx:0,momTotal:null,rawTotal:null,momPartial:false,
        momLoading:false,momDone:false,skippedIdle:false,idleFrames:0,
        // raw view (mode=all) — the original per-frame walk
        frames:[],total:null,order:[],idx:0,offset:0,loading:false,done:false,
        // shared
        sessions:{},filter:null,seq:0,dom:null};
}
// ---- moment loading (mode=changes) : server re-walks from 0 and returns a
// GROWING prefix of moments (cached dHashes let each call reach further), so we
// just replace our list and repeat until partial=false. ------------------------
async function deckLoadMoments(){
  if(DECK.momLoading||DECK.momDone)return false;
  DECK.momLoading=true;
  const url="/api/frames?mode=changes&date="+DATE+
    (DECK.filter?"&session="+encodeURIComponent(DECK.filter):"");
  let j;
  try{j=await fetch(url).then(r=>r.json());}catch(e){DECK.momLoading=false;return false;}
  DECK.moments=j.moments||[];
  if(j.sessions)Object.assign(DECK.sessions,j.sessions);
  DECK.momTotal=(j.total_moments!=null)?j.total_moments:DECK.moments.length;
  DECK.rawTotal=(j.total_raw!=null)?j.total_raw:DECK.rawTotal;
  DECK.momPartial=!!j.partial;
  if(!j.partial)DECK.momDone=true;
  DECK.momLoading=false;return true;
}
function momSessionOrder(){
  const o=[];DECK.moments.forEach(m=>{if(o.indexOf(m.session_id)<0)o.push(m.session_id);});return o;
}
function isIdleMoment(m){
  const v=(m.verdict||"").toLowerCase();
  return v==="not_work"||v==="not work"||v==="—"||v==="";
}
// skip-idle default: on first paint, start at the first real-change moment and
// remember how many idle frames we jumped, for the "N idle frames at start" chip.
function applyIdleSkip(){
  if(DECK.skippedIdle||DECK.momIdx>0)return;
  DECK.skippedIdle=true;
  let first=0,frames=0;
  while(first<DECK.moments.length&&isIdleMoment(DECK.moments[first])){
    frames+=DECK.moments[first].count||1;first++;
  }
  if(first>0&&first<DECK.moments.length){DECK.momIdx=first;DECK.idleFrames=frames;}
}
async function momEnsure(idx){
  let guard=0;
  while(idx>=DECK.moments.length&&!DECK.momDone&&guard<400){await deckLoadMoments();guard++;}
  return idx<DECK.moments.length;
}
async function deckLoadPage(){
  if(DECK.loading||DECK.done)return false;
  DECK.loading=true;
  const url=DECK.filter
    ?"/api/frames?date="+DATE+"&session="+encodeURIComponent(DECK.filter)+"&offset="+DECK.offset+"&limit=96"
    :"/api/frames?day=1&date="+DATE+"&offset="+DECK.offset+"&limit=96";
  let j;
  try{j=await fetch(url).then(r=>r.json());}catch(e){DECK.loading=false;return false;}
  const frames=j.frames||[];
  if(j.total!=null)DECK.total=j.total;else if(DECK.total==null)DECK.total=frames.length;
  if(j.sessions)Object.assign(DECK.sessions,j.sessions);
  frames.forEach(f=>{
    if(DECK.filter&&!f.session_id)f.session_id=DECK.filter;
    if(DECK.order.indexOf(f.session_id)<0)DECK.order.push(f.session_id);
    DECK.frames.push(f);
  });
  DECK.offset+=frames.length;
  if(!j.has_more||frames.length===0)DECK.done=true;
  DECK.loading=false;return true;
}
async function deckEnsureLoaded(idx){
  let guard=0;
  while(idx>=DECK.frames.length&&!DECK.done&&guard<300){await deckLoadPage();guard++;}
  return idx<DECK.frames.length;
}
function renderDeck(m){
  // seed session meta from the day payload (covers the per-session filter too)
  (DAY.sessions||[]).forEach(s=>{if(!DECK.sessions[s.id])DECK.sessions[s.id]=
    {app:s.app,verdict:(s.final&&(s.final.verdict_name||s.final.verdict))||"—",span:s.span};});
  const deck=el("div","deck");
  const head=el("div","deckhead");
  const prog=el("div","prog","… / …");head.appendChild(prog);
  const ctx=el("div","ctx");head.appendChild(ctx);
  const doneTag=el("div","done","commented ✓");doneTag.style.display="none";head.appendChild(doneTag);
  const spacer=el("div");spacer.style.flex="1";head.appendChild(spacer);
  // "changes only ⇄ all frames" toggle — flips the deck between moments + raw
  const modeBtn=el("button","mini","🎞 changes only");
  modeBtn.title="collapse idle repeats into moments (changes) ⇄ every raw frame";
  modeBtn.onclick=()=>toggleDeckMode();
  head.appendChild(modeBtn);
  const filt=el("select");filt.appendChild(el("option",null,"all sessions"));
  (DAY.sessions||[]).forEach(s=>{const o=el("option",null,(s.app||"?")+" · "+(s.span||""));o.value=s.id;filt.appendChild(o);});
  filt.value=DECK.filter||"";
  filt.onchange=()=>setDeckFilter(filt.value||null);
  head.appendChild(filt);
  deck.appendChild(head);
  // moment "held HH:MM–HH:MM · N frames · expand" line + the skip-idle chip
  const heldrow=el("div","deckhead");heldrow.style.marginTop="-4px";
  const held=el("div","held");heldrow.appendChild(held);
  const chip=el("div","idlechip");chip.style.display="none";heldrow.appendChild(chip);
  heldrow.style.display="none";deck.appendChild(heldrow);
  const divider=el("div","deckdivider");divider.style.display="none";deck.appendChild(divider);
  const stage=el("div","deckstage");
  const imgwrap=el("div","deckimgwrap");
  const img=el("img","deckimg");
  img.onerror=()=>{imgwrap.innerHTML="";imgwrap.appendChild(el("div","ferr","frame image unavailable (rolled out of retention)"));};
  imgwrap.appendChild(img);stage.appendChild(imgwrap);
  const side=el("div","deckside");
  const cbox=el("div","deckcbox");const cboxh=el("h4","","comment this moment — Enter saves & advances");cbox.appendChild(cboxh);
  const existing=el("div","deckexisting");cbox.appendChild(existing);
  const ta=el("textarea","ct");ta.placeholder="type a note, Enter to save + next · Shift+Enter for newline";
  cbox.appendChild(ta);
  const crow=el("div","deckcrow");const saved=el("span","csaved");
  crow.appendChild(el("span","deckhint","Enter save+next · → next · ← back · Shift+Enter newline"));
  crow.appendChild(saved);cbox.appendChild(crow);side.appendChild(cbox);
  const nav=el("div","decknav");
  const back=el("button","mini","← back");back.onclick=()=>deckAdvance(-1);
  const skip=el("button","mini","next →");skip.onclick=()=>deckAdvance(1);
  nav.appendChild(back);nav.appendChild(skip);side.appendChild(nav);
  const ocr=el("details","deckocr");ocr.appendChild(el("summary",null,"OCR text on this frame"));
  const ocrText=el("div","ocrtext","…");ocr.appendChild(ocrText);side.appendChild(ocr);
  stage.appendChild(side);deck.appendChild(stage);m.appendChild(deck);
  DECK.dom={prog,ctx,doneTag,divider,img,imgwrap,existing,ta,saved,ocrText,filt,
            modeBtn,heldrow,held,chip,cboxh};
  ta.addEventListener("keydown",(e)=>{
    if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();deckSave();}
  });
  if(DECK.mode==="changes"){
    modeBtn.textContent="🎞 changes only";
    deckLoadMoments().then(()=>{applyIdleSkip();momShow();});
  }else{
    modeBtn.textContent="🖼 all frames";
    deckEnsureLoaded(DECK.idx).then(()=>deckShow());
  }
}
// ---- moment deck: representative image + held span + comment-and-advance ------
function momShow(){
  const dom=DECK.dom;if(!dom)return;
  dom.heldrow.style.display="";
  const m=DECK.moments[DECK.momIdx];
  if(!m){
    dom.prog.textContent=DECK.momPartial?"indexing frames… (dHash)":"no moments for this day";
    dom.ctx.textContent="";dom.held.textContent="";dom.chip.style.display="none";
    if(DECK.momPartial&&!DECK.momDone)deckLoadMoments().then(()=>momShow());
    return;
  }
  const myseq=(++DECK.seq);
  if(!dom.imgwrap.contains(dom.img)){dom.imgwrap.innerHTML="";dom.imgwrap.appendChild(dom.img);}
  dom.img.classList.add("loading");
  dom.img.onload=()=>dom.img.classList.remove("loading");
  dom.img.src="/frame/"+m.frame_id+".jpg?full=1";
  const order=momSessionOrder();
  const sidx=order.indexOf(m.session_id);
  const mtot=(DECK.momTotal!=null?DECK.momTotal:DECK.moments.length);
  dom.prog.textContent="moment "+(DECK.momIdx+1)+" / "+mtot+(DECK.momPartial?"+":"")+
    " · session "+(sidx+1)+"/"+(order.length||1);
  dom.ctx.innerHTML="";
  dom.ctx.appendChild(el("b",null,(m.app||"?")+"  "));
  dom.ctx.appendChild(el("span",null,(m.span||"")+"  "));
  dom.ctx.appendChild(el("span","v",m.verdict||"—"));
  // held line + expand affordance
  dom.held.innerHTML="";
  const single=(m.count||1)<=1;
  dom.held.appendChild(el("span",null,single
    ? ("single frame · "+(m.start_ts||""))
    : ("held "+(m.start_ts||"")+"–"+(m.end_ts||"")+" · "+m.count+" frames · "+m.held_minutes+"m")));
  if(!single){
    const exp=el("button","mini");exp.style.marginLeft="8px";exp.textContent="⤢ expand "+m.count+" frames";
    exp.title="step into every raw frame of this moment";
    exp.onclick=()=>expandMoment(m);
    dom.held.appendChild(exp);
  }
  // skip-idle chip (only on the auto-skipped first landing)
  if(DECK.idleFrames>0&&DECK.momIdx>0){
    dom.chip.style.display="";dom.chip.innerHTML="";
    dom.chip.appendChild(document.createTextNode("skipped "+DECK.idleFrames+" idle frames at start "));
    const jb=el("button","mini","↖ jump back");jb.onclick=()=>{DECK.momIdx=0;momShow();};
    dom.chip.appendChild(jb);
  }else{dom.chip.style.display="none";}
  // session divider when the moment starts a new session
  const prev=DECK.momIdx>0?DECK.moments[DECK.momIdx-1]:null;
  if(!prev||prev.session_id!==m.session_id){
    dom.divider.style.display="";dom.divider.innerHTML="";
    dom.divider.appendChild(document.createTextNode("— "+(m.start_ts||"")+"  "));
    dom.divider.appendChild(el("b",null,m.app||"?"));
    dom.divider.appendChild(document.createTextNode(" · "+(m.verdict||"—")+" —"));
  }else{dom.divider.style.display="none";}
  dom.cboxh.textContent=single?"comment this frame — Enter saves & advances"
                               :"comment this moment — Enter saves & advances";
  const notes=fbForFrame(m.frame_id);
  dom.existing.innerHTML="";notes.forEach(n=>dom.existing.appendChild(renderNote(n)));
  dom.doneTag.style.display=((m.comments||0)>0||notes.length)?"":"none";
  dom.saved.textContent="";dom.ta.value="";dom.ta.focus();
  dom.ocrText.textContent="loading…";
  fetch("/api/frame?date="+DATE+"&id="+m.frame_id).then(r=>r.json()).then(j=>{
    if(myseq!==DECK.seq)return;
    dom.ocrText.textContent=(j&&j.ocr_snippet&&j.ocr_snippet.trim())?j.ocr_snippet:"(no OCR text captured for this frame)";
  }).catch(()=>{if(myseq===DECK.seq)dom.ocrText.textContent="(OCR unavailable)";});
  momPreload();
}
function momPreload(){
  for(let k=1;k<=3;k++){const nm=DECK.moments[DECK.momIdx+k];if(nm){const im=new Image();im.src="/frame/"+nm.frame_id+".jpg?full=1";}}
  if(DECK.momIdx+4>=DECK.moments.length&&DECK.momPartial&&!DECK.momDone)deckLoadMoments().then(()=>{if(MODE==="deck"&&DECK.mode==="changes")momShow();});
}
async function momAdvance(n){
  const target=DECK.momIdx+n;
  if(target<0)return;
  const ok=await momEnsure(target);
  if(!ok){if(DECK.dom)DECK.dom.saved.textContent="✓ end — "+DECK.moments.length+" moments reviewed";return;}
  DECK.momIdx=target;momShow();
}
async function momSave(){
  const dom=DECK.dom;if(!dom)return;
  const m=DECK.moments[DECK.momIdx];if(!m)return;
  let text=dom.ta.value.trim();
  if(!text){momAdvance(1);return;}   // empty Enter = skip forward
  // a moment comment is filed on the representative frame; note the span it covers.
  if((m.count||1)>1){
    text+="\n\n(covers moment "+(m.start_ts||"")+"–"+(m.end_ts||"")+", "+m.count+" frames)";
  }
  dom.saved.textContent="…";
  try{
    await postComment({date:DATE,kind:"frame",frame_id:m.frame_id,session_id:m.session_id||null,comment:text});
    m.comments=(m.comments||0)+1;dom.ta.value="";
    momAdvance(1);
  }catch(err){dom.saved.textContent="✕ "+err.message;}
}
// flip the whole deck between moment (changes) and raw (all) views, keeping place
function toggleDeckMode(){
  if(DECK.mode==="changes"){
    const m=DECK.moments[DECK.momIdx];
    DECK.mode="all";
    const fid=m?m.frame_id:null;
    // reset raw walk and jump to the current moment's representative frame
    DECK.frames=[];DECK.order=[];DECK.offset=0;DECK.total=null;DECK.done=false;DECK.loading=false;DECK.idx=0;
    render();
    if(fid!=null)jumpDeck(fid);
  }else{
    // find the moment holding the current raw frame (by nearest representative <= idx)
    DECK.mode="changes";
    render();
  }
}
// "expand N frames": drop into the raw view at this moment's representative frame
function expandMoment(m){
  DECK.mode="all";
  DECK.frames=[];DECK.order=[];DECK.offset=0;DECK.total=null;DECK.done=false;DECK.loading=false;DECK.idx=0;
  render();
  jumpDeck(m.frame_id);
}
function deckShow(){
  const dom=DECK.dom;if(!dom)return;
  if(DECK.mode==="changes")return momShow();
  dom.heldrow.style.display="none";
  const f=DECK.frames[DECK.idx];
  if(!f){dom.prog.textContent="no frames captured for this day";dom.ctx.textContent="";return;}
  const myseq=(++DECK.seq);
  if(!dom.imgwrap.contains(dom.img)){dom.imgwrap.innerHTML="";dom.imgwrap.appendChild(dom.img);}
  dom.img.classList.add("loading");
  dom.img.onload=()=>dom.img.classList.remove("loading");
  dom.img.src="/frame/"+f.frame_id+".jpg?full=1";
  const total=DECK.total!=null?DECK.total:DECK.frames.length;
  const sidx=DECK.order.indexOf(f.session_id);
  const meta=DECK.sessions[f.session_id]||{};
  dom.prog.textContent="frame "+(DECK.idx+1)+" / "+total+" · session "+(sidx+1)+"/"+(DECK.order.length||1);
  dom.ctx.innerHTML="";
  dom.ctx.appendChild(el("span",null,f.ts||""));
  dom.ctx.appendChild(el("b",null,"  "+(meta.app||"?")));
  dom.ctx.appendChild(el("span",null," "+(meta.span||"")+"  "));
  dom.ctx.appendChild(el("span","v",meta.verdict||"—"));
  const prev=DECK.idx>0?DECK.frames[DECK.idx-1]:null;
  if(!prev||prev.session_id!==f.session_id){
    dom.divider.style.display="";dom.divider.innerHTML="";
    dom.divider.appendChild(document.createTextNode("— "+(f.ts||"")+"  "));
    dom.divider.appendChild(el("b",null,meta.app||"?"));
    dom.divider.appendChild(document.createTextNode(" · "+(meta.verdict||"—")+" —"));
  }else{dom.divider.style.display="none";}
  const notes=fbForFrame(f.frame_id);
  dom.existing.innerHTML="";notes.forEach(n=>dom.existing.appendChild(renderNote(n)));
  dom.doneTag.style.display=((f.comments||0)>0||notes.length)?"":"none";
  dom.saved.textContent="";dom.ta.value="";dom.ta.focus();
  dom.ocrText.textContent="loading…";
  fetch("/api/frame?date="+DATE+"&id="+f.frame_id).then(r=>r.json()).then(j=>{
    if(myseq!==DECK.seq)return;
    dom.ocrText.textContent=(j&&j.ocr_snippet&&j.ocr_snippet.trim())?j.ocr_snippet:"(no OCR text captured for this frame)";
  }).catch(()=>{if(myseq===DECK.seq)dom.ocrText.textContent="(OCR unavailable)";});
  deckPreload();
}
function deckPreload(){
  if(DECK.mode==="changes")return momPreload();
  for(let k=1;k<=3;k++){const nf=DECK.frames[DECK.idx+k];if(nf){const im=new Image();im.src="/frame/"+nf.frame_id+".jpg?full=1";}}
  if(DECK.idx+4>=DECK.frames.length&&!DECK.done)deckLoadPage();
}
async function deckAdvance(n){
  if(DECK.mode==="changes")return momAdvance(n);
  const target=DECK.idx+n;
  if(target<0)return;
  const ok=await deckEnsureLoaded(target);
  if(!ok){if(DECK.dom)DECK.dom.saved.textContent="✓ end of the day — "+DECK.frames.length+" frames reviewed";return;}
  DECK.idx=target;deckShow();
}
async function deckSave(){
  if(DECK.mode==="changes")return momSave();
  const dom=DECK.dom;if(!dom)return;
  const f=DECK.frames[DECK.idx];if(!f)return;
  const text=dom.ta.value.trim();
  if(!text){deckAdvance(1);return;}   // empty Enter = skip forward
  dom.saved.textContent="…";
  try{
    await postComment({date:DATE,kind:"frame",frame_id:f.frame_id,session_id:f.session_id||null,comment:text});
    f.comments=(f.comments||0)+1;dom.ta.value="";
    deckAdvance(1);
  }catch(err){dom.saved.textContent="✕ "+err.message;}
}
function setDeckFilter(sid){
  DECK.filter=sid;
  DECK.frames=[];DECK.order=[];DECK.offset=0;DECK.total=null;DECK.done=false;DECK.loading=false;DECK.idx=0;
  DECK.moments=[];DECK.momIdx=0;DECK.momTotal=null;DECK.rawTotal=null;DECK.momPartial=false;
  DECK.momDone=false;DECK.momLoading=false;DECK.skippedIdle=false;DECK.idleFrames=0;
  render();
}
async function jumpDeck(fid){
  MODE="deck";DECK.mode="all";
  if(DECK.filter!==null){DECK.filter=null;DECK.frames=[];DECK.order=[];DECK.offset=0;DECK.total=null;DECK.done=false;DECK.loading=false;}
  let guard=0,pos=DECK.frames.findIndex(f=>Number(f.frame_id)===Number(fid));
  while(pos<0&&!DECK.done&&guard<400){await deckLoadPage();guard++;pos=DECK.frames.findIndex(f=>Number(f.frame_id)===Number(fid));}
  DECK.idx=pos>=0?pos:0;
  render();
}
function deckKey(e){
  if(MODE!=="deck"||!DECK.dom)return;
  const ta=DECK.dom.ta;
  const typing=ta&&document.activeElement===ta&&ta.value.trim().length>0;
  if(e.key==="ArrowRight"&&!typing){e.preventDefault();deckAdvance(1);}
  else if(e.key==="ArrowLeft"&&!typing){e.preventDefault();deckAdvance(-1);}
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
const _md=document.getElementById("mode-deck");if(_md)_md.onclick=()=>setMode("deck");
const _mg=document.getElementById("mode-grid");if(_mg)_mg.onclick=()=>setMode("grid");
const _ma=document.getElementById("mode-audit");if(_ma)_ma.onclick=()=>setMode("audit");
document.addEventListener("keydown",deckKey);
loadDates().then(load);
</script>
</body></html>
"""
