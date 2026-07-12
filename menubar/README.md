# DayloopBar — the dayloop menu bar app

A tiny native macOS menu bar app (SwiftUI `MenuBarExtra`, no Xcode project — pure
SwiftPM) that puts today's dayloop alignment score in your menu bar and shows a
live popover of where your time is going. It is a **read-only viewer** over the
dayloop engine: it shells out to the `dayloop` CLI, polls `status --json` on a
timer, and renders the result. It never writes to your data.

## What you see

**In the menu bar:** a gauge glyph + the numeric alignment score (0–100). The
glyph and color reflect state:

- green gauge — on track
- amber gauge — drifting (score present but below target)
- red triangle — engine unavailable / no data
- greyed gauge — first fetch in flight

The whole item dims when the data is stale (older than ~2 poll cycles) or the
engine is erroring, so a frozen number never looks live.

**The popover** (click the item) has four sections:

1. **Header** — big score `NN / 100`, an "on track / drifting / engine
   unavailable" line, the date, and a gear menu (Refresh now; settings are a
   placeholder — see below).
2. **NOW** — what you're doing right now (current app → mapped goal) with an
   on-task/off-task dot. Because screenpipe isn't installed, this shows
   "no sensor / screenpipe not reachable" and a grey dot; it lights up once a
   live sensor is present.
3. **GOALS** — each goal with its share of time today and its target
   (`43% / 35%`), a green/amber dot per goal, and a weekly sparkline.
4. **Footer** — health chips (screenpipe, backend/ollama), last-capture time,
   any engine error, and **Refresh** / **Quit** buttons.

## Build

```sh
bash menubar/build.sh
```

This runs `swift build -c release`, assembles `menubar/DayloopBar.app`
(Contents/MacOS + Info.plist with `LSUIElement=1` so there's no Dock icon), and
**ad-hoc code-signs** it (`codesign --sign -`). Output:
`menubar/DayloopBar.app`. Requires only the Swift toolchain (macOS 14+).

## Run

Double-click **DayloopBar.app** in Finder. Because it's ad-hoc signed (not
notarized), the **first** launch Gatekeeper will block it — **right-click the app
→ Open**, then confirm. After that it opens normally.

> A `MenuBarExtra` accessory app only attaches its item to the menu bar inside a
> real, logged-in Aqua GUI session. Launching the bare binary over SSH/headless
> runs the process and polls the engine (you can watch it in the debug log) but
> the icon won't appear until it's run in a desktop login session. This is a
> macOS constraint, not a bug — run the `.app` from Finder on the desktop to see
> the item.

Look for the gauge + score at the top-right of your menu bar. Click it for the
popover; use **Quit** in the footer to exit.

### Run the binary directly (for debugging)

```sh
# logs every engine call to a file
DAYLOOP_BAR_DEBUG=/tmp/dayloopbar.log \
  menubar/DayloopBar.app/Contents/MacOS/DayloopBar
```

Env knobs:

- `DAYLOOP_BAR_DEBUG=<path>` (or `=1` → `$TMPDIR/dayloopbar.log`) — append a
  timestamped line for every `status --json` call and its result.
- `DAYLOOP_REFRESH_SECONDS=<n>` — poll cadence (default 30, minimum 1).
- `DAYLOOP_BIN=<path>` — hard override for the engine executable (see below).

## How it finds the engine

At launch the app resolves how to invoke dayloop, in this order:

1. `$DAYLOOP_BIN` if set and non-empty (run as-is, no extra args).
2. `/Users/contains/projects/dayloop/.venv/bin/dayloop` — the venv console
   script, if present and executable (this is the normal case).
3. `/Users/contains/projects/dayloop/.venv/bin/python -m dayloop` — module
   fallback.

It always runs the child with the repo as the working directory, so the engine
finds `data/`, `config.toml`, and `goals.md`. To point the app at a different
checkout or interpreter, launch it with `DAYLOOP_BIN` set. (The in-app gear
menu's "Settings" entry is a placeholder — engine overrides are via the env var
today.)

## Start at login

The gear menu labels a Settings/login-item toggle as "coming soon"; it is not
wired yet. To make the app start at login now, add it via **System Settings**:

1. **System Settings → General → Login Items**.
2. Under **Open at Login**, click **+**, choose
   `menubar/DayloopBar.app`, and add it.

It will then launch (and start polling) automatically each time you log in.

## Sensors absent (screenpipe / icalBuddy)

screenpipe and icalBuddy aren't installed on this machine, and the app degrades
gracefully: the live **NOW** line shows "no sensor" and the **next event** /
calendar data stay empty, while the screenpipe health chip stays grey. Everything
that doesn't need a live sensor — the score, per-goal breakdown, weekly
sparkline, and health/backend info — still renders from the engine's stored
timeline. Generate a deterministic day to see the full popover with no sensors:

```sh
/Users/contains/projects/dayloop/.venv/bin/python -m dayloop mock --date 2026-07-11
```

Install screenpipe/icalBuddy later and the live sections fill in automatically —
no app change required.
