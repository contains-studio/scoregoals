#!/usr/bin/env bash
#
# scoregoals setup — the ONE command a colleague runs after cloning.
#
#   ./scripts/setup.sh                       # interactive, idempotent
#   ./scripts/setup.sh --non-interactive     # take all defaults, no prompts
#   ./scripts/setup.sh --gemini-key KEY      # set the BYOK Gemini key
#   ./scripts/setup.sh --projects-dir ~/code # dir scanned for your git projects
#
# What it does (all steps are safe to re-run):
#   1. Check prerequisites (python3 >=3.12, git; swift is optional).
#   2. Create the .venv and install scoregoals (editable) — prefers uv.
#   3. Optionally record your Gemini API key + projects dir (per-user, in
#      data/settings.json — never in git).
#   4. Write a starter goals.md IF you don't already have one.
#   5. Build the menu bar app if the swift toolchain is present.
#   6. Run `scoregoals doctor` and print next steps.
#
# It never edits config.toml or overwrites an existing goals.md / settings.

set -eu

# --- Resolve paths -----------------------------------------------------------
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
VENV_DIR="$REPO_DIR/.venv"
PY="$VENV_DIR/bin/python"

# --- Flags -------------------------------------------------------------------
NON_INTERACTIVE=0
GEMINI_KEY=""
GEMINI_KEY_SET=0
PROJECTS_DIR=""
PROJECTS_DIR_SET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=1 ;;
        --gemini-key)      GEMINI_KEY="${2:-}"; GEMINI_KEY_SET=1; shift ;;
        --gemini-key=*)    GEMINI_KEY="${1#*=}"; GEMINI_KEY_SET=1 ;;
        --projects-dir)    PROJECTS_DIR="${2:-}"; PROJECTS_DIR_SET=1; shift ;;
        --projects-dir=*)  PROJECTS_DIR="${1#*=}"; PROJECTS_DIR_SET=1 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "setup.sh: unknown option '$1' (try --help)" >&2
            exit 2 ;;
    esac
    shift
done

echo "==> scoregoals setup  (repo: $REPO_DIR)"

# --- 1. Prerequisites --------------------------------------------------------
echo "==> Checking prerequisites"

if ! command -v git >/dev/null 2>&1; then
    echo "    ERROR: git is required but was not found on PATH." >&2
    exit 1
fi
echo "    git   ok ($(command -v git))"

if ! command -v python3 >/dev/null 2>&1; then
    echo "    ERROR: python3 is required but was not found on PATH." >&2
    exit 1
fi
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)'; then
    echo "    ERROR: python >= 3.12 required, found $PYVER." >&2
    exit 1
fi
echo "    python3 ok ($PYVER)"

HAVE_UV=0
if command -v uv >/dev/null 2>&1; then HAVE_UV=1; echo "    uv    ok ($(command -v uv))"; fi

HAVE_SWIFT=0
if command -v swift >/dev/null 2>&1; then
    HAVE_SWIFT=1
    echo "    swift ok ($(command -v swift)) — menu bar app will be built"
else
    echo "    swift not found — skipping the menu bar app build (engine still installs)"
fi

# --- 2. venv + editable install ---------------------------------------------
if [ -d "$VENV_DIR" ]; then
    echo "==> Reusing existing venv at $VENV_DIR"
else
    echo "==> Creating venv at $VENV_DIR"
    if [ "$HAVE_UV" -eq 1 ]; then
        uv venv "$VENV_DIR"
    else
        python3 -m venv "$VENV_DIR"
    fi
fi

echo "==> Installing scoregoals (editable)"
if [ "$HAVE_UV" -eq 1 ]; then
    VIRTUAL_ENV="$VENV_DIR" uv pip install -e "$REPO_DIR"
else
    "$PY" -m pip install --upgrade pip >/dev/null
    "$PY" -m pip install -e "$REPO_DIR"
fi

if [ ! -x "$PY" ]; then
    echo "    ERROR: venv python not found at $PY after install." >&2
    exit 1
fi

# --- 3. Per-user settings (Gemini BYOK + projects dir) -----------------------
# All writes go to data/settings.json via `scoregoals config set` (gitignored),
# never to config.toml. The Gemini key is never echoed.

