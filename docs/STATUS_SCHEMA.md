# dayloop machine-readable CLI schema (`schema_version = 1`)

This document is the contract between the dayloop Python engine and the macOS
menu bar app. The Swift app decodes against the shapes below, so field names,
types, and nullability are **stable** — additive changes only; renames/removals
bump `schema_version`.

All three commands:

- Emit **exactly one** JSON object to **stdout** (pretty-printed, UTF-8,
  `ensure_ascii=false` so unicode glyphs like the sparkline are literal).
- Send diagnostics/warnings to **stderr**, never stdout.
- Are produced by `dayloop/status.py`, `dayloop/intentions.py`,
  `dayloop/config.py`.

Timestamps are ISO-8601 strings with a UTC offset (e.g.
`"2026-07-11T18:51:04-07:00"`), except a few date-only calendar/session strings
that may be naive local (`"2026-07-11T18:30:00"`); the app should parse both.

Run the engine from the repo root as:
`.venv/bin/python -m dayloop <command>`

---

## `dayloop status --json`

One live snapshot. **Never crashes**: every section is guarded; failures append
a string to `warnings` (and a line to stderr) and fall back to nulls/zeros. The
command **always exits 0** with valid JSON, even when screenpipe/ollama/gemini
are down or no timeline exists yet. `--json` is accepted but output is JSON
regardless; `--date YYYY-MM-DD` overrides the default (today).

### Top level

| field | type | notes |
|------|------|------|
| `schema_version` | int | always `1` |
| `date` | string `YYYY-MM-DD` | the day summarized (default: today) |
| `generated_at` | string (ISO) | when this snapshot was produced |
| `now` | object | current activity (see below) |
| `score` | object | day score (see below) |
| `goals` | array of object | per-goal alignment incl. an `unaligned` entry |
| `drift_flags` | array of string | deterministic drift warnings (may be empty) |
| `intentions` | object | today's intentions block (same shape as `today --json`) |
| `focus` | object | focus block (same shape as `focus --json`) |
| `next_event` | object \| null | next upcoming calendar event, or null |
| `week` | object | last-7-day scores + sparkline |
| `health` | object | services, costs, disk, toggles |
| `warnings` | array of string | non-fatal degradations this run (usually empty) |

### `now` — current activity

Derived from a ~10-minute screenpipe window; the dominant recent session is
mapped to a goal with the same keyword logic as the day score.

| field | type | notes |
|------|------|------|
| `app` | string \| null | dominant app in the window; null when idle/unknown |
| `title` | string \| null | window title |
| `goal_id` | string \| null | matched goal id, or null if none matched |
| `goal_name` | string \| null | matched goal name |
| `on_task` | bool | `true` iff `goal_id` is non-null (mapped to a real goal) |
| `category` | string \| null | coding\|comms\|meeting\|browsing\|research\|design\|other |
| `since` | string (ISO) \| null | when the current activity started |
| `minutes` | number | minutes on the current activity (0 when idle/unknown) |
| `source` | string | `"screenpipe"` (live data) \| `"idle"` (reachable, nothing recent) \| `"unknown"` (screenpipe unreachable) |

When `source` is `"idle"` or `"unknown"`, all other `now` fields are
null/0/false.

### `score` — day score

| field | type | notes |
|------|------|------|
| `overall` | int 0–100 | deterministic `compare.align.overall_score` (matches the EOD report) |
| `on_track` | bool | `overall >= 60` |
| `active_minutes` | number | `timeline.stats.total_active_minutes` |

### `goals[]` — per-goal alignment

One entry per goal in `goals.md` order, **plus a trailing `unaligned` entry**.

| field | type | notes |
|------|------|------|
| `goal_id` | string | slug id (`unaligned` for the pseudo-goal) |
| `goal_name` | string | display name (`Unaligned` for the pseudo-goal) |
| `minutes` | number | minutes attributed to this goal today |
| `pct_time` | number | % of active time (0 when the day is empty) |
| `target_pct` | number \| null | from `goals.md`; null when untargeted (incl. `unaligned`) |
| `on_track` | bool | `target_pct is null` or `pct_time >= 0.7 * target_pct` |

### `intentions`

Exactly the object returned by `today --json` (see that section).

### `focus`

Exactly the object returned by `focus --json` (see that section).

### `next_event` — object | null

