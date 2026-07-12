"""scoregoals.mockdata — FROZEN deterministic mock DayTimeline.

Lets the whole pipeline (alignment, analysis, reports, benchmarks) be built
and tested WITHOUT screenpipe/icalBuddy installed. Content is fully
deterministic: same `date` in -> identical timeline out (generated_at is
pinned to <date>T23:59:00, no randomness, no wall-clock dependence).
"""

from __future__ import annotations

from .models import ActivityRecord, DayTimeline, Session

__all__ = ["mock_timeline"]


def _t(date: str, hhmm: str) -> str:
    """'2026-07-11', '09:30' -> '2026-07-11T09:30:00' (local, naive)."""
    return f"{date}T{hhmm}:00"


def mock_timeline(date: str) -> DayTimeline:
    """A realistic mock day for Michael: coding on scoregoals, python docs,
    an investor Zoom (with transcript + granola note), Slack, an off-goal
    YouTube block, TradingView research, and email triage."""

    sessions = [
        Session(
            start=_t(date, "09:00"), end=_t(date, "09:25"),
            app="Mail", title="Inbox — msg@containsmsg.com",
            project=None, topic="email triage", category="comms",
            summary="Email triage: replied to two investor intros, forwarded a partnership deck, archived newsletters.",
            minutes=25.0,
            text_excerpt=(
                "Re: intro to Northwind Capital — happy to chat Thursday. "
                "Fwd: partnership deck v3. Unsubscribe. Calendar invite: Investor sync."
            ),
            record_count=41,
        ),
        Session(
            start=_t(date, "09:32"), end=_t(date, "11:14"),
            app="Code", title="cli.py — scoregoals",
            project="scoregoals", topic="scoregoals CLI + doctor command", category="coding",
            summary="Deep work on scoregoals: built argparse CLI, doctor checks, sqlite store; ran tests in integrated terminal.",
            minutes=102.0,
            text_excerpt=(
                "def cmd_doctor(args): checks.append((\"ollama\", *_check_ollama(cfg))) ... "
                "pytest -q 14 passed ... git commit -m 'feat(cli): add doctor and mock subcommands'"
            ),
            record_count=387,
        ),
        Session(
            start=_t(date, "11:15"), end=_t(date, "11:44"),
            app="Google Chrome", title="argparse — Python 3.14 documentation",
            project="scoregoals", topic="python stdlib docs", category="research",
            summary="Read python docs: argparse subparsers, tomllib, sqlite3 executescript semantics.",
            minutes=29.0,
            text_excerpt=(
                "docs.python.org/3.14/library/argparse.html add_subparsers(dest='command') ... "
                "tomllib.load(fp) ... sqlite3 — DB-API 2.0 interface"
            ),
            record_count=63,
        ),
        Session(
            start=_t(date, "12:00"), end=_t(date, "12:31"),
            app="zoom.us", title="Investor sync — Zoom",
            project=None, topic="investor update call", category="meeting",
            summary="30-min investor sync: walked through traction, discussed bridge timing, agreed to send updated metrics deck.",
            minutes=31.0,
            text_excerpt="Zoom meeting: Investor sync (see meetings[] for transcript).",
            record_count=58,
        ),
        Session(
            start=_t(date, "13:30"), end=_t(date, "14:08"),
            app="Slack", title="contains-studio — #general, #eng",
            project=None, topic="team comms", category="comms",
            summary="Slack catch-up: unblocked design review thread, coordinated deploy window, answered partner DM.",
            minutes=38.0,
            text_excerpt=(
                "#eng: deploy at 4pm works. DM @sam: contract redlines back from counsel. "
                "#design: approve v2 of the onboarding flow."
            ),
            record_count=92,
        ),
        Session(
            start=_t(date, "14:15"), end=_t(date, "14:47"),
            app="Google Chrome", title="F1 Silverstone highlights — YouTube",
            project=None, topic="youtube break", category="browsing",
            summary="Off-goal YouTube block: F1 highlights and two recommended videos.",
            minutes=32.0,
            text_excerpt="youtube.com/watch F1 Highlights ... Up next: Top 10 overtakes ... autoplay",
            record_count=44,
        ),
        Session(
            start=_t(date, "15:00"), end=_t(date, "15:47"),
            app="Google Chrome", title="TradingView — watchlist: momentum",
            project=None, topic="market research", category="research",
            summary="TradingView review of the momentum watchlist; flagged two setups, saved dual-layout charts.",
            minutes=47.0,
            text_excerpt=(
                "tradingview.com/chart NVDA 4H ascending triangle ... PLTR daily volume spike ... "
                "alert set: breakout above 52wk high"
            ),
            record_count=71,
        ),
    ]

    calendar = [
        ActivityRecord(
            source="calendar", kind="calendar",
            start=_t(date, "09:30"), end=_t(date, "11:30"),
            app="Calendar", title="Deep work block — scoregoals",
            text="Recurring focus block: no meetings, build scoregoals.",
            meta={"calendar": "Personal", "all_day": False},
        ),
        ActivityRecord(
            source="calendar", kind="calendar",
            start=_t(date, "12:00"), end=_t(date, "12:30"),
            app="Calendar", title="Investor sync",
            text="Monthly update call with Northwind Capital. Zoom link in invite.",
            meta={"calendar": "Work", "all_day": False, "attendees": 3},
        ),
        ActivityRecord(
            source="calendar", kind="calendar",
            start=_t(date, "18:30"), end=_t(date, "19:30"),
            app="Calendar", title="Gym — pull day",
            text="Training session.",
            meta={"calendar": "Personal", "all_day": False},
        ),
    ]

    github = [
        ActivityRecord(
            source="github", kind="github",
            start=_t(date, "10:05"), end=None,
            app=None, title="feat(models): freeze dataclass contracts",
            text="Commit to mgalpert/scoregoals: add ActivityRecord/Session/DayTimeline/Goal/Report + JSON helpers.",
            meta={"repo": "mgalpert/scoregoals", "sha": "a1b2c3d", "branch": "main", "additions": 214, "deletions": 0},
        ),
        ActivityRecord(
            source="github", kind="github",
            start=_t(date, "10:52"), end=None,
            app=None, title="feat(cli): add doctor and mock subcommands",
            text="Commit to mgalpert/scoregoals: doctor probes ollama/screenpipe/tools; mock writes a deterministic timeline.",
            meta={"repo": "mgalpert/scoregoals", "sha": "b2c3d4e", "branch": "main", "additions": 158, "deletions": 12},
        ),
        ActivityRecord(
            source="github", kind="github",
            start=_t(date, "11:10"), end=None,
            app=None, title="chore: scaffold module stubs + config defaults",
            text="Commit to mgalpert/scoregoals: sources/aggregate/analyze/compare/feedback stubs, config.toml defaults.",
            meta={"repo": "mgalpert/scoregoals", "sha": "c3d4e5f", "branch": "main", "additions": 96, "deletions": 3},
        ),
    ]

    meetings = [
        ActivityRecord(
            source="screenpipe", kind="audio",
            start=_t(date, "12:00"), end=_t(date, "12:31"),
            app="zoom.us", title="Investor sync — transcript",
            text=(
                "Michael: Quick update — usage doubled since the June launch, and the daily "
                "retention curve finally flattened above 40%.\n"
                "Dana (Northwind): That's the number we wanted. What's burn looking like?\n"
                "Michael: Nine months of runway at current spend. If we close the two partner "
                "deals in the pipeline it stretches past twelve.\n"
                "Dana: Then let's not rush the bridge. Send the updated metrics deck and the "
                "partner pipeline by Friday?\n"
                "Michael: Will do. One ask — an intro to your platform team for distribution.\n"
                "Dana: Done, I'll connect you this week."
            ),
            meta={"meeting": "Investor sync", "speakers": ["Michael", "Dana"], "duration_min": 31},
        ),
        ActivityRecord(
            source="granola", kind="granola",
            start=_t(date, "12:31"), end=None,
            app="Granola", title="Investor sync — notes",
            text=(
                "Summary: Positive monthly sync with Northwind. Metrics trending up; no urgency "
                "on the bridge.\n"
                "Decisions: Hold on bridge until partner deals resolve.\n"
                "Action items:\n"
                "- [ ] Michael: send updated metrics deck by Friday\n"
                "- [ ] Michael: send partner pipeline summary by Friday\n"
                "- [ ] Dana: intro to Northwind platform team this week"
            ),
            meta={"note_id": "granola-mock-001", "attendees": ["Michael", "Dana"]},
        ),
    ]

    per_app: dict[str, float] = {}
    per_cat: dict[str, float] = {}
    total = 0.0
    for s in sessions:
        total += s.minutes
        if s.app:
            per_app[s.app] = per_app.get(s.app, 0.0) + s.minutes
        cat = s.category or "other"
        per_cat[cat] = per_cat.get(cat, 0.0) + s.minutes

    stats = {
        "total_active_minutes": round(total, 1),
        "per_app_minutes": {k: round(v, 1) for k, v in per_app.items()},
        "per_category_minutes": {k: round(v, 1) for k, v in per_cat.items()},
        "counts": {
            "sessions": len(sessions),
            "calendar_events": len(calendar),
            "github_events": len(github),
            "meeting_records": len(meetings),
            "raw_records": sum(s.record_count for s in sessions),
        },
    }

    return DayTimeline(
        date=date,
        sessions=sessions,
        calendar=calendar,
        github=github,
        meetings=meetings,
        stats=stats,
        generated_at=f"{date}T23:59:00",  # pinned for determinism
    )
