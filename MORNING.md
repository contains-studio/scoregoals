# ☕ Good morning, Michael

Coffee first. Here's what happened while you slept — and the two small things
you need to do. Today's real day is intact (score **66**, scored, 32.8 active
min; your corrections file is untouched at 0 lines).

## What shipped overnight

- **Review & correct pane** — the popover now lists uncertain sessions
  uncertain-first; one gesture (goal · Off-track · Not work · ✓) files a
  correction and reshows the day score.
- **Score evidence view** — tap the score to see exactly which sessions and
  minutes produced it, grouped by goal.
- **Learning rules** — a pattern you correct the same way 3+ times (with no
  contradictions) becomes a deterministic rule that pre-answers future sessions.
- **Min-data honesty** — a day under 30 active minutes now reads "insufficient
  data" everywhere (menu bar, week strip, nightly report) instead of a
  misleading number; empty days are gaps, not zeros.
- **Away-aware recorder** — capture stops on lock / sleep / idle-with-no-meeting
  and resumes on activity, and now self-heals a missed lock/unlock via
  window-server ground truth so it can never get stuck watching (or stuck off).
- **App icon** — ScoreGoals now has its own menu-bar/Finder icon.
- **Polish** — honest "Confirm all" results, no re-tappable rows mid-write,
  archived-goal sessions labeled honestly instead of "Unmatched".
- **2 adversarial reviewers** audited the night's commits: all high & medium
  findings fixed (13 of 15; the 2 low-risk items are noted under Limits below).

## Your 2 steps

1. **Relaunch the recorder once** (it's still running last night's binary; the
   fixed one is staged and waiting):

   ```
   open /Users/contains/projects/scoregoals/recorder/ScreenpipeRecorder.app
   ```

   Re-toggle Screen Recording **only if macOS asks** (same bundle identity, so it
   usually won't). The menu-bar app already relaunched itself on the new build.

2. **Optional — turn on the daily schedule:**

   ```
   sh /Users/contains/projects/scoregoals/scripts/install.sh
   ```

   Installs: **07:30** morning plan · **20-min** drift nudges · **21:00**
   end-of-day report (Sun 20:00 weekly). Reverts cleanly with:

   ```
   sh /Users/contains/projects/scoregoals/scripts/install.sh uninstall
   ```

## First-run flow

Open the popover → **set Today's 3** intentions → work. At lunch, **tap the
score** to see the evidence behind it, then **review the uncertain sessions** —
every correction teaches the engine, and repeated ones become rules. That's the
whole loop: it gets quieter the more you correct it.

## Notes

- The menu-bar icon can hide **behind the notch** on laptops. If you don't see
  the gauge, quit another menu-bar app or install a manager (Bartender / Ice).
- **Limits (honest):**
  - Learning v1 is **mined rules only** — deterministic exact-pattern rules.
    kNN / few-shot generalization is the next step, so a pattern whose window
    title keeps changing may not promote yet (it fails safe: no wrong rule,
    just less automation).
  - The review UI is **v1** — functional, not yet fancy.
  - Gemini narratives run through **agy (Antigravity)** locally, ~10s each; the
    ollama backend is faster. The score itself is deterministic and identical
    across backends either way.
  - Two low-risk reviewer notes were left as-is by design: the corrections file
    append is already crash-tolerant (a torn line is skipped by the reader, not
    merged), and rule-mining can stall on volatile titles (the fail-safe above).
