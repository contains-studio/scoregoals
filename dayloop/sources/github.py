"""Sensor: GitHub + local git activity for config.github_user (mgalpert).

fetch() unions two sources and dedupes:

  (1) LOCAL git sweep — every .git under config.projects_dir (find -maxdepth 2),
      `git -C <repo> log --since=<date>T00:00 --until=<nextday>T00:00` using
      local-time day boundaries. Each commit -> ActivityRecord(kind="git").

  (2) SERVER via the authenticated `gh` CLI —
        gh api users/<user>/events --paginate   (PushEvent / PullRequestEvent / ...)
        gh search prs --author=<user> --updated=<date> --json title,url,repository,state
      Each item -> ActivityRecord(kind="github").

Rules: subprocess/network only inside fetch(), imported lazily, every call has a
timeout. If gh is missing/unauthed or a repo errors, skip it with a one-line
warning — never crash the pipeline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date as _date, datetime, timedelta, timezone

from ..config import Config
from ..models import ActivityRecord


def _log(msg: str) -> None:
    print(f"[github] {msg}", file=sys.stderr)


def _next_day(date: str) -> str:
    y, m, d = (int(p) for p in date.split("-"))
    return (_date(y, m, d) + timedelta(days=1)).isoformat()


def _local_date_of(iso_ts: str) -> str | None:
    """Local calendar date (YYYY-MM-DD) of an ISO-8601 timestamp.

    GitHub's `created_at` is UTC (e.g. '2026-07-11T19:15:03Z'); convert it to
    local time before bucketing so it agrees with the local-time day window
    the git sweep and aggregate.timeline use. Returns None if unparseable.
    """
    s = (iso_ts or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:  # assume UTC when the timestamp carries no offset
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


# --- (1) local git sweep -----------------------------------------------------

def _find_repos(projects_dir: str) -> list[str]:
    """Emulate `find <projects_dir> -maxdepth 2 -name .git -type d`:
    repos are projects_dir itself (if it has .git) and each immediate child."""
    from pathlib import Path

    root = Path(projects_dir).expanduser()
    repos: list[str] = []
    if not root.is_dir():
        return repos
    if (root / ".git").is_dir():
        repos.append(str(root))
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        _log(f"cannot list {root}: {exc}")
        return repos
    for child in children:
        if (child / ".git").is_dir():
            repos.append(str(child))
    return repos


def _git_commits(date: str, config: Config) -> list[ActivityRecord]:
    if shutil.which("git") is None:
        _log("git not found on PATH; skipping local sweep")
        return []

    start = f"{date}T00:00"
    until = f"{_next_day(date)}T00:00"
    fmt = "%H%x09%an%x09%aI%x09%s"  # sha \t author \t author-date-ISO \t subject

    records: list[ActivityRecord] = []
    for repo in _find_repos(config.projects_dir):
        name = repo.rsplit("/", 1)[-1]
        try:
            proc = subprocess.run(
                ["git", "-C", repo, "log",
                 f"--since={start}", f"--until={until}",
                 f"--pretty=format:{fmt}"],
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            _log(f"git log failed for {name}: {exc}")
            continue
        if proc.returncode != 0:
            err = proc.stderr.strip().splitlines()[:1]
            _log(f"git log returned {proc.returncode} for {name}: {' '.join(err)}")
            continue
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, author, adate, subject = parts
            records.append(ActivityRecord(
                source="github", kind="git",
                start=adate, end=None,
                app=name, title=name, text=subject,
                meta={"repo": name, "sha": sha, "author": author, "path": repo},
            ))
    return records


# --- (2) gh CLI (server) -----------------------------------------------------

def _gh(args: list[str], timeout: int = 45) -> str | None:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log(f"gh {' '.join(args[:2])} failed: {exc}")
        return None
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()[:1]
        _log(f"gh {' '.join(args[:2])} returned {proc.returncode}: {' '.join(err)}")
        return None
    return proc.stdout


def _gh_events(date: str, user: str) -> list[ActivityRecord]:
    out = _gh(["api", f"users/{user}/events", "--paginate"])
    if out is None:
        return []
    try:
        events = json.loads(out)
    except json.JSONDecodeError as exc:
        _log(f"could not parse events JSON: {exc}")
        return []
    if not isinstance(events, list):
        return []

    records: list[ActivityRecord] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        created = str(ev.get("created_at") or "")
        if _local_date_of(created) != date:  # UTC created_at -> local day
            continue
        etype = str(ev.get("type") or "")
        repo = str((ev.get("repo") or {}).get("name") or "")
        payload = ev.get("payload") or {}

        if etype == "PushEvent":
            for commit in payload.get("commits") or []:
                if not isinstance(commit, dict):
                    continue
                sha = str(commit.get("sha") or "")
                msg = str(commit.get("message") or "").splitlines()[0:1]
                subject = msg[0] if msg else ""
                records.append(ActivityRecord(
                    source="github", kind="github",
                    start=created, end=None,
                    app=repo.rsplit("/", 1)[-1] if repo else None, title=repo,
                    text=subject,
                    meta={"repo": repo, "sha": sha, "event": "PushEvent"},
                ))
        elif etype == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            action = str(payload.get("action") or "")
            title = str(pr.get("title") or "")
            records.append(ActivityRecord(
                source="github", kind="github",
                start=created, end=None,
                app=repo.rsplit("/", 1)[-1] if repo else None, title=repo,
                text=f"PR {action}: {title}".strip(),
                meta={"repo": repo, "event": "PullRequestEvent",
                      "action": action, "url": pr.get("html_url"),
                      "number": pr.get("number")},
            ))
        else:
            # Keep other event types as low-detail records.
            records.append(ActivityRecord(
                source="github", kind="github",
                start=created, end=None,
                app=repo.rsplit("/", 1)[-1] if repo else None, title=repo,
                text=etype,
                meta={"repo": repo, "event": etype},
            ))
    return records


def _gh_search_prs(date: str, user: str) -> list[ActivityRecord]:
    out = _gh([
        "search", "prs", f"--author={user}", f"--updated={date}",
        "--json", "title,url,repository,state",
    ])
    if out is None:
        return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError as exc:
        _log(f"could not parse PR search JSON: {exc}")
        return []
    if not isinstance(items, list):
        return []

    records: list[ActivityRecord] = []
    for pr in items:
        if not isinstance(pr, dict):
            continue
        repo_obj = pr.get("repository") or {}
        repo = str(repo_obj.get("nameWithOwner") or repo_obj.get("name") or "")
        title = str(pr.get("title") or "")
        state = str(pr.get("state") or "")
        url = str(pr.get("url") or "")
        records.append(ActivityRecord(
            source="github", kind="github",
            start=f"{date}T00:00:00", end=None,
            app=repo.rsplit("/", 1)[-1] if repo else None, title=repo,
            text=f"PR [{state}] {title}".strip(),
            meta={"repo": repo, "event": "pr", "state": state, "url": url},
        ))
    return records


def _gh_activity(date: str, config: Config) -> list[ActivityRecord]:
    if shutil.which("gh") is None:
        _log("gh CLI not found on PATH; skipping server activity")
        return []
    user = config.github_user
    return _gh_events(date, user) + _gh_search_prs(date, user)


# --- dedup + public entry ----------------------------------------------------

def _dedup_key(rec: ActivityRecord) -> tuple:
    sha = rec.meta.get("sha")
    if sha:
        return ("sha", sha)
    url = rec.meta.get("url")
    if url:
        return ("url", url)
    return ("txt", rec.meta.get("repo"), rec.start, rec.text)


def fetch(date: str, config: Config) -> list[ActivityRecord]:
    """Fetch GitHub/git activity for `date` (YYYY-MM-DD) as ActivityRecords.

    Union of the local git sweep and the gh CLI server activity, deduped by
    commit sha / PR url. Returns whatever succeeds; never raises.
    """
    records = _git_commits(date, config) + _gh_activity(date, config)

    seen: set[tuple] = set()
    deduped: list[ActivityRecord] = []
    for rec in records:
        key = _dedup_key(rec)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rec)
    return deduped
