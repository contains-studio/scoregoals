# dayloop

A personal, local-first **cybernetic activity tracker** for macOS. It watches
what actually happens on the Mac (screen OCR, accessibility text, meeting
audio via [screenpipe](https://github.com/mediar-ai/screenpipe), plus
Calendar, GitHub, and Granola), builds a daily timeline, compares it against
`goals.md`, and closes the loop with feedback: a morning plan, real-time
drift nudges, an end-of-day report with an alignment score, and a weekly
synthesis. Analysis runs through pluggable LLM backends — Google Gemini or
local Ollama (qwen3) — with a benchmark harness recording cost/latency/quality
so the winner is picked with data, not vibes.

Everything stays on-device except what you explicitly send to Gemini.

## The loop

```
      ┌───────────────────────── SENSORS ─────────────────────────┐
      │ screenpipe (OCR · audio · UI)   Calendar   GitHub  Granola │
      │              dayloop/sources/*.fetch()                     │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼
      ┌──────────────────────── ESTIMATOR ────────────────────────┐
      │ aggregate/: records → segment → Sessions → redact          │
      │            → DayTimeline (data/timeline/<date>.json)       │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼                    goals.md
      ┌──────────────────────── COMPARATOR ───────────────────◄───┐
      │ compare/align (keyword baseline) + analyze/* (LLM):        │
      │ gemini | ollama, benchmarked in data/benchmarks/compare.csv│
      └───────────────────────────┬────────────────────────────────┘
                                  ▼
      ┌──────────────────────── ACTUATORS ────────────────────────┐
      │ feedback/: morning plan · drift nudge · eod report ·       │
      │ weekly synthesis  → macOS notifications + markdown reports │
      └───────────────────────────┬────────────────────────────────┘
                                  ▼
                     Michael adjusts tomorrow ──► (loop)
```

## Quick start (works today, no screenpipe needed)

```sh
cd /Users/contains/projects/dayloop
python3 -m dayloop doctor                    # environment checklist
python3 -m dayloop mock --date 2026-07-11    # deterministic test timeline
python3 -m dayloop analyze 2026-07-11 --backend ollama   # local model + benchmark row
python3 -m dayloop report  2026-07-11 --backend ollama   # -> data/reports/2026-07-11-eod.md
```

All commands: `capture`, `analyze`, `report`, `plan`, `nudge`, `weekly`,
`mock`, `doctor` — see `python3 -m dayloop --help`.

## Layout

- `dayloop/models.py` — frozen data contracts (ActivityRecord, Session,
  DayTimeline, Goal, GoalAlignment, Report)
- `dayloop/sources/` — sensors, `dayloop/aggregate/` — estimator,
  `dayloop/compare/` + `dayloop/analyze/` — comparator,
  `dayloop/feedback/` — actuators
- `config.toml` — defaults that work out of the box; `goals.md` — your goals
- `data/` — sqlite + JSON timelines/reports/benchmarks (gitignored)

**Definition of done and the full test plan live in [GOAL.md](GOAL.md).**
