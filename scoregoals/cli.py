"""scoregoals.cli — FROZEN command-line interface.

Subcommands orchestrate the module functions. Many modules start as scaffold
stubs raising NotImplementedError; main() converts that into a clean one-line
message and exit code 2 instead of a traceback.

`doctor` and `mock` are fully implemented HERE and must always work, even on
a bare system python with no third-party packages installed (stdlib only —
requests etc. are imported lazily inside the source modules, never here).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from .config import Config, load_config

GLYPH_OK = "✓"   # ✓
GLYPH_BAD = "✗"  # ✗


def _today() -> str:
    return _date.today().isoformat()


def _cfg(args: argparse.Namespace) -> Config:
    return load_config(getattr(args, "config", None))


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# --- subcommand handlers -----------------------------------------------------


def cmd_capture(args: argparse.Namespace) -> int:
    """capture <date>: build the timeline from all sources and store it."""
    cfg = _cfg(args)
    if cfg.capture_paused and not getattr(args, "force", False):
        # Honor the app's pause toggle: skip without touching existing data
        # (duplicate-safe — any prior timeline for the date is left intact).
        print(f"capture paused (settings capture_paused=true) — skipping {args.date}")
        return 0
    from .aggregate import timeline as timeline_mod
    from .store import save_timeline

    tl = timeline_mod.build(args.date, cfg)
    path = save_timeline(cfg, tl)
    total = round(float((tl.stats or {}).get("total_active_minutes", 0)))
    print(f"timeline written: {path} ({len(tl.sessions)} sessions, {total} active min)")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """analyze <date> --backend: run backend(s), benchmark, print comparison."""
    cfg = _cfg(args)
    from .compare import align
    from .store import load_timeline, save_benchmark, save_report

    tl = load_timeline(cfg, args.date)
    if tl is None:
        from .aggregate import timeline as timeline_mod

        tl = timeline_mod.build(args.date, cfg)
    goals = align.load_goals(cfg)
    alignments = align.align(tl, goals)

    backends: list = []
    if args.backend in ("gemini", "both"):
        from .analyze.gemini import GeminiBackend

        backends.append(GeminiBackend(cfg))
    if args.backend in ("ollama", "both"):
        from .analyze.ollama import OllamaBackend

        backends.append(OllamaBackend(cfg))

    from .analyze import benchmark

    reports = benchmark.run(tl, goals, alignments, backends, "eod", cfg)
    benchmark.append_csv(reports, str(Path(cfg.benchmarks_dir) / "compare.csv"))
    for rpt in reports:
        save_report(cfg, rpt)
        save_benchmark(cfg, rpt)

    print(f"{'backend':<10} {'model':<52} {'latency_s':>9} {'cost_usd':>9} {'score':>5}")
    for rpt in reports:
        print(
            f"{rpt.backend:<10} {rpt.model:<52} {rpt.latency_s:>9.2f}"
            f" {rpt.cost_usd:>9.4f} {rpt.overall_score:>5}"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """report <date> --backend: end-of-day markdown report (+ iCloud mirror)."""
    cfg = _cfg(args)
    from .feedback import eod

    rpt = eod.generate(args.date, cfg, args.backend)
    md = eod.render_markdown(rpt)
    out = Path(cfg.reports_dir) / f"{args.date}-eod.md"
    out.write_text(md, encoding="utf-8")
    print(f"eod report: {out} (score {rpt.overall_score}/100, backend {rpt.backend})")
    if cfg.icloud_mirror:
        mirror = Path(cfg.icloud_mirror).expanduser()
        try:
            mirror.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, mirror / out.name)
            print(f"mirrored to {mirror / out.name}")
        except OSError as exc:
            print(f"warning: iCloud mirror failed: {exc}", file=sys.stderr)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """plan: morning plan for today + notification."""
    cfg = _cfg(args)
    from .feedback import morning, notify

    d = _today()
    text = morning.generate(d, cfg)
    out = Path(cfg.reports_dir) / f"{d}-morning.md"
    out.write_text(text, encoding="utf-8")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "morning plan ready")
    notify.notify("scoregoals — morning plan", first[:200])
    print(f"morning plan: {out}")
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    """nudge: real-time drift check; notify only if drifting."""
    cfg = _cfg(args)
    from .feedback import notify, nudge

    msg = nudge.check(cfg)
    if msg:
        notify.notify("scoregoals — drift", msg)
        print(msg)
    else:
        print("on track — no nudge")
    return 0


def cmd_weekly(args: argparse.Namespace) -> int:
    """weekly [--week]: weekly synthesis markdown."""
    cfg = _cfg(args)
    from .feedback import weekly

    week = args.week
    if not week:
        today = _date.today()
        week = (today - timedelta(days=today.weekday())).isoformat()
    text = weekly.generate(week, cfg)
    out = Path(cfg.reports_dir) / f"{week}-weekly.md"
    out.write_text(text, encoding="utf-8")
    print(f"weekly synthesis: {out}")
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    """mock [--date]: write the deterministic mock timeline (FULLY IMPLEMENTED)."""
    cfg = _cfg(args)
    from .mockdata import mock_timeline
    from .store import save_timeline

    d = args.date or _today()
    tl = mock_timeline(d)
    path = save_timeline(cfg, tl)
    stats = tl.stats or {}
    print(f"mock timeline written: {path}")
    print(
        f"  sessions={len(tl.sessions)} calendar={len(tl.calendar)}"
        f" github={len(tl.github)} meetings={len(tl.meetings)}"
    )
    print(f"  total_active_minutes={stats.get('total_active_minutes')}")
    return 0


# --- status / today / focus / config (menu bar app surface) ------------------


def cmd_status(args: argparse.Namespace) -> int:
    """status [--json] [--date]: one live JSON snapshot for the app (exit 0)."""
    cfg = _cfg(args)
    from . import status as status_mod

    d = getattr(args, "date", None) or _today()
    print(status_mod.build_json(cfg, d))
    return 0


def _today_date(args: argparse.Namespace) -> str:
    return getattr(args, "date", None) or _today()


def cmd_today_show(args: argparse.Namespace) -> int:
    """today [--json]: intentions with time attributed to each from today."""
    cfg = _cfg(args)
    from . import intentions

    d = _today_date(args)
    block = intentions.block(cfg, d)
    if getattr(args, "json", False):
        _print_json(block)
        return 0

    print(f"scoregoals — intentions for {d}")
    if block.get("set_at"):
        print(f"  set {block['set_at']}")
    items = block["items"]
    if not items:
        print('  (none yet — set with: scoregoals today set "a|b|c")')
        return 0
    for i, it in enumerate(items, 1):
        mark = "x" if it["done"] else " "
        goal = f"  → {it['goal_name']}" if it.get("goal_name") else "  (no goal)"
        attr = f"  [{it['attributed_minutes']:.0f}m today]" if it.get("attributed_minutes") else ""
        carried = f"  ↩ {it['carried_from']}" if it.get("carried_from") else ""
        print(f"  {i}. [{mark}] {it['text']}{goal}{attr}{carried}")
        if it.get("apps"):
            print(f"        apps: {', '.join(it['apps'])}")
    return 0


def cmd_today_set(args: argparse.Namespace) -> int:
    """today set "a|b|c": replace with up to 3 auto-linked intentions."""
    cfg = _cfg(args)
    from . import intentions

    d = _today_date(args)
    texts = [t.strip() for t in args.items.split("|")]
    rec = intentions.set_items(cfg, d, texts)
    print(f"set {len(rec['items'])} intention(s) for {d}:")
    for i, it in enumerate(rec["items"], 1):
        goal = f" → {it['goal_id']}" if it.get("goal_id") else " (no goal match)"
        print(f"  {i}. {it['text']}{goal}")
    return 0


def cmd_today_add(args: argparse.Namespace) -> int:
    """today add "text" [--goal ID]: append one intention."""
    cfg = _cfg(args)
    from . import intentions

    d = _today_date(args)
    try:
        rec = intentions.add_item(cfg, d, args.text, goal_id=getattr(args, "goal", None))
    except ValueError as exc:
        print(f"scoregoals: {exc}", file=sys.stderr)
        return 2
    it = rec["items"][-1]
    goal = f" → {it['goal_id']}" if it.get("goal_id") else " (no goal match)"
    print(f"added: {it['text']}{goal}")
    return 0


def cmd_today_toggle(args: argparse.Namespace) -> int:
    """today toggle <id-or-index>: flip an intention's done flag."""
    cfg = _cfg(args)
    from . import intentions

    d = _today_date(args)
    it = intentions.toggle(cfg, d, args.ref)
    if it is None:
        print(f"scoregoals: no intention matching {args.ref!r}", file=sys.stderr)
        return 2
    print(f"{'done' if it['done'] else 'reopened'}: {it['text']}")
    return 0