# a. Gemini API key
if [ "$GEMINI_KEY_SET" -eq 0 ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    printf "Gemini API key (optional, blank = local-only / add later): "
    # -s hides the key; guard for shells/pipes where -s is unavailable.
    if read -r -s GEMINI_KEY 2>/dev/null; then echo; else read -r GEMINI_KEY; fi
    GEMINI_KEY_SET=1
fi
if [ "$GEMINI_KEY_SET" -eq 1 ] && [ -n "$GEMINI_KEY" ]; then
    "$PY" -m scoregoals config set gemini_api_key "$GEMINI_KEY" >/dev/null
    echo "==> Gemini API key stored (data/settings.json, gitignored)"
    unset GEMINI_KEY
else
    echo "==> No Gemini key set — analysis stays local (ollama), with the gemini CLI OAuth fallback if present"
fi

# b. projects dir
if [ "$PROJECTS_DIR_SET" -eq 0 ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    printf "Directory to scan for your git projects [~/projects]: "
    read -r PROJECTS_DIR
    PROJECTS_DIR_SET=1
fi
if [ "$PROJECTS_DIR_SET" -eq 1 ]; then
    # Blank answer -> the ~/projects default.
    [ -n "$PROJECTS_DIR" ] || PROJECTS_DIR="$HOME/projects"
    # Expand a leading ~ to $HOME.
    case "$PROJECTS_DIR" in "~"/*) PROJECTS_DIR="$HOME/${PROJECTS_DIR#~/}" ;; "~") PROJECTS_DIR="$HOME" ;; esac
    "$PY" -m scoregoals config set projects_dir "$PROJECTS_DIR" >/dev/null
    echo "==> projects_dir = $PROJECTS_DIR"
fi

# --- 4. Starter goals.md (only if missing) -----------------------------------
GOALS_FILE="$REPO_DIR/goals.md"
if [ -f "$GOALS_FILE" ]; then
    echo "==> Keeping your existing goals.md"
else
    echo "==> Writing a starter goals.md"
    cat > "$GOALS_FILE" <<'GOALS'
<!--
scoregoals goals file — parsed by scoregoals/compare/align.py (load_goals).

Format, one goal per section:

  ## Goal: <name>
  keywords: comma, separated, keywords      <- matched (case-insensitive) against
                                               session app/title/project/topic/summary/excerpt
  target_pct: 30                            <- optional; % of ACTIVE time you want on this goal
  <description paragraph — free text, fed to the LLM for context>

Notes:
- Each session counts toward AT MOST ONE goal (most distinct keyword hits wins;
  ties break by the order below). Targets need not sum to 100.
- Edit freely; changes apply on the next capture/analyze run.
-->

## Goal: Deep work
keywords: code, vscode, terminal, iterm, github, python, debugging, refactor, commit, docs
target_pct: 50
Long, uninterrupted maker blocks — writing and shipping work, reading docs,
reviewing changes. The point is contiguous focus, not just total minutes.

## Goal: Communications
keywords: email, gmail, mail, slack, zoom, meeting, calendar, message, reply
target_pct: 20
Keep collaborators and stakeholders in the loop without letting comms eat the
day: timely replies, tight calls, follow-ups shipped the same week.
GOALS
fi

# --- 5. Menu bar app ---------------------------------------------------------
if [ "$HAVE_SWIFT" -eq 1 ]; then
    echo "==> Building the menu bar app (menubar/build.sh)"
    bash "$REPO_DIR/menubar/build.sh"
else
    echo "==> Skipping menu bar app (no swift toolchain)"
fi

# --- 6. Doctor + next steps --------------------------------------------------
echo ""
echo "==> Running scoregoals doctor"
"$PY" -m scoregoals doctor || true

echo ""
echo "======================================================================"
echo " scoregoals setup complete."
echo ""
echo " Next steps:"
echo "   1. Install the screenpipe desktop app (external, free for personal"
echo "      use):  https://screenpi.pe  — then grant Screen Recording +"
echo "      Microphone. scoregoals detects it at http://localhost:3030."
echo "   2. (optional) Schedule the daily/weekly jobs:  scripts/install.sh"
if [ "$HAVE_SWIFT" -eq 1 ]; then
echo "   3. Open the menu bar app:  open menubar/ScoreGoals.app"
fi
echo ""
echo " Try it now (no screenpipe needed):"
echo "   .venv/bin/python -m scoregoals mock --date 2026-07-11"
echo "   .venv/bin/python -m scoregoals analyze 2026-07-11 --backend ollama"
echo ""
echo " Change the Gemini key later:"
echo "   .venv/bin/python -m scoregoals config set gemini_api_key <key>   # '' clears"
echo "======================================================================"
