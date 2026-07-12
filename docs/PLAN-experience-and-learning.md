# ScoreGoals — Experience & Self-Improvement Plan

*Drafted 2026-07-11 (overnight build spec). Owner: Michael. This doc steers the
"review & correct" interface and the learning loop; implementation phases at
the bottom map to the overnight agent build.*

## North star

ScoreGoals earns a permanent spot in the menu bar only if the score is
**believed**. A score is believed when it is (a) grounded in real captured
evidence, (b) explainable in one click, and (c) correctable in two. Every
feature below serves one of those three.

## The three loops

1. **Inner loop (built):** sense the day → align to goals → score → nudge/report.
2. **Correction loop (this plan):** Michael overrules the system — reassign a
   session to a goal, mark it off-track, or mark it *not work*.
3. **Learning loop (this plan):** corrections become training data; the system
   classifies better next week than this week, measured, without Michael's
   involvement. Success metric: **corrections per week trends toward zero.**

## The Review & Correct interface

A "Review" surface in the menu bar popover (and `scoregoals review` in the CLI):

- Each session row: time span, app, inferred goal, confidence, minutes.
- One-gesture overrides: **[goal picker] · [off-track] · [not work] · [✓ confirm]**.
- **Uncertain-first ordering**: only low-confidence sessions surface by default,
  biggest minutes first; "confirm all" clears the rest in one tap.
- A day's review must take **under 60 seconds**. If it takes longer, the
  surfacing threshold is wrong, not the user.
- Corrections recompute the score **immediately and visibly** — cause and
  effect is the trust-builder.
- Tapping the score opens the evidence: which sessions produced it.

### Data model

- Sessions get stable ids (hash of date + start + app).
- `data/labels.jsonl` — append-only: `{ts, session_id, date, fingerprint:
  {app, title_tokens, text_keywords, hour_bucket}, verdict: goal_id |
  "off_track" | "not_work", source: "user" | "implicit"}`.
- Labels are the **highest-authority** signal: user label > learned rule >
  keyword match > LLM guess.
- `not_work` excludes the session from active minutes entirely — personal time
  is not penalized, it is out of scope.

## What must be true for a great experience

- **Explainable**: score → sessions → raw evidence, all local, one click per hop.
- **Honest uncertainty**: below ~30 active captured minutes the day reads
  "insufficient data", never a confident number. Unknown ≠ off-track.
- **Corrections are sacred**: never lost, instantly effective, stored locally,
  and visible ("3 corrections this week").
- **Sensing legitimacy**: record only when recording is legitimate — pause
  capture on screen lock immediately, and after ~5 idle minutes *unless a
  meeting is detected* (hands-off calls must keep transcribing). The system
  that watches you must demonstrably stop watching when you leave.
- **Zero terminal**: everything above reachable from the menu bar.

## Learning without the user

Three mechanisms, escalating in sophistication, all local:

1. **Rule mining (P1)** — a fingerprint pattern corrected/confirmed the same
   way ≥3 times with no contradictions auto-promotes to a deterministic rule in
   `data/learned_rules.json` (`app=Code + title~scoregoals → ship-scoregoals`).
   Rules apply before any LLM, cite the label ids that created them, and
   **retire automatically when contradicted** or when their goal is archived.
2. **Nearest-neighbor memory (P2)** — embed fingerprints with a local Ollama
   embedding model; classify new sessions by their labeled neighbors when rules
   and keywords are silent. Confidence = neighbor agreement.
3. **Few-shot correction (P2)** — the most recent N corrections ride along in
   the LLM classification prompt as worked examples.

**Implicit signal:** an unreviewed day whose classifications were never
overruled is a weak "accepted" label (`source: "implicit"`, low weight). The
system improves even on days Michael never opens Review.

**The learning KPI is user-visible:** weekly report and Settings show the
correction-rate trend ("12 corrections two weeks ago → 3 this week"). A
learning claim without this number is faith, not feedback.

## Build phases

- **P1 (tonight):** session ids + labels store + `review`/`label` CLI +
  Review pane + instant rescore + not-work category + min-data guard +
  rule mining v1 + correction-rate in status + idle/lock/meeting-aware
  recording pause + visual polish pass.
- **P2:** embeddings kNN, few-shot corrections, implicit labels, confidence
  calibration.
- **P3:** weekly self-report of learning KPI; auto-threshold tuning for the
  review queue; project auto-detection (session → git repo).

## Open questions for Michael

- Review cadence: end-of-day prompt, or purely pull (open it when you want)?
- Should `not_work` sessions be visible-but-grey in the day view, or hidden?
- Nudge tone: terse ("20m off-goal") vs coaching ("park Slack til noon?").