def cmd_today_clear(args: argparse.Namespace) -> int:
    """today clear [--keep-history]: remove today's intentions only.

    Clearing NEVER touches past days' files — history is the archive. The
    `--keep-history` flag is the default (and only) behavior; it is accepted so
    the guarantee is explicit and scriptable.
    """
    cfg = _cfg(args)
    from . import intentions

    d = _today_date(args)
    intentions.clear(cfg, d)
    print(f"cleared intentions for {d} (past days' history preserved)")
    return 0


def cmd_today_history(args: argparse.Namespace) -> int:
    """today history [--days N] [--json]: past intentions + completion rate."""
    cfg = _cfg(args)
    from . import intentions

    days = getattr(args, "days", None) or intentions.HISTORY_DAYS
    end = _today_date(args)
    hist = intentions.history(cfg, days=days, end_date=end)
    if getattr(args, "json", False):
        _print_json(hist)
        return 0

    print(f"scoregoals — intentions history (last {hist['days']} days ending {hist['end_date']})")
    for day in hist["days_list"]:
        items = day["items"]
        if not items:
            print(f"  {day['date']}  —  (none)")
            continue
        print(f"  {day['date']}  ({day['n_done']}/{day['n_total']} done)")
        for it in items:
            mark = "x" if it["done"] else " "
            carried = f" ↩ from {it['carried_from']}" if it.get("carried_from") else ""
            attr = f"  [{it['attributed_minutes']:.0f}m]" if it.get("attributed_minutes") else ""
            print(f"    [{mark}] {it['text']}{carried}{attr}")
    rate = hist["completion_rate"] * 100
    print(
        f"  completion: {hist['items_done']}/{hist['items_total']} items done "
        f"({rate:.0f}%) over {hist['days']} days"
    )
    return 0


