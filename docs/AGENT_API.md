# ScoreGoals agent-facing CLI API

This is the single document a **checking agent** (an LLM that periodically asks
"how is Michael doing?") is given. Every command below is a **read-only** JSON
reader: the agent shells out to `scoregoals <cmd> --json`, parses stdout, and
reaches every piece of stored ScoreGoals data without ever mutating it.

Field shapes reuse the definitions in **[STATUS_SCHEMA.md](STATUS_SCHEMA.md)** —
where a section says "same shape as `status.score`" etc., that document is the
authority.

## Contract (read this first)

- **stdout is PURE JSON.** Every command prints exactly one JSON value to
  stdout. Nothing else is ever written to stdout.
- **stderr is for humans.** Warnings, the timeline self-heal note, screenpipe
  reachability messages — all go to **stderr**. An agent can discard stderr, or
  log it, but must never try to parse it as part of the result.
- **Exit codes:** `0` on success (this includes "no data for that date" — an
  empty/`exists:false` result is a normal success, not an error). `2` on a
  **user error** — an unknown flag value, a malformed argument, an unknown
  config key. There is no other exit code in normal operation.
- **`--json` is accepted everywhere and is the default.** These commands emit
  JSON with or without the flag; pass it for clarity in scripts.
- **Invocation:** `scoregoals <cmd>` = the installed `.venv/bin/scoregoals`
  console script. Equivalent: `.venv/bin/python -m scoregoals <cmd>`.
- **Redaction:** any text that originated from screen capture (the `search`
  command) is passed through the same secret/PII redaction as stored data —
  API keys, tokens, passwords, cards, SSNs, JWTs become `[REDACTED:<tag>]`
  before they leave the process.

## Table of contents

