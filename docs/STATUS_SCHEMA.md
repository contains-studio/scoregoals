# ScoreGoals machine-readable CLI schema (`schema_version = 1`)

This document is the contract between the ScoreGoals Python engine and the macOS
menu bar app. The Swift app decodes against the shapes below, so field names,
types, and nullability are **stable** — additive changes only; renames/removals
bump `schema_version`.

All three commands:

- Emit **exactly one** JSON object to **stdout** (pretty-printed, UTF-8,
  `ensure_ascii=false` so unicode glyphs like the sparkline are literal).
- Send diagnostics/warnings to **stderr**, never stdout.
- Are produced by `scoregoals/status.py`, `scoregoals/intentions.py`,
  `scoregoals/config.py`.

Timestamps are ISO-8601 strings with a UTC offset (e.g.
`"2026-07-11T18:51:04-07:00"`), except a few date-only calendar/session strings
that may be naive local (`"2026-07-11T18:30:00"`); the app should parse both.

Run the engine from the repo root as:
`.venv/bin/python -m scoregoals <command>`

---

## `scoregoals status --json`

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
| `review` | object | `{ "needs_review": int }` — low-confidence sessions awaiting a correction today (see `review`) |
| `corrections_this_week` | int | count of user labels applied in the 7-day window ending `date` |
| `learning` | object | learning KPI surface (see below) |
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

Scoring now applies the correction/learning layer (`scoregoals/align.py`): user
labels and learned rules override keyword matches, and `not_work` sessions are
excluded from active minutes entirely (see the review section). **Min-data
guard:** below **30** active captured minutes the day is *unscored* — `overall`
is `null` and `scored` is `false` (honest uncertainty; unknown ≠ off-track). The
Swift app must treat `overall` as **nullable** and gate on `scored`.

| field | type | notes |
|------|------|------|
| `overall` | int 0–100 **\| null** | day score; `null` when `scored` is `false` (< 30 active min). When non-null it matches `review`'s score and the EOD report |
| `scored` | bool | `false` => insufficient captured data (< 30 active min); `overall` is then `null` |
| `on_track` | bool | `scored && overall != null && overall >= 60` (always `false` when unscored) |
| `active_minutes` | number | active minutes **after** excluding `not_work` sessions |

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

### `review` and `learning`

Surfaced so the menu bar can show the correction backlog and the learning KPI
without a second call.

`review`:

| field | type | notes |
|------|------|------|
| `needs_review` | int | sessions today whose assignment is a keyword guess or unmatched (i.e. not settled by a user label or a learned rule) — the count the Review pane badges |

`corrections_this_week` (top level): int — user labels applied in the 7-day
window ending `date`. The learning KPI is "this trends toward zero".

`learning`:

| field | type | notes |
|------|------|------|
| `active_rules` | int | number of active learned rules in `data/learned_rules.json` |
| `corrections_by_week` | array of object | `[{ "week": "2026-W28", "count": int }, …]`, oldest ISO-week first; the correction-rate trend |

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
  "score": { "overall": 75, "scored": true, "on_track": true, "active_minutes": 304.0 },
  "goals": [
    { "goal_id": "ship-scoregoals", "goal_name": "Ship scoregoals", "minutes": 131.0, "pct_time": 43.1, "target_pct": 35.0, "on_track": true },
    { "goal_id": "deep-work-coding", "goal_name": "Deep work / coding", "minutes": 0.0, "pct_time": 0.0, "target_pct": 50.0, "on_track": false },
    { "goal_id": "investor-partner-comms", "goal_name": "Investor & partner comms", "minutes": 94.0, "pct_time": 30.9, "target_pct": 20.0, "on_track": true },
    { "goal_id": "learning-research", "goal_name": "Learning & research", "minutes": 47.0, "pct_time": 15.5, "target_pct": 10.0, "on_track": true },
    { "goal_id": "unaligned", "goal_name": "Unaligned", "minutes": 32.0, "pct_time": 10.5, "target_pct": null, "on_track": true }
  ],
  "drift_flags": [ "No time on 'Deep work / coding' (target 50%)" ],
  "review": { "needs_review": 3 },
  "corrections_this_week": 1,
  "learning": { "active_rules": 2, "corrections_by_week": [ { "week": "2026-W27", "count": 5 }, { "week": "2026-W28", "count": 1 } ] },
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

## `scoregoals today --json`

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
    { "id": "9a4b3739", "text": "Read screenpipe docs", "goal_id": "ship-scoregoals", "goal_name": "Ship scoregoals", "done": false, "attributed_minutes": 131.0, "apps": ["Code", "Google Chrome"], "carried_from": null }
  ],
  "history_summary": { "days": 7, "completion_rate": 0.6 }
}
```

---

## `scoregoals today history [--days N] [--json]`

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

## `scoregoals focus --json`

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
  "goal_id": "ship-scoregoals",
  "goal_name": "Ship scoregoals",
  "started_at": "2026-07-11T18:51:44-07:00",
  "until": "2026-07-11T19:51:44-07:00"
}
```

---

## `scoregoals config --json`

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

Environment overrides (highest precedence): `SCOREGOALS_DEFAULT_BACKEND`,
`SCOREGOALS_NUDGES_ENABLED`, `SCOREGOALS_CAPTURE_PAUSED`, `SCOREGOALS_REFRESH_SECONDS`,
`SCOREGOALS_OLLAMA_URL`, `SCOREGOALS_GEMINI_MODEL`.

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