def cmd_focus_show(args: argparse.Namespace) -> int:
    """focus [--json]: show the active focus block, if any."""
    cfg = _cfg(args)
    from . import focus

    block = focus.load(cfg)
    if getattr(args, "json", False):
        _print_json(block)
        return 0
    if not block["active"]:
        print("focus: none active")
        return 0
    until = f" until {block['until']}" if block.get("until") else " (open-ended)"
    print(f"focus: {block['goal_name']} ({block['goal_id']}){until}; started {block['started_at']}")
    return 0


def cmd_focus_start(args: argparse.Namespace) -> int:
    """focus start <goal> [--minutes N]: begin a focus block."""
    cfg = _cfg(args)
    from . import focus

    block = focus.start(cfg, args.goal, minutes=getattr(args, "minutes", None))
    if block.get("until"):
        tail = f" for {args.minutes}m (until {block['until']})"
    else:
        tail = " (open-ended)"
    print(f"focus started: {block['goal_name']} ({block['goal_id']}){tail}")
    return 0


def cmd_focus_stop(args: argparse.Namespace) -> int:
    """focus stop: end the active focus block."""
    cfg = _cfg(args)
    from . import focus

    focus.stop(cfg)
    print("focus stopped")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    """config [--json]: effective app-mutable settings."""
    cfg = _cfg(args)
    from .config import effective_settings

    eff = effective_settings(cfg)
    if getattr(args, "json", False):
        _print_json(eff)
        return 0
    for k, v in eff.items():
        print(f"{k} = {json.dumps(v)}")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    """config get <key>: print one setting's effective value.

    Secret keys (e.g. gemini_api_key) print only "set"/"not set" — never the
    stored value.
    """
    cfg = _cfg(args)
    from .config import SECRET_KEYS, SETTINGS_KEYS, get_setting

    if args.key in SECRET_KEYS:
        print("set" if getattr(cfg, args.key, None) else "not set")
        return 0
    try:
        v = get_setting(cfg, args.key)
    except KeyError:
        print(
            f"scoregoals: unknown config key {args.key!r}; valid: {', '.join(SETTINGS_KEYS)}",
            file=sys.stderr,
        )
        return 2
    print("true" if v is True else "false" if v is False else v)
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    """config set <key> <value>: persist one setting to data/settings.json.

    Secret keys (e.g. gemini_api_key) are stored but never echoed back — the
    confirmation prints only "set"/"not set". Pass an empty value to clear one.
    """
    cfg = _cfg(args)
    from .config import SECRET_KEYS, SETTINGS_KEYS, get_setting, set_setting

    try:
        set_setting(cfg, args.key, args.value)
    except KeyError:
        settable = list(SETTINGS_KEYS) + sorted(SECRET_KEYS)
        print(
            f"scoregoals: unknown config key {args.key!r}; settable: {', '.join(settable)}",
            file=sys.stderr,
        )
        return 2
    reloaded = _cfg(args)
    if args.key in SECRET_KEYS:
        state = "set" if getattr(reloaded, args.key, None) else "not set"
        print(f"{args.key} = {state} (saved to {reloaded.settings_path})")
        return 0
    v = get_setting(reloaded, args.key)
    print(f"{args.key} = {json.dumps(v)} (saved to {reloaded.settings_path})")
    return 0


