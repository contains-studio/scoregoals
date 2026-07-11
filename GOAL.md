# dayloop — Definition of Done & Test Plan

## What this is

dayloop is a personal, local-first **cybernetic activity tracker** for macOS:
it captures what actually happened on the Mac (screen OCR / audio / UI text via
[screenpipe](https://github.com/mediar-ai/screenpipe), plus Calendar, GitHub,
and Granola), builds a redacted daily timeline, compares that timeline against
your `goals.md` with deterministic keyword alignment, and closes the loop with
feedback — a morning plan, real-time drift nudges, an end-of-day report with an
alignment score, and a weekly synthesis. An LLM writes the narrative through a
pluggable backend (local **Ollama** or cloud **Gemini**), and a benchmark
harness records cost/latency/quality per backend so the default is picked with
data, not vibes. Everything stays on-device except the redacted digest you
explicitly send to Gemini.

## Status: READY TO TEST (mock + local ollama)

The whole pipeline runs and is testable **right now** on this machine with the
mock timeline and the local Ollama model — no screenpipe, no icalBuddy, no
Gemini API key required. Live sensor capture (Track B) waits only on the
sensor installs + macOS permission grants.

These exact commands pass today (verified 2026-07-11, venv at `.venv`):

```sh
cd /Users/contains/projects/dayloop
.venv/bin/python -m dayloop --help                       # exit 0
.venv/bin/python -m dayloop doctor                       # 5/7 checks (screenpipe + icalBuddy absent)
.venv/bin/python -m dayloop mock --date 2026-07-11       # 7 sessions, 304 active min
.venv/bin/python -m dayloop analyze 2026-07-11 --backend ollama   # real local model call
.venv/bin/python -m dayloop report  2026-07-11 --backend ollama   # renders data/reports/2026-07-11-eod.md
```

**Observed local Ollama analyze latency:** ~3.8s cold, ~2.2s warm
(model `huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16`, 1341 prompt tokens,
$0.00). A live Gemini CLI (OAuth, no key) run of the same day returned in
~13s at ~$0.0008.

**Sample `data/benchmarks/compare.csv` rows** (same mock day, both backends):

```
date,kind,backend,model,tokens_in,tokens_out,cost_usd,latency_s,overall_score,generated_at
2026-07-11,eod,gemini,gemini-2.5-flash,1180,189,0.000826,12.992,75,2026-07-11T16:22:39-07:00
2026-07-11,eod,ollama,huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16,1341,111,0.000000,2.162,75,2026-07-11T16:22:41-07:00
```

Note both backends report the **same** `overall_score` (75): the score and
drift flags are the deterministic `compare/align.py` math, identical across
backends. The LLM only contributes the free-form narrative + suggestions (its
own self-reported score is kept in the report JSON `raw.llm_overall_score`).

## TRACK A — Test right now (no screenpipe needed)

Run these five and eyeball the output:

```sh
cd /Users/contains/projects/dayloop

# 1. Environment checklist
.venv/bin/python -m dayloop doctor
#    expect: ollama ✓, gemini ✓ (CLI OAuth), gh ✓, terminal-notifier ✓,
#            data dirs ✓; screenpipe ✗ + icalBuddy ✗ (not installed yet) -> 5/7

# 2. Write the deterministic mock day
.venv/bin/python -m dayloop mock --date 2026-07-11
#    -> data/timeline/2026-07-11.json  (sessions=7 calendar=3 github=3 meetings=2, 304 active min)

# 3. Analyze with the local model + benchmark it
.venv/bin/python -m dayloop analyze 2026-07-11 --backend ollama
#    -> prints backend/model/latency/cost/score; appends a row to
#       data/benchmarks/compare.csv; writes data/reports/2026-07-11-eod-ollama.json

# 4. Render the end-of-day report
.venv/bin/python -m dayloop report 2026-07-11 --backend ollama
#    -> data/reports/2026-07-11-eod.md  (score 75/100)

# 5. Read it
open data/reports/2026-07-11-eod.md   # or: cat data/reports/2026-07-11-eod.md
```

What to expect (all deterministic for the mock day, 304 active minutes):

- every command exits 0 (a not-yet-built stub would exit 2 with a clean
  one-line message — never a traceback)
- the ollama Report has a non-empty narrative that names concrete goals with
  real minutes/percent, `overall_score` 0-100, `cost_usd` 0.0
- pinned keyword alignment table:

  | Goal | Minutes | % time | Target | On track |
  |------|--------:|-------:|-------:|:--------:|
  | Ship dayloop | 131 | 43.1% | 35% | yes |
  | Deep work / coding | 0 | 0.0% | 50% | no |
  | Investor & partner comms | 94 | 30.9% | 20% | yes |
  | Learning & research | 47 | 15.5% | 10% | yes |
  | Unaligned | 32 | 10.5% | — | yes |

  (each session maps to at most one goal, so the coding sessions score toward
  "Ship dayloop" and "Deep work / coding" shows 0 — that is expected)
- deterministic day score **75/100** and drift flag
  `No time on 'Deep work / coding' (target 50%)`, both consistent with the
  table above and identical whichever backend runs
- graceful degradation (verified): with Ollama down
  (`DAYLOOP_OLLAMA_URL=http://localhost:9 ... report ... --backend ollama`) the
  report still renders from the deterministic summary and exits 0 — it never
  crashes the pipeline

## TRACK B — Go live (after installing the sensors)

1. **Install screenpipe** (`.dmg` from the repo/site, or `npx @screenpipe/dev`)
   and grant macOS **Screen Recording + Microphone + Accessibility** in System
   Settings → Privacy & Security. Start it; `doctor` should then show
   `screenpipe ✓`.
2. **Calendar:** `brew install ical-buddy` and approve **Calendars** access on
   its first run. `doctor` should then show `icalBuddy ✓`.
3. *(optional)* **Gemini metered mode:** `export GEMINI_API_KEY=…` and
   `uv pip install google-genai` for real token/cost metering. Without a key,
   the Gemini **OAuth CLI** path is used automatically (works today, tokens
   estimated, `raw.metered=false`).
4. *(optional)* Pull a larger / vision-capable Ollama model and point at it via
   `DAYLOOP_OLLAMA_MODEL=…` or `config.toml` (the default 4B thinking model is
   text-only and already installed).
5. **Load the schedule:** `chmod +x scripts/install.sh && scripts/install.sh`
   — creates the venv, installs the `dayloop` console script, and loads the
   four launchd user agents. (`scripts/install.sh uninstall` removes them.)
6. Work a normal morning, then run it live:

   ```sh
   dayloop capture $(date +%F)                     # build today's timeline from live sensors
   dayloop analyze $(date +%F) --backend both      # Gemini vs Ollama, benchmarked
   dayloop report  $(date +%F) --backend ollama    # end-of-day markdown
   dayloop plan                                     # morning plan + notification
   dayloop nudge                                    # drift check (notifies only if drifting)
   ```

Launchd schedule installed by `scripts/install.sh` (rendered from
`dayloop/launchd/*.plist`):

| agent               | when          | runs                                              |
|---------------------|---------------|---------------------------------------------------|
| com.dayloop.morning | 07:30 daily   | `dayloop plan`                                    |
| com.dayloop.nudge   | every 20 min  | `dayloop nudge`                                   |
| com.dayloop.eod     | 21:00 daily   | `dayloop capture <today> && report <today> --backend ollama` |
| com.dayloop.weekly  | Sun 20:00     | `dayloop weekly`                                  |

Logs land in `~/Library/Logs/dayloop/`; verify with
`launchctl list | grep com.dayloop`.

Live acceptance criteria:

- **session accuracy:** spot-check 10 sessions against memory; ≥8 have the
  right app, plausible boundaries (±5 min), and a sensible category
- **nudges:** ≤3 per workday; none while screenpipe is down or during a
  session that matches a goal keyword
- **redaction:** put a fake `sk-…` key on screen for a minute; it must appear
  as `[REDACTED:api-key]` (never verbatim) in the stored timeline JSON

## How to compare Gemini vs Ollama

```sh
dayloop analyze 2026-07-11 --backend both
```

Then read the three signals side by side:

- **`data/benchmarks/compare.csv`** — one row per backend per run:
  `cost_usd` (Gemini metered/estimated vs Ollama always 0.0), `latency_s`
  (local is faster and offline; cloud adds network), and `overall_score`
  (the deterministic quality number — identical across backends by design, so
  it is a control, not a differentiator).
- **`data/reports/2026-07-11-eod-gemini.json`** vs
  **`…-eod-ollama.json`** — compare the two `narrative` + `suggestions`
  fields for which model actually writes the more useful review.

Rule of thumb: default to **ollama** (free, private, ~2s) for the nightly job;
reach for **gemini** when you want a sharper narrative and don't mind the
egress + a fraction of a cent.

## Checklist — DONE vs needs Michael

Done (works now, verified 2026-07-11):

- [x] Package imports on bare system python 3.14 (no third-party deps)
- [x] `python3 -m dayloop --help` / `doctor` / `mock` all work
- [x] sources: screenpipe / calendar / github / granola — each degrades to
      `[]` + one-line warning when its tool/key is absent
- [x] aggregate: segment (sessionize + categorize), redact (keys/JWTs/cards/
      SSNs/OTPs), timeline.build (sparse-input safe)
- [x] compare/align: goals.md parser, keyword alignment, **deterministic**
      score + drift flags (now authoritative in every report/CSV; empty day = 0)
- [x] analyze/ollama: real end-to-end Report on the mock day (metered tokens)
- [x] analyze/gemini: SDK path (keyed) + OAuth **CLI one-shot** path
      (`-p` non-interactive — verified live, returns parseable JSON in ~13s)
- [x] analyze/benchmark: per-backend fault isolation; compare.csv appended;
      score column is deterministic + identical across backends
- [x] feedback: eod / morning / weekly / nudge / notify — all render and
      degrade with **no** LLM reachable (eod no longer crashes on a dead backend)
- [x] launchd agents + `scripts/install.sh` (plists render + load)

Needs Michael:

- [ ] Install **screenpipe** + grant Screen Recording / Microphone /
      Accessibility (Track B step 1)
- [ ] `brew install ical-buddy` + grant Calendars (Track B step 2)
- [ ] *(optional)* set `GEMINI_API_KEY` + `uv pip install google-genai` for
      metered Gemini cost numbers
- [ ] *(optional)* pull a bigger/vision Ollama model
- [ ] Edit **`goals.md`** to your real goals/keywords/targets (the four in
      there now are a working example)
- [ ] Run `scripts/install.sh` to load the nightly schedule, then let it run a
      full untouched day

## Privacy

Local-first by default: capture, segmentation, alignment, storage, and the
Ollama analysis all happen **on-device** (sqlite + JSON under `data/`, which is
gitignored). The only egress is the redacted day digest sent to **Gemini**, and
only when you explicitly choose `--backend gemini`/`both`. Secrets and PII
(API keys, JWTs, card numbers with a Luhn check, SSNs, OTPs) are scrubbed by
`aggregate/redact.py` **before** anything is stored or handed to any LLM.

## Hard rules recap (for every implementer)

- Only edit your assigned files; frozen files (models.py, config.py,
  store/db.py, analyze/base.py, cli.py, mockdata.py) are read-only contracts.
- stdlib + requests + python-dateutil only; google-genai / pyobjc are lazy
  optional extras.
- No network/subprocess at import time; degrade gracefully, never crash the
  pipeline because a sensor or key is missing.
