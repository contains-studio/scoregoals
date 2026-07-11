"""macOS notifications: terminal-notifier first, osascript fallback."""

from __future__ import annotations

import sys


def _which(name: str) -> str | None:
    """shutil.which plus the usual Homebrew locations (launchd-safe)."""
    import shutil
    from pathlib import Path

    found = shutil.which(name)
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        cand = Path(d) / name
        if cand.exists():
            return str(cand)
    return None


def notify(title: str, message: str) -> None:
    """Post a macOS notification.

    1. terminal-notifier (installed at /opt/homebrew/bin/terminal-notifier;
       also try shutil.which) with -title/-message/-sound flags.
    2. Fallback: `osascript -e 'display notification "..." with title "..."'`.
    Never raises — a failed notification prints a one-line warning and returns.
    Subprocess calls only inside this function.
    """
    import subprocess

    try:
        tn = _which("terminal-notifier")
        if tn:
            subprocess.run(
                [tn, "-title", title, "-message", message, "-sound", "default"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return

        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # a failed notification must never break a command
        print(f"warning: notification failed ({exc})", file=sys.stderr)