# --- goals (show / write / json — menu bar Goals editor surface) -------------


def _goals_payload(cfg: Config) -> dict:
    """Build the `goals --json` object: the file path, its verbatim text, and
    the parsed goals (id/name/keywords/target_pct/archived). Archived goals are
    INCLUDED here (with archived:true) so the editor can list + unarchive them."""
    from .compare import align

    try:
        raw = Path(cfg.goals_path).read_text(encoding="utf-8")
    except OSError:
        raw = ""
    goals = align.load_goals(cfg, include_archived=True)
    return {
        "path": cfg.goals_path,
        "raw": raw,
        "goals": [
            {
                "id": g.id,
                "name": g.name,
                "keywords": g.keywords,
                "target_pct": g.target_pct,
                "archived": g.archived,
            }
            for g in goals
        ],
    }


def _print_goals_summary(payload: dict) -> None:
    print(f"goals.md: {payload['path']}")
    goals = payload["goals"]
    if not goals:
        print("  (no goals parsed)")
        return
    for g in goals:
        tgt = f" (target {g['target_pct']:.0f}%)" if g.get("target_pct") is not None else ""
        flag = " [archived]" if g.get("archived") else ""
        kws = ", ".join(g.get("keywords") or [])
        print(f"  - {g['id']}: {g['name']}{tgt}{flag}")
        if kws:
            print(f"      keywords: {kws}")


def _cmd_goals_set_archived(args: argparse.Namespace, archived: bool) -> int:
    cfg = _cfg(args)
    from .compare import align

    ok = align.set_archived(cfg, args.goal_id, archived)
    verb = "archived" if archived else "unarchived"
    if not ok:
        ids = ", ".join(g.id for g in align.load_goals(cfg, include_archived=True)) or "(none)"
        print(
            f"scoregoals: no goal with id {args.goal_id!r}; known ids: {ids}",
            file=sys.stderr,
        )
        return 2
    print(f"{verb} goal: {args.goal_id}")
    return 0


def cmd_goals_archive(args: argparse.Namespace) -> int:
    """goals archive <goal-id>: retire a goal (excluded from alignment)."""
    return _cmd_goals_set_archived(args, True)


def cmd_goals_unarchive(args: argparse.Namespace) -> int:
    """goals unarchive <goal-id>: reactivate an archived goal."""
    return _cmd_goals_set_archived(args, False)


def cmd_goals(args: argparse.Namespace) -> int:
    """goals [--json]: show the parsed goals (or the full JSON surface)."""
    cfg = _cfg(args)
    payload = _goals_payload(cfg)
    if getattr(args, "json", False):
        _print_json(payload)
        return 0
    _print_goals_summary(payload)
    return 0