`null` when there is no upcoming event today (icalBuddy missing, nothing
scheduled, or the day's events are all in the past). Otherwise:

| field | type | notes |
|------|------|------|
| `title` | string | event title |
| `start` | string (ISO) | event start (may be naive local) |
| `minutes_until` | number | whole minutes from now until `start` |

### `week`

| field | type | notes |
|------|------|------|
| `scores` | array of (int \| null), length 7 | oldest→newest, `date-6 … date`; null on days with no timeline |
| `on_track_days` | int | count of days with a non-null score `>= 60` |
| `sparkline` | string | 7 chars; `▁▂▃▄▅▆▇█` ramp per score, `·` for a null day |

### `health`

| field | type | notes |
|------|------|------|
| `screenpipe` | object | `{ "ok": bool, "detail": string }` |
| `backend` | object | see below |
| `last_capture` | string (ISO) \| null | `generated_at` of the newest timeline file |
| `gemini_cost_today_usd` | number | sum of today's gemini `cost_usd` rows in `benchmarks/compare.csv` |
| `data_dir_mb` | number | total size of `data/` in MB (2 dp) |
| `capture_paused` | bool | effective `capture_paused` setting |
| `nudges_enabled` | bool | effective `nudges_enabled` setting |

`health.backend`:

| field | type | notes |
|------|------|------|
| `default` | string | effective `default_backend`: `"ollama"` \| `"gemini"` \| `"both"` |
| `ollama_ok` | bool | ollama `/api/tags` reachable |
| `ollama_latency_s` | number \| null | probe latency in seconds; null when unreachable |
| `gemini` | string | `"key"` (GEMINI_API_KEY set) \| `"cli"` (gemini CLI on PATH) \| `"off"` |

### Example

```json
{
  "schema_version": 1,
  "date": "2026-07-11",
  "generated_at": "2026-07-11T18:51:04-07:00",
  "now": {
    "app": null, "title": null, "goal_id": null, "goal_name": null,
    "on_task": false, "category": null, "since": null, "minutes": 0.0,
    "source": "unknown"
  },
  "score": { "overall": 75, "on_track": true, "active_minutes": 304.0 },
  "goals": [
    { "goal_id": "ship-dayloop", "goal_name": "Ship dayloop", "minutes": 131.0, "pct_time": 43.1, "target_pct": 35.0, "on_track": true },
    { "goal_id": "deep-work-coding", "goal_name": "Deep work / coding", "minutes": 0.0, "pct_time": 0.0, "target_pct": 50.0, "on_track": false },
    { "goal_id": "investor-partner-comms", "goal_name": "Investor & partner comms", "minutes": 94.0, "pct_time": 30.9, "target_pct": 20.0, "on_track": true },
    { "goal_id": "learning-research", "goal_name": "Learning & research", "minutes": 47.0, "pct_time": 15.5, "target_pct": 10.0, "on_track": true },
    { "goal_id": "unaligned", "goal_name": "Unaligned", "minutes": 32.0, "pct_time": 10.5, "target_pct": null, "on_track": true }
  ],
  "drift_flags": [ "No time on 'Deep work / coding' (target 50%)" ],
  "intentions": { "date": "2026-07-11", "set_at": null, "items": [], "history_summary": { "days": 7, "completion_rate": 0.0 } },
  "focus": { "active": false, "goal_id": null, "goal_name": null, "started_at": null, "until": null },
  "next_event": null,
  "week": { "scores": [null, null, null, null, 75, 0, 75], "on_track_days": 2, "sparkline": "····▇▁▇" },
  "health": {
    "screenpipe": { "ok": false, "detail": "not reachable at http://localhost:3030 (mock mode still works)" },
    "backend": { "default": "ollama", "ollama_ok": true, "ollama_latency_s": 0.001, "gemini": "cli" },
    "last_capture": "2026-07-11T23:59:00",
    "gemini_cost_today_usd": 0.00161,
    "data_dir_mb": 0.15,
    "capture_paused": false,
    "nudges_enabled": true
  },
  "warnings": []
}
```

---

## `dayloop today --json`

The daily intentions block, enriched with time attributed to each intention
from today's aligned sessions. Stored at `data/intentions/<date>.json`; the
`--json` block adds `goal_name`, `attributed_minutes`, and `apps`. This is the
**identical object** embedded at `status.intentions`.

| field | type | notes |
|------|------|------|
| `date` | string `YYYY-MM-DD` | the day |
| `set_at` | string (ISO) \| null | when intentions were established; null if none |
| `items` | array of object | 0–3 items (see below) |
| `history_summary` | object | cheap 7-day completion rollup (see below) |

`items[]`:

| field | type | notes |
|------|------|------|
| `id` | string | short stable id (8 hex chars) |
| `text` | string | the intention text |
| `goal_id` | string \| null | auto-linked (or explicit) goal id; null if no keyword match |
| `goal_name` | string \| null | resolved from `goal_id`; null if unmatched/removed |
| `done` | bool | completion flag |
| `attributed_minutes` | number | minutes today's sessions attributed to `goal_id` (0 when `goal_id` is null); when several intentions share one `goal_id`, that goal's minutes are split **evenly** across them, so their sum equals the goal's real minutes rather than double-counting |
| `apps` | array of string | distinct apps that earned that time (empty when unmatched) |
| `carried_from` | string `YYYY-MM-DD` \| null | the day this item was carried over from (yesterday's undone work, seeded by the morning plan); null for items set today |

`history_summary`:

| field | type | notes |
|------|------|------|
| `days` | int | window size (default 7) |
| `completion_rate` | number 0–1 | done items ÷ total items over the last `days` (0.0 when the window has no items); computed cheaply from the intention files only |

Related write commands (human-readable stdout, not JSON): `today set "a|b|c"`
(replace up to 3, auto-link each), `today add "text" [--goal ID]`,
`today toggle <id-or-1based-index>`, `today clear [--keep-history]` (removes only
today's items — past days' files are always kept). Bare `today` pretty-prints.
Yesterday's UNDONE items are carried over into today's plan (see `carried_from`).

### Example

```json
{
  "date": "2026-07-11",
  "set_at": "2026-07-11T18:51:33-07:00",
  "items": [
    { "id": "97e0e320", "text": "Finish menu bar app", "goal_id": null, "goal_name": null, "done": true, "attributed_minutes": 0.0, "apps": [], "carried_from": null },
    { "id": "8c4c46a3", "text": "Investor follow-ups", "goal_id": "investor-partner-comms", "goal_name": "Investor & partner comms", "done": false, "attributed_minutes": 94.0, "apps": ["Mail", "zoom.us", "Slack"], "carried_from": "2026-07-10" },
    { "id": "9a4b3739", "text": "Read screenpipe docs", "goal_id": "ship-dayloop", "goal_name": "Ship dayloop", "done": false, "attributed_minutes": 131.0, "apps": ["Code", "Google Chrome"], "carried_from": null }
  ],
  "history_summary": { "days": 7, "completion_rate": 0.6 }
}
```

---

## `dayloop today history [--days N] [--json]`

Past intentions plus a completion rate, for the last `days` (default 7) ending
today, **newest day first**. `--json` emits the object below; without it, a
human-readable list is printed. Read-only — it never mutates any file. Clearing
today (`today clear`) never deletes past days' files, so history is the archive.

| field | type | notes |
|------|------|------|
| `days` | int | window size requested |
| `end_date` | string `YYYY-MM-DD` | most recent day in the window (today by default) |
| `items_total` | int | total intentions across the window |
| `items_done` | int | completed intentions across the window |
| `completion_rate` | number 0–1 | `items_done / items_total` (0.0 when empty) |
| `days_list` | array of object | one entry per day, newest first (see below) |

`days_list[]`:

| field | type | notes |
|------|------|------|
| `date` | string `YYYY-MM-DD` | the day |
| `set_at` | string (ISO) \| null | when that day's intentions were set |
| `n_done` | int | completed items that day |
| `n_total` | int | total items that day |
| `items` | array of object | `{ id, text, done, attributed_minutes, goal_name, carried_from }` |

### Example

```json
{
  "days": 7,
  "end_date": "2026-07-11",
  "items_total": 12,
  "items_done": 8,
  "completion_rate": 0.667,
  "days_list": [
    { "date": "2026-07-11", "set_at": "2026-07-11T09:00:00-07:00", "n_done": 1, "n_total": 3,
      "items": [ { "id": "8c4c46a3", "text": "Investor follow-ups", "done": false, "attributed_minutes": 94.0, "goal_name": "Investor & partner comms", "carried_from": "2026-07-10" } ] }
  ]
}
```

---

## `dayloop focus --json`

The single focus-block slot, stored at `data/focus.json`. A block with an
`until` in the past auto-expires (reads as `active: false`). This is the
**identical object** embedded at `status.focus`.

| field | type | notes |
|------|------|------|
| `active` | bool | whether a focus block is currently active |
| `goal_id` | string \| null | focus goal id (resolved from id/name/slug) |
| `goal_name` | string \| null | focus goal display name |
| `started_at` | string (ISO) \| null | when the block started |
| `until` | string (ISO) \| null | auto-expire deadline; null for open-ended |

Write commands: `focus start <goal-id-or-name> [--minutes N]`, `focus stop`.
While a block is active **and** recent activity matches the focus goal, nudges
are suppressed.

### Example

```json
{
  "active": true,
  "goal_id": "ship-dayloop",
  "goal_name": "Ship dayloop",
  "started_at": "2026-07-11T18:51:44-07:00",
  "until": "2026-07-11T19:51:44-07:00"
}
```

---

## `dayloop config --json`

The **effective** app-mutable settings — the merge of
`DEFAULTS < config.toml < data/settings.json < env`. The app writes these via
`config set <key> <value>` (persisted to the JSON overlay `data/settings.json`,
which never touches `config.toml`) and reads a single value via
`config get <key>`.

| field | type | allowed values / notes |
|------|------|------|
| `default_backend` | string | `"ollama"` \| `"gemini"` \| `"both"` |
| `nudges_enabled` | bool | `nudge` honors this |
| `capture_paused` | bool | `capture` skips when true (existing data untouched) |
| `refresh_seconds` | int | app poll cadence (advisory) |
| `ollama_url` | string | ollama base URL |
| `gemini_model` | string | gemini model id |

Environment overrides (highest precedence): `DAYLOOP_DEFAULT_BACKEND`,
`DAYLOOP_NUDGES_ENABLED`, `DAYLOOP_CAPTURE_PAUSED`, `DAYLOOP_REFRESH_SECONDS`,
`DAYLOOP_OLLAMA_URL`, `DAYLOOP_GEMINI_MODEL`.

### Example

```json
{
  "default_backend": "ollama",
  "nudges_enabled": true,
  "capture_paused": false,
  "refresh_seconds": 30,
  "ollama_url": "http://localhost:11434",
  "gemini_model": "gemini-3.5-flash"
}
```

---

## `dayloop goals --json`

The `goals.md` editing surface used by the menu bar Goals editor: the file path,
its verbatim text, and the parsed goals. The Swift app loads `raw` into a
`TextEditor` and writes edits back via `goals write` (below).

| field | type | notes |
|------|------|------|
| `path` | string | absolute path to `goals.md` |
| `raw` | string | the file's verbatim UTF-8 text (`""` if unreadable) |
| `goals` | array of object | parsed goals in `goals.md` order (may be empty); **includes archived goals** |

`goals[]`:

| field | type | notes |
|------|------|------|
| `id` | string | slug of the name (`ship-dayloop`), duplicate slugs get `-2`, `-3`, … |
| `name` | string | display name from the `## Goal: <name>` heading |
| `keywords` | array of string | lowercased keywords for session matching |
| `target_pct` | number \| null | desired % of active time; null when untargeted |
| `archived` | bool | `true` for a retired goal (`archived: true` in goals.md). Archived goals are **excluded** from alignment/targets/drift, but still listed here so the editor can unarchive them |

Related commands (human-readable stdout, not JSON): `goals show --raw` prints the
file verbatim; `goals write` reads new markdown from **STDIN**, atomically
overwrites `goals.md` (temp file + rename), then prints a one-line summary
`wrote goals.md (N goals: id1, id2, …)`. The write **never rejects**: if the new
content parses to zero goals it is still written and a warning goes to stderr
(the file may be mid-draft). `goals archive <goal-id>` / `goals unarchive
<goal-id>` toggle a goal's `archived:` line in place (atomic write). Bare
`dayloop goals` pretty-prints the summary (archived goals tagged `[archived]`).

### Example

```json
{
  "path": "/Users/you/projects/dayloop/goals.md",
  "raw": "## Goal: Ship dayloop\nkeywords: dayloop, screenpipe\ntarget_pct: 35\n…",
  "goals": [
    { "id": "ship-dayloop", "name": "Ship dayloop", "keywords": ["dayloop", "screenpipe"], "target_pct": 35.0 },
    { "id": "deep-work-coding", "name": "Deep work / coding", "keywords": ["code", "vscode"], "target_pct": 50.0 }
  ]
}
```
