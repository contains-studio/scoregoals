"""dayloop.cli — FROZEN command-line interface.

Subcommands orchestrate the module functions. Many modules start as scaffold
stubs raising NotImplementedError; main() converts that into a clean one-line
message and exit code 2 instead of a traceback.

`doctor` and `mock` are fully implemented HERE and must always work, even on
a bare system python with no third-party packages installed (stdlib only —
requests etc. are imported lazily inside the source modules, never here).
"""

from __future__ import annotations

import argparse
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


# --- subcommand handlers -----------------------------------------------------


def cmd_capture(args: argparse.Namespace) -> int:
    """capture <date>: build the timeline from all sources and store it."""
    cfg = _cfg(args)
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
    notify.notify("dayloop — morning plan", first[:200])
    print(f"morning plan: {out}")
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    """nudge: real-time drift check; notify only if drifting."""
    cfg = _cfg(args)
    from .feedback import notify, nudge

    msg = nudge.check(cfg)
    if msg:
        notify.notify("dayloop — drift", msg)
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
            " — install/start screenpipe for live capture (mock mode works without it)"
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
    cli = _which("gemini")
    if cfg.gemini_api_key:
        return True, "GEMINI_API_KEY set" + (f"; CLI at {cli}" if cli else "")
    if cli:
        return True, f"no GEMINI_API_KEY, but gemini CLI (OAuth) at {cli}"
    return False, "no GEMINI_API_KEY and no gemini CLI — gemini backend unavailable (ollama still works)"


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

    print("dayloop doctor — environment checklist\n")
    for name, ok, detail in checks:
        glyph = GLYPH_OK if ok else GLYPH_BAD
        print(f"  {glyph} {name:<18} {detail}")
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed.")
    if not checks[0][1]:
        print(
            "tip: dayloop works right now without screenpipe —"
            " `python -m dayloop mock` then analyze with the ollama backend."
        )
    return 0


# --- parser / main -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dayloop",
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
            f"dayloop: not implemented yet: {exc} — scaffold stub, see GOAL.md for the build plan.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