1. [`timeline`](#timeline) — the full stored day
2. [`search`](#search) — screenpipe full-text search (redacted)
3. [`labels`](#labels) — the corrections log
4. [`rules`](#rules) — learned rules (active + retired)
5. [`bench`](#bench) — backend benchmark rows
6. [`reports`](#reports) — stored EOD/weekly/morning reports
7. [`trend`](#trend) — per-day score/minutes/goals history
8. [`status` / `review`](#status--review-existing) — the live snapshot & correction queue (existing)
9. [Recipes](#recipes) — how the agent answers common questions

---

## `timeline`

**Purpose:** the complete stored `DayTimeline` for one day — every session
(with its stable `id`), plus calendar/github/meeting records and the day stats.
This is the raw "what happened" record behind the score.

**Invocation:** `scoregoals timeline [--date YYYY-MM-DD] --json`
(default date: today). Loaded through the store heal path (`store.load_timeline`),
so it always reflects the reconciled DB↔file copy.

**Output shape** (top level):

| field | type | notes |
|------|------|------|
| `date` | string `YYYY-MM-DD` | the day |
| `exists` | bool | `false` when nothing was captured for the date (all arrays empty) |
| `sessions` | array of object | contiguous activity blocks (see below) |
| `calendar` | array of object | `ActivityRecord`s (source `calendar`) |
| `github` | array of object | `ActivityRecord`s (source `github`) |
| `meetings` | array of object | `ActivityRecord`s (source `granola`/audio) |
| `stats` | object | `total_active_minutes`, `per_app_minutes`, `per_category_minutes`, `counts` |
| `generated_at` | string (ISO) | when the timeline was built |

`sessions[]` is a `Session` (`scoregoals/models.py`): `id` (12-hex stable id —
the handle `label`/`review` use), `start`, `end`, `app`, `title`, `project`,
`topic`, `category`, `summary`, `minutes`, `text_excerpt`, `record_count`.

**Missing date** (exit 0):

```json
{ "date": "1999-01-01", "exists": false, "sessions": [], "calendar": [], "github": [], "meetings": [], "stats": {} }
```

**One real session** (from `timeline --date 2026-07-12`):

```json
{
  "id": "3251edde5d48",
  "start": "2026-07-12T07:31:25",
  "end": "2026-07-12T07:35:15",
  "app": "Claude",
  "title": null,
  "category": "other",
  "minutes": 3.8,
  "text_excerpt": "Claude FileEdit View Window Helpdelightful-raman /...",
  "record_count": 34
}
```

That real day (2026-07-12) has 9 sessions; session `6c66c14da1ef`
(`UserNotificationCenter`) carries a `not_work` correction — see `review`/`labels`.

---

## `search`

**Purpose:** free-text search over the raw screenpipe capture (screen OCR, audio
transcripts, accessibility/UI text). The one place the agent can answer "what
was actually on screen" for a moment in time. Proxies screenpipe's `GET /search`
with the Bearer token auto-resolved (config → `screenpipe auth token`).

**Invocation:**
`scoregoals search "<query>" [--from ISO] [--to ISO] [--limit N] [--type ocr|audio|all] --json`

- `--from` / `--to`: ISO-8601 time bounds (optional; unbounded when omitted).
- `--limit N`: max rows returned (default 20).
- `--type`: `ocr`, `audio`, or `all` (default `all` = ocr + audio + ui).

**CRITICAL — redaction:** every `text`, `title`, and `speaker` field is passed
through `aggregate.redact.redact_text` before output. The CLI never leaks a
secret that redaction would have caught.

**Output shape:**

| field | type | notes |
|------|------|------|
| `query` | string | the query echoed back |
| `from` / `to` | string \| null | the requested bounds |
| `type` | string | `ocr`\|`audio`\|`all` |
| `limit` | int | requested cap |
| `count` | int | number of `results` |
| `results` | array of object | redacted hits (see below) |
| `error` | string | present only on failure; `"screenpipe unreachable"` when the sensor is down |

`results[]`: `type` (`ocr`\|`audio`\|`ui`), `timestamp` (ISO), `end`
(ISO\|null), `app`, `title` (redacted), `text` (redacted), `frame_id`
(int\|null), `speaker` (redacted\|null).

**Unreachable screenpipe** (exit 0, empty results):

```json
{ "query": "scoregoals", "from": null, "to": null, "type": "all", "limit": 3,
  "count": 0, "results": [], "error": "screenpipe unreachable" }
```

**Real redacted OCR row** (`search "scoregoals" --limit 1`, from this machine):

```json
{
  "query": "scoregoals", "type": "all", "limit": 1, "count": 1,
  "results": [
    {
      "type": "ocr",
      "timestamp": "2026-07-11T21:51:05.350799-07:00",
      "end": null,
      "app": "Claude",
      "title": "",
      "text": "account yeu care aboutgo humansin-the-toop wit& dynadotDomai",
      "frame_id": 209,
      "speaker": null
    }
  ]
}
```

(The OCR text is verbatim screen capture — noisy, and already redaction-passed.)

---

## `labels`

**Purpose:** the append-only corrections log (`data/labels.jsonl`) — every time
Michael reassigned a session to a goal, marked it off-track, or marked it
not-work. This is what "what did he correct recently" reads, and the raw
material `rules` (learning) is mined from.

**Invocation:** `scoregoals labels [--date YYYY-MM-DD | --days N] --json`

- No filter: every label.
- `--date`: only labels whose session day equals that date.
- `--days N`: labels in the N-day window ending today.

**Output shape:**

| field | type | notes |
|------|------|------|
| `from` / `to` | string \| null | window bounds (null when unfiltered) |
| `count` | int | number of `labels` |
| `labels` | array of object | corrections, **newest first** |

`labels[]` is the stored line verbatim: `ts`, `session_id`, `date`,
`fingerprint` (`app`, `title_tokens[]`, `text_keywords[]`, `hour_bucket`),
`verdict` (a goal id, `"off_track"`, or `"not_work"`), `source`
(`"user"`\|`"implicit"`). See STATUS_SCHEMA's "Data model additions".

**Real output** (this machine — the one correction filed on 2026-07-12):

```json
{
  "from": null, "to": null, "count": 1,
  "labels": [
    {
      "ts": "2026-07-12T10:35:15-07:00",
      "session_id": "6c66c14da1ef",
      "date": "2026-07-12",
      "fingerprint": { "app": "UserNotificationCenter", "title_tokens": [],
        "text_keywords": ["dayloop", "screenpipe", "recorder", "requesting", "bypass", "system", "private", "picker"],
        "hour_bucket": 0 },
      "verdict": "not_work",
      "source": "user"
    }
  ]
}
```

---

## `rules`

**Purpose:** the learned deterministic rules (`data/learned_rules.json`) that
apply before any keyword/LLM guess — plus the retired ones and why they retired.
Answers "what has ScoreGoals learned to auto-classify without asking?".

**Invocation:** `scoregoals rules --json`

**Output shape:**

| field | type | notes |
|------|------|------|
| `active_count` | int | number of active rules |
| `retired_count` | int | number of retired rules |
| `active` | array of object | applied rules (see below) |
| `retired` | array of object | retired rules; each adds `reason` (`contradicted`\|`archived-goal`\|`app-only-too-broad`) and `retired_at` |

Each rule: `rule` (`{app, title_token, verdict}`), `created_from` (the labels
that minted it), `created_from_count` (int — convenience: `len(created_from)`),
`created_at`.

**Real output** (this machine — no rules promoted yet):

```json
{ "active_count": 0, "retired_count": 0, "active": [], "retired": [] }
```

---

## `bench`

**Purpose:** the backend benchmark log (`data/benchmarks/compare.csv`) — one row
per analysis backend run, with cost / latency / token counts / the
deterministic day score. Answers "which backend, how fast, how much".

**Invocation:** `scoregoals bench [--days N] --json` (`--days` filters to the
N-day window ending today; omitted = all rows).

**Output shape:**

| field | type | notes |
|------|------|------|
| `from` / `to` | string \| null | window bounds (null when unfiltered) |
| `count` | int | number of `rows` |
| `rows` | array of object | benchmark rows, **newest first** |

`rows[]`: `date`, `kind`, `backend`, `model`, `tokens_in` (int), `tokens_out`
(int), `cost_usd` (float), `latency_s` (float), `overall_score` (int —
**`-1` is the documented sentinel** for an insufficient-data day, not a real 0),
`generated_at`.

**Real row** (`bench --days 3`, this machine):

```json
{
  "from": "2026-07-10", "to": "2026-07-12", "count": 19,
  "rows": [
    { "date": "2026-07-11", "kind": "eod", "backend": "ollama",
      "model": "huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16",
      "tokens_in": 2651, "tokens_out": 132, "cost_usd": 0.0, "latency_s": 3.8,
      "overall_score": 66, "generated_at": "2026-07-11T23:01:41-07:00" }
  ]
}
```

---

## `reports`

**Purpose:** the stored end-of-day / weekly / morning reports — the LLM
narrative, the deterministic score, drift flags, suggestions, per-goal
alignments, and the markdown path/text.

**Invocations:**

- `scoregoals reports list --json` — every available report (bare `scoregoals
  reports` also lists).
- `scoregoals reports show <date> [--kind eod|weekly|morning] --json` — one
  report (default kind `eod`).

**`reports list` shape:** `{ "count": int, "reports": [ ... ] }`, newest first.
Each entry: `date`, `kind`, `backend` (null for markdown-only weekly/morning),
`model`, `overall_score` (nullable), `scored` (nullable), `generated_at`,
`has_markdown` (bool), `md_path` (string\|null).

**`reports show` shape:** `{ date, kind, exists }` plus, when `exists` is true:
`backend`, `model`, `generated_at`, `overall_score`, `scored`, `narrative`,
`drift_flags[]`, `suggestions[]`, `alignments[]` (each a `GoalAlignment`:
`goal_id`, `goal_name`, `minutes`, `pct_time`, `target_pct`, `on_track`),
`tokens_in`, `tokens_out`, `cost_usd`, `latency_s`, `available_backends[]`,
`md_path`, `markdown` (full markdown text, or null). A date with no stored report
returns `{ "date": D, "kind": K, "exists": false }` (exit 0).

**Real output** (`reports show 2026-07-11 --kind eod`, trimmed):

```json
{
  "date": "2026-07-11", "kind": "eod", "exists": true,
  "backend": "ollama", "model": "huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16",
  "generated_at": "2026-07-11T23:46:00-07:00",
  "overall_score": 66, "scored": true,
  "narrative": "Michael spent most time on investor comms (27.4%) and unaligned tasks ...",
  "drift_flags": [ "'Deep work / coding' at 10% vs target 50%", "'Learning & research' at 4% vs target 10%" ],
  "suggestions": [ "Block 90-min coding sessions for dayloop v1", "Prioritize screenpipe docs over unaligned tasks" ],
  "alignments": [ { "goal_id": "ship-scoregoals", "goal_name": "Ship ScoreGoals", "minutes": 13.6, "pct_time": 41.5, "target_pct": 35.0, "on_track": true } ],
  "tokens_in": 2651, "tokens_out": 148, "cost_usd": 0.0, "latency_s": 4.53,
  "available_backends": [ "gemini", "ollama" ],
  "md_path": "/Users/.../data/reports/2026-07-11-eod.md",
  "markdown": "# ScoreGoals — 2026-07-11 ..."
}
```

---

## `trend`

**Purpose:** the day-over-day arc — score, active minutes, per-goal split, and
correction count for each of the trailing N days. The one call that answers "is
he trending up or down" and "is today on track vs. his baseline". Reuses
`align.score_day` per day, so each number matches `status`/`review`/the EOD
report exactly.

**Invocation:** `scoregoals trend [--days N] --json` (default `--days 14`).

**Output shape:**

| field | type | notes |
|------|------|------|
| `days` | int | window size |
| `from` / `to` | string `YYYY-MM-DD` | window bounds (oldest, newest) |
| `trend` | array of object | one per day, **oldest first** |

`trend[]`: `date`, `score` (int **\| null** when unscored — a day below 30 active
minutes, or with no timeline), `scored` (bool), `active_minutes` (number),
`goals` (array of `{goal_id, goal_name, minutes, pct_time, target_pct}`,
including the trailing `unaligned` pseudo-goal; empty on an unscored day),
`corrections` (int — user labels filed for that day).

**Real output** (`trend --days 3`, this machine — abbreviated goals):

```json
{
  "days": 3, "from": "2026-07-10", "to": "2026-07-12",
  "trend": [
    { "date": "2026-07-10", "score": null, "scored": false, "active_minutes": 0.0, "goals": [], "corrections": 0 },
    { "date": "2026-07-11", "score": 66, "scored": true, "active_minutes": 32.8,
      "goals": [ { "goal_id": "ship-scoregoals", "minutes": 13.6, "pct_time": 41.5, "target_pct": 35.0 }, "…" ],
      "corrections": 0 },
    { "date": "2026-07-12", "score": 27, "scored": true, "active_minutes": 177.1,
      "goals": [ { "goal_id": "deep-work-coding", "minutes": 163.7, "pct_time": 92.4, "target_pct": 50.0 }, "…" ],
      "corrections": 1 }
  ]
}
```

---

## `status` / `review` (existing)

These predate this API but are core to it, so they're summarized here; the full
field tables live in [STATUS_SCHEMA.md](STATUS_SCHEMA.md).

- **`scoregoals status --json`** — one live snapshot: `now` (current activity),
  `score` (`overall` nullable, `scored`, `on_track`, `active_minutes`), per-goal
  `goals[]`, `drift_flags[]`, `review.needs_review`, `corrections_this_week`,
  `learning`, `intentions`, `focus`, `next_event`, `week` (7-day scores +
  sparkline), and `health` (services, cost, disk, toggles). **Never crashes,
  always exit 0.** This is the agent's default "right now" call.
- **`scoregoals review [--date D] --json`** — every session for the day resolved
  to a verdict, uncertain-first, with `needs_review` flags and the day `score`.
  The correction queue.

---

## Recipes

Concrete "the agent wants to know X" → "call this".

### "What is Michael doing right now?"

`scoregoals status --json` → read `now` (`app`, `title`, `goal_name`,
`on_task`, `minutes`, `source`). `source: "screenpipe"` = live; `"idle"` =
sensor up but nothing recent; `"unknown"` = sensor down. Cross-check
`focus` (is he in a declared focus block) and `next_event` (what's coming up).

### "What did he do between 2 and 4pm?"

Two complementary calls:

1. `scoregoals timeline --date 2026-07-12 --json` → filter `sessions[]` to
   `start`/`end` inside 14:00–16:00; each session gives app, title, minutes,
   category, and a `text_excerpt`.
2. For the actual on-screen content, `scoregoals search "<topic>" --from
   2026-07-12T14:00:00 --to 2026-07-12T16:00:00 --type all --json` → redacted
   OCR/audio rows with timestamps.

### "Is he on track today?"

`scoregoals status --json` → `score.scored` gates everything (false = under 30
active minutes, `overall` is null → say "not enough data yet", don't infer
off-track). When scored, `score.overall` (0–100) and `score.on_track`
(≥60) are the headline; `goals[]` shows which targets are met; `drift_flags[]`
are the specific misses. For the trajectory, `scoregoals trend --days 7 --json`
→ compare today's `score` to the recent days.

### "What did he correct recently?"

`scoregoals labels --days 7 --json` → each entry's `verdict` and `fingerprint.app`
tell you what he reclassified and how (a goal id, `off_track`, or `not_work`).
`scoregoals rules --json` shows which of those corrections have hardened into
automatic rules (`active`) — a shrinking correction count + growing rules = the
system is learning.

### "Is the sensor stack healthy?"

`scoregoals doctor` (human checklist, **not** JSON — read the ✓/✗ lines from
**stdout**; it always exits 0). Each line is `✓`/`✗ <name> <detail>`:
`screenpipe`, `recorder app`, `frames`, `audio`, `a11y text`, `ollama`,
`gemini`, `icalBuddy`, `terminal-notifier`, `gh`, `data dirs`, then
`N/M checks passed.` For a machine-readable subset, `status --json → health`
gives `screenpipe.ok`, `backend.ollama_ok`, `backend.gemini`, `last_capture`,
`gemini_cost_today_usd`, `data_dir_mb`. A stale `frames` line or `screenpipe`
`✗` means capture isn't flowing — the agent should caveat any "right now" answer
accordingly.

### "Which analysis backend should he use / what has it cost?"

`scoregoals bench --days 14 --json` → compare `backend`, `latency_s`, `cost_usd`
across rows (same `overall_score` per day by design — backends differ only in
narrative/cost/latency). `status.health.gemini_cost_today_usd` is today's spend.
