# dayloop — Definition of Done & Test Plan

> STATUS: v1 integrated. All modules implemented and smoke-tested end-to-end
> on 2026-07-11 (mock timeline + a real local ollama analysis run). Track B
> still awaits the sensor installs (screenpipe, icalBuddy).

## What "done" means (v1)

dayloop is done when Michael can leave it running for a full week and get:
morning plans at breakfast, at most a few useful drift nudges a day, an
end-of-day report with an alignment score he mostly agrees with, a weekly
synthesis on Sunday, and a `compare.csv` with enough gemini-vs-ollama rows to
pick a default backend.

## Checklist

Scaffold (this commit):
- [x] Package imports on bare system python 3.14 (no third-party deps installed)
- [x] `python3 -m dayloop --help` works
- [x] `dayloop mock` writes a deterministic timeline JSON + sqlite row
- [x] `dayloop doctor` prints the ✓/✗ environment checklist
- [x] Frozen contracts: models.py, config.py, store/db.py, analyze/base.py,
      cli.py, mockdata.py — implementers read these, never edit them

Modules (each fills its stubs, signatures frozen):
- [x] sources/screenpipe.fetch — real records from the local API; one-line warning + [] when down (verified: `nudge`/`capture` with screenpipe absent)
- [x] sources/calendar.fetch — icalBuddy path; [] + warning when missing (verified: `plan`/`capture` with icalBuddy absent)
- [x] sources/github.fetch — gh CLI events/PR search + local git sweep, deduped by sha/url (verified via `capture 2026-07-10`)
- [x] sources/granola.fetch — public API when keyed; [] + one-line info when no key (verified: no key set)
- [x] aggregate/segment — deterministic sessionization + categorization (90s gap merge, micro-flip folding)
- [x] aggregate/redact — secrets/PII scrubbed before store or LLM (keys, JWTs, cards w/ Luhn, SSNs, OTPs)
- [x] aggregate/timeline.build — full orchestration, sparse-input safe (verified: 0-session day builds cleanly)
- [x] compare/align — goals.md parser, keyword alignment, baseline score, drift flags
- [x] analyze/ollama — end-to-end Report on mock data with the local qwen3 model (real run: parsed JSON, metered tokens)
- [x] analyze/gemini — SDK path (keyed) + gemini CLI OAuth fallback (CLI path present; live cloud call not exercised in smoke test)
- [x] analyze/benchmark — per-backend fault isolation; compare.csv appended (real row written)
- [x] feedback/eod, morning, weekly, nudge, notify — all render/degrade without any LLM
- [x] launchd agents + scripts/install.sh (plists + installer written; `install.sh` load deferred to Track B)

## Track A — TEST NOW (mock + ollama; no screenpipe/icalBuddy needed)

```sh
cd /Users/contains/projects/dayloop
python3 -m dayloop doctor          # expect: ollama ✓, gemini ✓ (CLI), screenpipe ✗, icalBuddy ✗
python3 -m dayloop mock --date 2026-07-11
#   -> data/timeline/2026-07-11.json (7 sessions, ~304 active minutes)
python3 -m dayloop analyze 2026-07-11 --backend ollama
#   -> report JSON + benchmark row; summary line with latency/cost/score
python3 -m dayloop report 2026-07-11 --backend ollama
#   -> data/reports/2026-07-11-eod.md
python3 -m dayloop analyze 2026-07-11 --backend both   # adds the gemini column
```

Pass criteria (Track A) — all verified on 2026-07-11:
- every command exits 0 (stubs exit 2 with a clear "not implemented" line until filled)
- ollama Report has a non-empty narrative, score 0-100, cost 0.0
- expected keyword alignment for the mock day (deterministic, pinned):
  Ship dayloop 131m / 43.1%, Deep work / coding 0m / 0.0% (its coding
  sessions out-score toward Ship dayloop — each session maps to at most one
  goal), Investor & partner comms 94m / 30.9%, Learning & research
  47m / 15.5%, Unaligned 32m / 10.5% (of 304 active minutes)
- narrative quality bar: references at least one concrete goal by name with
  its actual minutes/percent, and names at least one time leak
- reference ollama run on this machine: 1341 prompt + 161 output tokens
  (metered), ~3.6s latency warm, overall_score 78

## Track B — TEST LIVE (after installing sensors)

1. Install screenpipe; grant Screen Recording + Accessibility + Microphone.
2. `brew install ical-buddy` (and approve Calendar access on first run).
3. `python3 -m dayloop doctor` — everything ✓ except optionally the gemini key.
4. Work a normal morning, then `python3 -m dayloop capture $(date +%F)` and
   check the sessions look like what you actually did.
5. `python3 -m dayloop analyze $(date +%F) --backend both`, compare
   `data/benchmarks/compare.csv` rows.
6. `python3 -m dayloop plan` / `nudge` / `report` / `weekly` end-to-end;
   notifications must arrive via terminal-notifier.
7. `scripts/install.sh` to load the launchd schedule; verify a full untouched day.

Live acceptance criteria (Track B):
- session accuracy: spot-check 10 sessions against memory; ≥8 must have the
  right app, plausible boundaries (±5 min), and a sensible category
- nudges: ≤3 per workday; zero nudges while screenpipe is down or during a
  session matching any goal keyword
- launchd schedule (rendered from dayloop/launchd/*.plist by install.sh):
  | agent               | when              | runs                       |
  |---------------------|-------------------|----------------------------|
  | com.dayloop.morning | 07:30 daily       | plan                       |
  | com.dayloop.nudge   | every 20 min      | nudge                      |
  | com.dayloop.eod     | 21:00 daily       | capture <today> && report  |
  | com.dayloop.weekly  | Sunday 20:00      | weekly                     |
- redaction: seed a fake `sk-…` key on screen for a minute; it must appear as
  `[REDACTED:api-key]` (never verbatim) in the stored timeline JSON

## Hard rules recap (for every implementer)

- Only edit your assigned files; frozen files are read-only contracts.
- stdlib + requests + python-dateutil only; google-genai / pyobjc are lazy
  optional extras.
- No network/subprocess at import time; degrade gracefully, never crash the
  pipeline because a sensor or key is missing.