## `scoregoals goals --json`

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
| `id` | string | slug of the name (`ship-scoregoals`), duplicate slugs get `-2`, `-3`, … |
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
`scoregoals goals` pretty-prints the summary (archived goals tagged `[archived]`).

### Example

```json
{
  "path": "/Users/you/projects/scoregoals/goals.md",
  "raw": "## Goal: Ship scoregoals\nkeywords: scoregoals, screenpipe\ntarget_pct: 35\n…",
  "goals": [
    { "id": "ship-scoregoals", "name": "Ship scoregoals", "keywords": ["scoregoals", "screenpipe"], "target_pct": 35.0 },
    { "id": "deep-work-coding", "name": "Deep work / coding", "keywords": ["code", "vscode"], "target_pct": 50.0 }
  ]
}
```

---

## `scoregoals review [--date YYYY-MM-DD] [--json]`

The Review & Correct surface: every session for the day resolved to a verdict,
**uncertain-first** (sessions needing review come first, biggest minutes first —
the Review pane renders top-down). Read-only. Without `--json` a human table is
printed; `--json` emits the object below. When no timeline exists for the date,
`sessions` is `[]` and `score.overall` is `null`.

| field | type | notes |
|------|------|------|
| `date` | string `YYYY-MM-DD` | the day reviewed |
| `score` | object | `{ "overall": int\|null, "scored": bool, "active_minutes": number }` — same guard as `status.score` |
| `needs_review` | int | count of `sessions[]` with `needs_review == true` |
| `sessions` | array of object | one per session, uncertain-first (see below) |

`sessions[]`:

| field | type | notes |
|------|------|------|
| `id` | string | stable 12-hex session id (`models.session_id`); the handle `label` takes |
| `start` / `end` | string (ISO, may be naive local) | session bounds |
| `span` | string | `"HH:MM-HH:MM"` convenience form of start–end |
| `app` | string \| null | foreground app |
| `title` | string \| null | window title |
| `minutes` | number | session duration |
| `goal_id` | string \| null | resolved **active** goal id; null for `off_track`/`not_work`/unmatched or a verdict naming an archived goal |
| `goal_name` | string \| null | display name of `goal_id` |
| `verdict` | string \| null | goal id, `"off_track"`, `"not_work"`, or `null` (unmatched) |
| `confidence` | number 0–1 | `1.0` label · `0.9` rule · `0.6` keyword (`0.4` on a keyword collision) · `0.2` none |
| `verdict_source` | string | `"label"` \| `"rule"` \| `"keyword"` \| `"none"` — authority that set the verdict |
| `needs_review` | bool | `true` unless the verdict came from a user `label` or a learned `rule` |

## `scoregoals label <session-id> (--goal <id> | --off-track | --not-work | --confirm)`

Appends one **user** correction for the session to `data/labels.jsonl`, then
recomputes and prints the day delta: `labeled <id> → <verdict>   score 66 -> 71`
(each side is the integer score, or `insufficient data` when unscored). Exactly
one of the four verdict flags is required; `--confirm` accepts the session's
current resolved assignment (no-op on the number, but promotes it to an
authoritative label). `--date` defaults to today; the id may be a unique prefix.
Exit 2 (stderr message, no mutation) for an unknown session id, unknown
`--goal` id, or `--confirm` on a session with no current assignment.

- `--off-track` → verdict `off_track`: worked, but on no goal (counts as active,
  attributed to `unaligned`).
- `--not-work` → verdict `not_work`: out of scope — **excluded from active
  minutes and every goal computation** (personal time is not penalized).

## `scoregoals learn`

Mines consistent corrections into deterministic rules (see
`data/learned_rules.json` below) and prints a one-line summary plus the promoted
and retired rules:

```
scoregoals learn — 1 promoted, 0 retired, 1 active rule(s)
  + SynthApp · title~widget → off_track  (from 3 labels)
```

A pattern `(app, dominant title token) → verdict` promotes when its **latest**
user labels are ≥ 3, unanimous, and (for goal verdicts) the goal is still
active. A rule retires (`contradicted`) the moment a later label disagrees, or
(`archived-goal`) when its goal is archived/removed. Rules apply in `align.py`
**before** any keyword/LLM guess.

---

## Data model additions (`schema_version` unchanged — additive)

**`Session.id`** (`scoregoals/models.py`) — new `string` field on every session,
a stable 12-hex `sha1(date|start|app)[:12]` (`models.session_id`). Set during
segmentation and round-tripped through `to_json`/`from_json`; stable across
rebuilds of the same timeline. Sessions in timelines captured before ids existed
decode with `id == ""` and are re-derived on the fly by `review`/`label`.

**`Report.scored`** (`scoregoals/models.py`) — new `bool` field, default `true`.
`false` marks an insufficient-data day (< 30 active minutes). In that case the
deterministic score written to `data/benchmarks/compare.csv` is the sentinel
**`overall_score = -1`** (documented so consumers of the CSV don't mistake it for
a real 0). Scored days carry the real 0–100 value as before.

**`data/labels.jsonl`** — append-only correction store, one JSON object per line:
`{ ts, session_id, date, fingerprint: { app, title_tokens, text_keywords,
hour_bucket }, verdict: goal_id|"off_track"|"not_work", source: "user"|"implicit" }`.
Latest line per `session_id` wins. Malformed lines are skipped with a warning.

**`data/learned_rules.json`** — `{ "rules": [...], "retired": [...] }`. Each rule:
`{ "rule": { "app", "title_token", "verdict" }, "created_from": [{ session_id, ts }],
"created_at" }`. Retired entries add `"reason"` (`contradicted`|`archived-goal`)
and `"retired_at"`.
