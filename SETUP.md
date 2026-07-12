# Setting up ScoreGoals

ScoreGoals ([scoregoals.app](https://scoregoals.app)) is a personal, local-first
activity tracker for macOS: it captures what
actually happens on your Mac, compares it against your `goals.md`, and closes the
loop with a morning plan, drift nudges, an end-of-day report, and a weekly
synthesis. Everything stays on your machine except what you explicitly send to
Gemini.

## Prerequisites

- **macOS 14+** (Apple Silicon or Intel).
- **python3 ≥ 3.12** — `python3 --version`.
- **git**.
- **swift toolchain** — *optional*, only for the menu bar app. Install Xcode or
  the Command Line Tools (`xcode-select --install`). Setup skips the app build if
  swift is absent.
- **[screenpipe](https://screenpi.pe) desktop app** — *optional but recommended*
  for live capture. It's an external dependency you install yourself; it's free
  for personal use. scoregoals only detects it at `http://localhost:3030` and never
  bundles or launches it. Without it, everything still works against a mock day.
- **[Ollama](https://ollama.com)** running locally for the default (free) local
  analysis backend, or a Gemini API key (below).

## Install (3 steps)

```sh
# 1. Clone
git clone <this-repo> ~/projects/scoregoals
cd ~/projects/scoregoals

# 2. One setup command (idempotent — safe to re-run)
./scripts/setup.sh

# 3. Install the screenpipe desktop app and grant permissions
#    https://screenpi.pe  ->  System Settings ->
#    Privacy & Security -> Screen Recording + Microphone -> enable screenpipe
```

`setup.sh` creates the `.venv`, installs the engine, optionally records your
Gemini key and projects directory, writes a starter `goals.md` if you don't have
one, builds the menu bar app when swift is present, and finishes with
`scoregoals doctor`.

Non-interactive / scripted variants:

```sh
./scripts/setup.sh --non-interactive
./scripts/setup.sh --gemini-key "$MY_KEY" --projects-dir ~/code
```

## Where your data lives

Everything is local and gitignored:

- `data/` — sqlite DB, JSON timelines, reports, benchmarks, and
  `data/settings.json` (your per-user overrides). The whole `data/` directory is
  in `.gitignore`, so your settings and history are never committed.
- `goals.md` — your goals (edit freely).
- `config.toml` — generic, checked-in defaults. Setup does **not** edit it; your
  personal choices go into `data/settings.json`, which takes precedence.

## The Gemini API key (BYOK)

Gemini is bring-your-own-key and entirely optional. With no key, analysis runs
locally through Ollama (and, if you have the `gemini` CLI installed and
OAuth-authed, that acts as a fallback).

Set or change the key any time — it's stored in `data/settings.json` (gitignored)
and never printed back:

```sh
.venv/bin/python -m scoregoals config set gemini_api_key <your-key>   # set
.venv/bin/python -m scoregoals config get gemini_api_key              # -> set / not set
.venv/bin/python -m scoregoals config set gemini_api_key ""           # clear
```

You can also set it from the menu bar app: **Settings → Gemini API key (BYOK)**.
A `GEMINI_API_KEY` environment variable, if present, overrides the stored value.

## Troubleshooting

- **Menu bar app says "engine not found — set path in Settings".** The app
  couldn't locate the repo/venv. Open **Settings → Engine location** and set the
  repo directory (the folder containing `scoregoals/cli.py` and `.venv`), or the
  `.venv/bin/scoregoals` binary directly. The app also honors `$SCOREGOALS_BIN`.
- **`scoregoals doctor` shows screenpipe ✗.** Install the desktop app from
  <https://screenpi.pe> and grant Screen Recording + Microphone. Mock mode works
  without it.
- **Re-running setup.** It's idempotent: it reuses the existing venv and never
  overwrites your `goals.md` or `data/settings.json`.
