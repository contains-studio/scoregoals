# ScoreGoals — the ScoreGoals menu bar app

A tiny native macOS menu bar app (SwiftUI `MenuBarExtra`, no Xcode project — pure
SwiftPM) that puts today's ScoreGoals alignment score in your menu bar and gives you
a live, **interactive** popover over the ScoreGoals engine. It shells out to the
`scoregoals` CLI, polls `status --json` on a timer to render state, and drives the
engine's write commands (`today`, `focus`, `config`, `capture`, `report`, `plan`)
straight from the UI. Every write runs off the main thread and re-polls the
engine afterward, so what you see always reflects real engine state.

## What you see

**In the menu bar:** a gauge glyph + the numeric alignment score (0–100). The
glyph and color reflect state:

- green gauge — on track
- amber gauge — drifting (score present but below target)
- red triangle — engine unavailable / no data
- greyed gauge — first fetch in flight

The whole item dims when the data is stale (older than ~2 poll cycles) or the
engine is erroring, so a frozen number never looks live.

**The popover** (click the item) has these sections:

1. **Header** — big score `NN / 100`, an "on track / drifting / engine
   unavailable" line, the date, and a **gear menu** (Settings…, Refresh now,
   Quit ScoreGoals).
2. **NOW** — what you're doing right now (current app → mapped goal) with an
   on-task/off-task dot. With no live sensor it shows "no sensor / screenpipe not
   reachable"; it lights up once screenpipe is present.
3. **TODAY'S THREE** — your top-3 intentions as a checkable list. Each row has a
   checkbox (tap to flip done → `today toggle <id>`), the intention text, a small
   bar of attributed minutes, its earning apps, and the minute total. Items
   carried over from yesterday's undone work show a subtle ↩ marker. The header
   shows the 7-day completion rate (`67% / 7d`), and a **History** disclosure
   expands to the last 7 days as compact `Mon d — n/n` rows (from `today history
   --json`; a ↩ flags days that had carried-over items). When no intentions are
   set yet, an inline three-field editor appears with a **Set today's 3** button
   (→ `today set "a|b|c"`).
4. **FOCUS** — if a block is active, the focus goal + remaining time + a **Stop**
   button (→ `focus stop`). Otherwise a **Start focus block** menu of your current
   goals with a minutes stepper (10–120, default 50) (→ `focus start <goal-id>
   --minutes N`).
5. **TIME ON GOAL** — each goal with its share of time today and its target
   (`43% / 35%`), a green/amber progress bar vs target, plus a 7-day bar chart of
   scores, the on-track-days streak (`N/7`), and the **next calendar event** with
   a countdown when one is scheduled. A small **pencil** next to the header opens
   Settings' Goals editor.
6. **QUICK ACTIONS** — **Capture** (`capture <today>`), **EOD** (`report <today>
   --backend …`, then reveals `data/reports/<today>-eod.md` in Finder), **Plan**
   (`plan`), and **Refresh**. Each shows a spinner while running and the result
   (success or error) appears inline; the UI never blocks.
7. **Footer** — health chips (screenpipe, backend/ollama), last-capture time, and
   any engine error.

## How each control maps to the engine

| UI control                         | scoregoals command                              |
|------------------------------------|----------------------------------------------|
| Intention checkbox                 | `today toggle <id>`                          |
| "Set today's 3" editor             | `today set "a\|b\|c"`                         |
| History disclosure                 | `today history --days 7 --json`              |
| Start focus block (goal menu)      | `focus start <goal-id> --minutes <N>`        |
| Stop (focus)                       | `focus stop`                                 |
| Capture now                        | `capture <today>`                            |
| EOD report                         | `report <today> --backend <ollama\|gemini>`  |
| Plan my day                        | `plan`                                       |
| Settings: default backend          | `config set default_backend <ollama\|gemini\|both>` |
| Settings: nudges / pause capture   | `config set nudges_enabled\|capture_paused <bool>` |
| Settings: refresh cadence          | `config set refresh_seconds <n>` + live re-poll |
| Settings: Save goals               | `goals write` (new markdown piped on **stdin**) |
| Settings: Archive / Unarchive goal | `goals archive\|unarchive <goal-id>`         |

Reads are `status --json` (every poll), `config --json` (on launch + when Settings
opens), and `goals --json` (when the Goals editor loads). `report`'s backend is
derived from `status.health.backend.default` (`both` maps to `ollama`, since
`report --backend` accepts only `ollama`/`gemini`).

## Settings

Open **Settings…** from the gear menu. It loads current values from
`config --json` and writes each change back through the engine:

- **Default backend** — `ollama` / `gemini` / `both`. The `gemini` backend needs
  no API key on this machine: it prefers the **Antigravity CLI** (`agy`, model
  `gemini-3.5-flash`) when installed, falling back to the deprecated legacy
  `gemini` CLI, and uses the google-genai SDK only when a key is set. Those
  responses are covered by the Antigravity subscription, so their benchmarked
  `cost_usd` is 0.