def cmd_goals_show(args: argparse.Namespace) -> int:
    """goals show [--raw]: print goals.md verbatim (--raw) or a parsed summary."""
    cfg = _cfg(args)
    if getattr(args, "raw", False):
        try:
            sys.stdout.write(Path(cfg.goals_path).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"scoregoals: cannot read {cfg.goals_path}: {exc}", file=sys.stderr)
            return 2
        return 0
    _print_goals_summary(_goals_payload(cfg))
    return 0


def cmd_goals_write(args: argparse.Namespace) -> int:
    """goals write: read new markdown from STDIN, atomically overwrite goals.md
    (temp file + rename), then parse it and print a one-line summary. Never
    rejects the write — if the new content parses to ZERO goals, it is still
    written and a clear warning goes to stderr (the file may be mid-draft)."""
    import os
    import tempfile

    cfg = _cfg(args)
    from .compare import align

    data = sys.stdin.read()
    path = Path(cfg.goals_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic replace: write a temp file in the same directory, then os.replace.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".goals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    goals = align.load_goals(cfg)  # re-reads the file we just wrote
    if not goals:
        print(
            f"warning: wrote {path} but it parsed to ZERO goals —"
            " check the '## Goal: <name>' format",
            file=sys.stderr,
        )
        print("wrote goals.md (0 goals)")
        return 0
    ids = ", ".join(g.id for g in goals)
    print(f"wrote goals.md ({len(goals)} goals: {ids})")
    return 0


# --- doctor (FULLY IMPLEMENTED) ----------------------------------------------


def _which(name: str) -> str | None:
    """shutil.which plus the usual Homebrew locations (launchd-safe)."""
    found = shutil.which(name)
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = Path(d) / name
        if p.exists():
            return str(p)
    return None


def _http_get(url: str, timeout: float = 3.0) -> str:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (localhost probes)
        return resp.read().decode("utf-8", errors="replace")


def _check_screenpipe(cfg: Config) -> tuple[bool, str]:
    try:
        _http_get(f"{cfg.screenpipe_url}/health")
        return True, f"reachable at {cfg.screenpipe_url}"
    except Exception:
        return False, (
            f"not reachable at {cfg.screenpipe_url}"
            " — install the desktop app: https://screenpi.pe"
            " (mock mode works without it)"
        )


def _check_ollama(cfg: Config) -> tuple[bool, str]:
    import json as _json

    names: list[str] = []
    reachable = False
    try:
        data = _json.loads(_http_get(f"{cfg.ollama_url}/api/tags"))
        names = [m.get("name", "") for m in data.get("models", [])]
        reachable = True
    except Exception:
        exe = _which("ollama")
        if exe:
            try:
                proc = subprocess.run(
                    [exe, "list"], capture_output=True, text=True, timeout=10
                )
                if proc.returncode == 0:
                    reachable = True
                    names = [
                        ln.split()[0] for ln in proc.stdout.splitlines()[1:] if ln.split()
                    ]
            except Exception:
                pass
    if not reachable:
        return False, f"not reachable at {cfg.ollama_url} — start with `ollama serve`"
    want = cfg.ollama_model
    base = want.split(":", 1)[0]
    if want in names or any(n.split(":", 1)[0] == base for n in names):
        return True, f"reachable at {cfg.ollama_url}; model present: {want}"
    return False, (
        f"reachable at {cfg.ollama_url} but model MISSING: {want}"
        f" (`ollama pull {want}`)"
    )


def _check_gemini(cfg: Config) -> tuple[bool, str]:
    """Report which gemini path is active, probing in the same order analyze()
    resolves: API key, then agy (Antigravity), then the legacy gemini CLI."""
    agy = _which("agy")
    cli = _which("gemini")
    if cfg.gemini_api_key:
        extra = f"; agy at {agy}" if agy else (f"; legacy CLI at {cli}" if cli else "")
        return True, f"API key ({cfg.gemini_model}){extra}"
    if agy:
        return True, f"agy (Antigravity) at {agy} — model {cfg.gemini_model}, no key needed"
    if cli:
        return True, (
            f"gemini CLI (legacy, deprecated) at {cli}"
            " — install Antigravity for gemini-3.5-flash: `brew install antigravity-cli`"
        )
    return False, (
        "no GEMINI_API_KEY, no agy, no legacy gemini CLI — gemini backend unavailable"
        " (install Antigravity: `brew install antigravity-cli`; ollama still works)"
    )


def _check_tool(name: str, hint: str) -> tuple[bool, str]:
    path = _which(name)
    if path:
        return True, f"found at {path}"
    return False, f"not found — {hint}"


def _check_gh() -> tuple[bool, str]:
    gh = _which("gh")
    if not gh:
        return False, "gh not installed — `brew install gh`"
    try:
        proc = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return False, f"`gh auth status` failed ({exc.__class__.__name__})"
    if proc.returncode == 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            low = line.lower()
            if "logged in" in low or "account" in low:
                return True, line.strip().lstrip("-✓ ").strip()
        return True, "authenticated"
    return False, "gh installed but not authenticated — `gh auth login`"


def _check_dirs(cfg: Config) -> tuple[bool, str]:
    try:
        probe = Path(cfg.data_dir) / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, f"writable: {cfg.data_dir}"
    except Exception as exc:
        return False, f"cannot write {cfg.data_dir} ({exc})"


def cmd_doctor(args: argparse.Namespace) -> int:
    """doctor: probe external tools/services, print a ✓/✗ checklist."""
    cfg = _cfg(args)
    checks: list[tuple[str, bool, str]] = [
        ("screenpipe", *_check_screenpipe(cfg)),
        ("ollama", *_check_ollama(cfg)),
        ("gemini", *_check_gemini(cfg)),
        ("icalBuddy", *_check_tool("icalBuddy", "`brew install ical-buddy` for calendar capture")),
        ("terminal-notifier", *_check_tool("terminal-notifier", "`brew install terminal-notifier` (osascript fallback used)")),
        ("gh", *_check_gh()),
        ("data dirs", *_check_dirs(cfg)),
    ]

    print("scoregoals doctor — environment checklist\n")
    for name, ok, detail in checks:
        glyph = GLYPH_OK if ok else GLYPH_BAD
        print(f"  {glyph} {name:<18} {detail}")
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed.")
    if not checks[0][1]:
        print(
            "tip: scoregoals works right now without screenpipe —"
            " `python -m scoregoals mock` then analyze with the ollama backend."
        )
    return 0


# --- parser / main -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scoregoals",
        description=(
            "Personal, local-first cybernetic activity tracker: capture what happened, "
            "compare it to goals.md, feed back plans/nudges/reports."
        ),
    )
    parser.add_argument("--config", metavar="PATH", help="path to config.toml (default: auto-discover)")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("capture", help="build + store the day timeline from all sources")
    p.add_argument("date", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("analyze", help="run LLM backend(s) over a day and benchmark cost/latency/quality")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--backend", choices=["gemini", "ollama", "both"], default="both")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="generate the end-of-day markdown report")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--backend", choices=["gemini", "ollama"], default="ollama")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("plan", help="morning plan for today + notification")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("nudge", help="real-time drift check; notifies only when drifting")
    p.set_defaults(func=cmd_nudge)

    p = sub.add_parser("weekly", help="weekly synthesis report")
    p.add_argument("--week", metavar="YYYY-MM-DD", help="week start (default: this Monday)")
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("mock", help="write a deterministic mock timeline (test without screenpipe)")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="default: today")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("status", help="live JSON snapshot for the menu bar app (never crashes)")
    p.add_argument("--json", action="store_true", help="emit JSON (this is the default output)")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="default: today")
    p.set_defaults(func=cmd_status)

    # today — daily intentions -------------------------------------------------
    p_today = sub.add_parser("today", help="daily intentions (up to 3, time-attributed)")
    p_today.add_argument("--json", action="store_true", help="print the intentions block as JSON")
    p_today.set_defaults(func=cmd_today_show, today_action=None)
    today_sub = p_today.add_subparsers(dest="today_action", metavar="action")
    q = today_sub.add_parser("set", help='replace intentions from "a|b|c" (up to 3)')
    q.add_argument("items", metavar='"a|b|c"', help="pipe-separated intentions")
    q.set_defaults(func=cmd_today_set)
    q = today_sub.add_parser("add", help="append one intention")
    q.add_argument("text", help="intention text")
    q.add_argument("--goal", metavar="ID", help="link to a specific goal id")
    q.set_defaults(func=cmd_today_add)
    q = today_sub.add_parser("toggle", help="flip done by intention id or 1-based index")
    q.add_argument("ref", help="intention id or 1-based index")
    q.set_defaults(func=cmd_today_toggle)
    q = today_sub.add_parser("clear", help="remove today's intentions (past days' history is kept)")
    q.add_argument("--keep-history", action="store_true",
                   help="default+only behavior: never delete past days' files")
    q.set_defaults(func=cmd_today_clear)
    q = today_sub.add_parser("history", help="show past intentions + completion rate (default 7 days)")
    q.add_argument("--days", type=int, metavar="N", help=f"days to include (default {7})")
    q.add_argument("--json", action="store_true", help="print the history block as JSON")
    q.set_defaults(func=cmd_today_history)

    # focus — focus blocks -----------------------------------------------------
    p_focus = sub.add_parser("focus", help="focus block (suppresses nudges while on-goal)")
    p_focus.add_argument("--json", action="store_true", help="print the focus block as JSON")
    p_focus.set_defaults(func=cmd_focus_show, focus_action=None)
    focus_sub = p_focus.add_subparsers(dest="focus_action", metavar="action")
    q = focus_sub.add_parser("start", help="start a focus block on a goal id or name")
    q.add_argument("goal", help="goal id or name")
    q.add_argument("--minutes", type=int, metavar="N", help="auto-expire after N minutes")
    q.set_defaults(func=cmd_focus_start)
    q = focus_sub.add_parser("stop", help="stop the active focus block")
    q.set_defaults(func=cmd_focus_stop)

    # config — app-mutable settings overlay ------------------------------------
    p_config = sub.add_parser("config", help="read/write app-mutable settings (data/settings.json)")
    p_config.add_argument("--json", action="store_true", help="print effective settings as JSON")
    p_config.set_defaults(func=cmd_config_show, config_action=None)
    config_sub = p_config.add_subparsers(dest="config_action", metavar="action")
    q = config_sub.add_parser("get", help="print one setting's effective value")
    q.add_argument("key", help="setting key")
    q.set_defaults(func=cmd_config_get)
    q = config_sub.add_parser("set", help="write one setting to data/settings.json")
    q.add_argument("key", help="setting key")
    q.add_argument("value", help="new value")
    q.set_defaults(func=cmd_config_set)

    # goals — show / edit goals.md (menu bar Goals editor surface) -------------
    p_goals = sub.add_parser("goals", help="show or edit goals.md (menu bar Goals editor)")
    p_goals.add_argument("--json", action="store_true", help="print {path, raw, goals[]} as JSON")
    p_goals.set_defaults(func=cmd_goals, goals_action=None)
    goals_sub = p_goals.add_subparsers(dest="goals_action", metavar="action")
    q = goals_sub.add_parser("show", help="print goals.md (use --raw for the verbatim file text)")
    q.add_argument("--raw", action="store_true", help="print the file verbatim (no parsing)")
    q.set_defaults(func=cmd_goals_show)
    q = goals_sub.add_parser("write", help="overwrite goals.md from STDIN (atomic), then summarize")
    q.set_defaults(func=cmd_goals_write)
    q = goals_sub.add_parser("archive", help="retire a goal by id (excluded from alignment)")
    q.add_argument("goal_id", metavar="GOAL-ID", help="goal slug id (see `scoregoals goals`)")
    q.set_defaults(func=cmd_goals_archive)
    q = goals_sub.add_parser("unarchive", help="reactivate an archived goal by id")
    q.add_argument("goal_id", metavar="GOAL-ID", help="goal slug id (see `scoregoals goals`)")
    q.set_defaults(func=cmd_goals_unarchive)

    p = sub.add_parser("doctor", help="check external tools + services, print a checklist")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except NotImplementedError as exc:
        print(
            f"scoregoals: not implemented yet: {exc} — scaffold stub, see GOAL.md for the build plan.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # defense-in-depth: never dump a raw traceback
        print(f"scoregoals: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
