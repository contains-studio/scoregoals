#!/bin/sh
# scoregoals installer — idempotent setup for the local venv + launchd user agents.
#
# Usage:
#   scripts/install.sh            # create venv, render + load launchd agents
#   scripts/install.sh uninstall  # unload + remove launchd agents
#
# Make executable once with:  chmod +x scripts/install.sh
# (also runnable directly as `sh scripts/install.sh`).
#
# This script owns ONLY environment/launchd wiring. It never edits Python modules.

set -eu

# --- Resolve paths -----------------------------------------------------------
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
PLIST_SRC_DIR="$REPO_DIR/scoregoals/launchd"

VENV_DIR="$REPO_DIR/.venv"
SCOREGOALS_BIN="$VENV_DIR/bin/scoregoals"
SCOREGOALS_BINDIR="$VENV_DIR/bin"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/scoregoals"

# uv location per this machine (falls back to PATH lookup).
UV_BIN="$HOME/.local/bin/uv"
[ -x "$UV_BIN" ] || UV_BIN="uv"

AGENTS="com.scoregoals.morning com.scoregoals.eod com.scoregoals.weekly com.scoregoals.nudge"

# Legacy pre-rebrand labels — cleaned up on uninstall so anyone who loaded the
# old com.dayloop.* agents ends up with a clean slate after the rename.
LEGACY_AGENTS="com.dayloop.morning com.dayloop.eod com.dayloop.weekly com.dayloop.nudge"

# GUI domain for launchctl bootstrap/bootout (per-user Aqua session).
GUI_DOMAIN="gui/$(id -u)"

# --- Uninstall path ----------------------------------------------------------
uninstall() {
    echo "==> Uninstalling scoregoals launchd agents (incl. legacy com.dayloop.*)"
    for label in $AGENTS $LEGACY_AGENTS; do
        plist="$LAUNCH_AGENTS_DIR/$label.plist"
        # bootout (modern) then fall back to legacy unload; ignore errors.
        launchctl bootout "$GUI_DOMAIN/$label" 2>/dev/null || true
        launchctl unload "$plist" 2>/dev/null || true
        if [ -f "$plist" ]; then
            rm -f "$plist"
            echo "    removed $plist"
        fi
    done
    echo "==> Done. Venv at $VENV_DIR and logs at $LOG_DIR were left in place."
    exit 0
}

if [ "${1:-}" = "uninstall" ] || [ "${1:-}" = "--uninstall" ]; then
    uninstall
fi

# --- 1. Python venv via uv ---------------------------------------------------
echo "==> Creating venv with uv at $VENV_DIR"
"$UV_BIN" venv "$VENV_DIR" --python 3.14
echo "==> Installing scoregoals (editable) into the venv"
VIRTUAL_ENV="$VENV_DIR" "$UV_BIN" pip install -e "$REPO_DIR"

if [ ! -x "$SCOREGOALS_BIN" ]; then
    echo "ERROR: expected console script not found at $SCOREGOALS_BIN" >&2
    exit 1
fi

# --- 2. Directories ----------------------------------------------------------
echo "==> Ensuring log + LaunchAgents directories exist"
mkdir -p "$LOG_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"

# --- 3. Render + install plists ---------------------------------------------
# Substitute the four placeholders in each template and copy into LaunchAgents.
# sed with '|' delimiter so path slashes need no escaping.
echo "==> Rendering + loading launchd agents"
for label in $AGENTS; do
    src="$PLIST_SRC_DIR/$label.plist"
    dst="$LAUNCH_AGENTS_DIR/$label.plist"

    if [ ! -f "$src" ]; then
        echo "ERROR: missing plist template $src" >&2
        exit 1
    fi

    sed \
        -e "s|__SCOREGOALS_BIN__|$SCOREGOALS_BIN|g" \
        -e "s|__SCOREGOALS_BINDIR__|$SCOREGOALS_BINDIR|g" \
        -e "s|__SCOREGOALS_REPO__|$REPO_DIR|g" \
        -e "s|__HOME__|$HOME|g" \
        "$src" > "$dst"

    # Reload cleanly: bootout any existing instance, then bootstrap fresh.
    launchctl bootout "$GUI_DOMAIN/$label" 2>/dev/null || true
    launchctl unload "$dst" 2>/dev/null || true
    if launchctl bootstrap "$GUI_DOMAIN" "$dst" 2>/dev/null; then
        echo "    loaded $label (bootstrap)"
    else
        # Fallback for older macOS.
        launchctl load "$dst"
        echo "    loaded $label (load)"
    fi
done

# --- Done --------------------------------------------------------------------
echo ""
echo "==> scoregoals installed."
echo "    venv:    $VENV_DIR"
echo "    binary:  $SCOREGOALS_BIN"
echo "    agents:  $LAUNCH_AGENTS_DIR/com.scoregoals.*.plist"
echo "    logs:    $LOG_DIR"
echo ""
echo "    Schedules:"
echo "      morning  com.scoregoals.morning  07:30 daily   -> scoregoals plan"
echo "      eod      com.scoregoals.eod      21:00 daily   -> scoregoals capture <today> && report <today> --backend ollama"
echo "      weekly   com.scoregoals.weekly   Sun 20:00     -> scoregoals weekly"
echo "      nudge    com.scoregoals.nudge    every 20 min  -> scoregoals nudge"
echo ""
echo "    Verify:     launchctl list | grep com.scoregoals"
echo "    Uninstall:  scripts/install.sh uninstall"
echo ""
echo "Next: install screenpipe and grant permissions (see GOAL.md)."