- **Refresh cadence** — 15s / 30s / 1m / 5m. Writes `refresh_seconds` **and**
  updates the live poll cadence immediately.
- **Nudges enabled** / **Pause capture** — boolean toggles.
- **Goals** — a monospaced editor for `goals.md`, loaded from `goals --json` (the
  verbatim `raw` field). Edit inline; an **edited** dot shows unsaved changes.
  **Save goals** pipes the text to `goals write` on stdin (atomic temp+rename in
  the engine), shows the returned summary line — e.g. `wrote goals.md (4 goals:
  …)` — or an error inline, and re-polls so the day score reflects the new goals.
  **Reload** re-fetches from disk; **Open file** opens `goals.md` in your default
  editor. Saving never rejects: content that parses to zero goals is still written
  (the engine warns), so a mid-draft file is never lost. Below the raw editor a
  compact **per-goal list** (name + target, with an `archived` tag) offers a
  one-click **Archive / Unarchive** button per goal (→ `goals archive|unarchive
  <goal-id>`) — archived goals stay in the file but drop out of alignment,
  targets, and drift. The raw editor remains the power path.
- **Engine location** — a repo directory or engine binary, persisted in
  UserDefaults (key `scoregoalsEnginePath`) and used by `ScoreGoalsClient` so
  `$SCOREGOALS_BIN` is no longer the only override. "Apply path" rebuilds the engine
  client and re-polls; the resolved invocation is shown below the field.
- **Launch at login** — see below.

## Launch at login

The **Launch at login** toggle in Settings is a real login item backed by
`SMAppService.mainApp` (`register()` / `unregister()`, reflecting `.status`). If
registration fails — which can happen when the app is ad-hoc signed or run from
outside `/Applications` — the error is caught and shown as a one-line hint
("Move ScoreGoals.app to /Applications and try again"); the app never crashes.
For the most reliable behavior, move **ScoreGoals.app** to `/Applications` before
enabling it.

## Build

```sh
bash menubar/build.sh
```

This runs `swift build -c release`, assembles `menubar/ScoreGoals.app`
(Contents/MacOS + Info.plist with `LSUIElement=1` so there's no Dock icon), and
**ad-hoc code-signs** it (`codesign --sign -`). Output:
`menubar/ScoreGoals.app`. Requires only the Swift toolchain (macOS 14+).

## Run

Double-click **ScoreGoals.app** in Finder. Because it's ad-hoc signed (not
notarized), the **first** launch Gatekeeper will block it — **right-click the app
→ Open**, then confirm. After that it opens normally.

> A `MenuBarExtra` accessory app only attaches its item to the menu bar inside a
> real, logged-in Aqua GUI session. Launching the bare binary over SSH/headless
> runs the process and polls the engine (you can watch it in the debug log) but
> the icon won't appear until it's run in a desktop login session. This is a
> macOS constraint, not a bug — run the `.app` from Finder on the desktop to see
> the item.

Look for the gauge + score at the top-right of your menu bar. Click it for the
popover; use **Quit ScoreGoals** in the gear menu to exit.

### Run the binary directly (for debugging)

```sh
# logs every engine call to a file
SCOREGOALS_BAR_DEBUG=/tmp/scoregoalsbar.log \
  menubar/ScoreGoals.app/Contents/MacOS/ScoreGoals
```

Env knobs:

- `SCOREGOALS_BAR_DEBUG=<path>` (or `=1` → `$TMPDIR/scoregoalsbar.log`) — append a
  timestamped line for every engine call and its result.
- `SCOREGOALS_REFRESH_SECONDS=<n>` — initial poll cadence (default 30, minimum 1).
  Settings' refresh-cadence picker overrides this at runtime.
- `SCOREGOALS_BIN=<path>` — hard override for the engine executable (see below).

## How it finds the engine

At launch (and whenever you Apply a new path in Settings) the app resolves how to
invoke scoregoals, in this order:

1. UserDefaults `scoregoalsEnginePath` (from Settings) — a **repo directory** (used
   as the working dir + probed for `.venv/bin/…`) or an **executable binary**.
2. `$SCOREGOALS_BIN` if set and non-empty (run as-is; working dir = the resolved
   repo).
3. `<repo>/.venv/bin/scoregoals` — the venv console script, if present.
4. `<repo>/.venv/bin/python -m scoregoals` — module fallback.

It always runs the child with the repo as the working directory, so the engine
finds `data/`, `config.toml`, and `goals.md`.

## Sensors absent (screenpipe / icalBuddy)

screenpipe and icalBuddy needn't be installed; the app degrades gracefully. The
live **NOW** line shows "no sensor" and the **next event** stays empty while the
screenpipe health chip stays grey. Everything that doesn't need a live sensor —
the score, intentions, focus, per-goal breakdown, weekly bars, quick actions, and
health/backend info — still works from the engine's stored timeline. Generate a
deterministic day to see the full popover with no sensors:

```sh
.venv/bin/python -m scoregoals mock --date 2026-07-11
```

Install screenpipe/icalBuddy later and the live sections fill in automatically —
no app change required.
