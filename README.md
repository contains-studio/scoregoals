# ScoreGoals

[scoregoals.app](https://scoregoals.app) — **know where your day actually went.**

ScoreGoals watches what you really do on your Mac — the apps you're in, the
text on your screen, your meetings and commits — scores each day against goals
*you* write down, and nudges you when you drift. At night you get an honest
report; over time it learns your patterns from your corrections and gets
quieter. Everything stays on your machine.

- **See it**: a menu bar score (0–100) of how aligned today is with your goals,
  with the evidence one click away.
- **Steer it**: set three intentions each morning; get a gentle nudge when
  you've been off-goal too long; read the end-of-day report.
- **Teach it**: when it mislabels a session, one tap fixes it — repeat
  corrections become automatic rules, so it needs you less every week.
- **Trust it**: local-first (screen data never leaves your Mac), secrets are
  scrubbed before anything is stored or analyzed, capture pauses when you lock
  your screen or step away, and days with too little data say "insufficient
  data" instead of inventing a number.

## How it works

```
      ┌───────────────────────── WATCH ───────────────────────────┐
      │  screenpipe (screen text · meeting audio)  Calendar        │
      │  GitHub commits & PRs · Granola notes                      │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼
      ┌──────────────────────── UNDERSTAND ───────────────────────┐
      │  raw capture → work sessions → secrets scrubbed            │
      │  → a timeline of your day                                  │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼                    goals.md
      ┌──────────────────────── SCORE ────────────────────────◄───┐
      │  each session mapped to one of your goals (your            │
      │  corrections > learned rules > keywords > LLM guess)       │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼
      ┌──────────────────────── NUDGE ────────────────────────────┐
      │  morning plan · drift nudges · end-of-day report ·         │
      │  weekly review                                             │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼
                       you adjust tomorrow ──► (loop)
```

The day's narrative is written by a pluggable LLM — local
[Ollama](https://ollama.com) or Google Gemini (bring your own key) — and every
run is benchmarked (cost, latency, quality) in `data/benchmarks/compare.csv` so
you pick a backend with data, not vibes. The score itself is deterministic
math, identical whichever model writes the prose.

## Quick start

One command sets up the venv, installs the engine, and (optionally) builds the
menu bar app. See [SETUP.md](SETUP.md) for the full guide.

```sh
git clone https://github.com/contains-studio/scoregoals ~/projects/scoregoals
cd ~/projects/scoregoals
./scripts/setup.sh                           # venv + install + prompts, idempotent
```

Then, from the repo (works immediately, no screenpipe needed — uses a mock day):

```sh
.venv/bin/python -m scoregoals doctor                    # environment checklist
.venv/bin/python -m scoregoals mock                      # deterministic test timeline
.venv/bin/python -m scoregoals analyze 2000-01-01 --backend ollama   # local model + benchmark row
.venv/bin/python -m scoregoals report  2000-01-01 --backend ollama   # -> data/reports/…-eod.md
```

For live capture, install the screenpipe CLI (`npm i -g screenpipe`), build the
recorder wrapper (`bash recorder/build.sh`), open it, and grant Screen
Recording + Microphone + Accessibility. Gemini is bring-your-own-key
(optional); set it during setup or later with
`.venv/bin/python -m scoregoals config set gemini_api_key <key>`.

All commands: `capture`, `analyze`, `report`, `plan`, `nudge`, `weekly`,
`mock`, `status`, `today`, `focus`, `config`, `review`, `label`, `learn`,
`goals`, `timeline`, `search`, `labels`, `rules`, `bench`, `reports`, `trend`,
`doctor` — see `python3 -m scoregoals --help`. The JSON surfaces the menu bar
app drives are documented in `docs/STATUS_SCHEMA.md`.

The read-only `timeline`/`search`/`labels`/`rules`/`bench`/`reports`/`trend`
commands (plus `status`/`review`) are the **agent-facing API**: everything an
automated "check on me" agent needs, as clean `--json`. Documented in
**[docs/AGENT_API.md](docs/AGENT_API.md)**.

## Where the models run

Everything runs locally by default (`ollama_url = http://localhost:11434` in
`config.toml`). If you'd rather serve the model from a beefier box, it's one
config value — e.g. over Tailscale:

```sh
export SCOREGOALS_OLLAMA_URL=http://your-server.<tailnet>.ts.net:11434
# or set ollama_url in config.toml
```

On that host: `OLLAMA_HOST=0.0.0.0 ollama serve` and pull your model. Capture,
scoring, and storage still happen on your Mac; only the narrative-writing call
is redirected. Gemini (if you opt in with a key) only ever receives the
redacted text digest — never screenshots or audio.

## Menu bar app

A native macOS menu bar app lives in [`menubar/`](menubar/): today's score in
the bar, and a popover with your current activity, today's three intentions,
the sessions that need review (one-tap corrections), per-goal time, a weekly
trend, and quick actions.

```sh
bash menubar/build.sh          # -> menubar/ScoreGoals.app (ad-hoc signed)
open menubar/ScoreGoals.app    # first launch: right-click -> Open
```

See [`menubar/README.md`](menubar/README.md) for how it finds the engine, how
to start it at login, and how it degrades gracefully when sensors are absent.

## Layout

- `scoregoals/models.py` — frozen data contracts (ActivityRecord, Session,
  DayTimeline, Goal, GoalAlignment, Report)
- `scoregoals/sources/` — what it watches · `scoregoals/aggregate/` — how raw
  capture becomes a timeline · `scoregoals/compare/` + `scoregoals/analyze/` —
  scoring and narratives · `scoregoals/feedback/` — plans, nudges, reports
- `recorder/` — a tiny signed wrapper app that gives the screenpipe CLI a
  stable macOS permission identity (and pauses capture when you're away)
- `config.toml` — defaults that work out of the box; `goals.md` — your goals
- `data/` — sqlite + JSON timelines/reports/benchmarks (gitignored — your data
  never ships with this repo)

## Setup

New here? [SETUP.md](SETUP.md) has the three-step install, where data lives,
and how to set the Gemini key. The project's definition of done and test plan
live in [GOAL.md](GOAL.md).
