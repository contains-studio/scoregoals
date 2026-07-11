<!--
dayloop goals file — parsed by dayloop/compare/align.py (load_goals).

Format, one goal per section:

  ## Goal: <name>
  keywords: comma, separated, keywords      <- matched (case-insensitive) against
                                               session app/title/project/topic/summary/excerpt
  target_pct: 30                            <- optional; % of ACTIVE time you want on this goal
  <description paragraph — free text, fed to the LLM for context>

Notes:
- Each session counts toward AT MOST ONE goal (most distinct keyword hits
  wins; ties break by the order below) — see compare/align.py. Targets still
  need not sum to 100.
- Edit freely; changes apply on the next capture/analyze run.
-->

## Goal: Ship dayloop
keywords: dayloop, screenpipe, ollama, python, vs code, vscode, pytest, cli, sqlite
target_pct: 35
Get the dayloop cybernetic tracker to a daily-usable v1: live capture via
screenpipe, nightly end-of-day reports, morning plans, and the
gemini-vs-ollama benchmark so I can pick a default backend.

## Goal: Deep work / coding
keywords: code, vscode, terminal, iterm, github, python, docs.python.org, debugging, refactor, commit
target_pct: 50
Long, uninterrupted maker blocks — writing and shipping code, reading
technical docs, reviewing PRs. The point is contiguous focus, not just total
minutes; fragmented coding time counts against this goal's spirit.

## Goal: Investor & partner comms
keywords: email, gmail, mail, slack, zoom, meeting, investor, northwind, deck, partner, granola
target_pct: 20
Keep investor and partner relationships warm without letting comms eat the
day: timely replies, tight update calls, decks and follow-ups shipped the
same week they're promised.

## Goal: Learning & research
keywords: tradingview, watchlist, research, paper, article, documentation, tutorial, hacker news
target_pct: 10
Deliberate input: market/chart review (TradingView watchlists), technical
reading, and research that feeds current projects. Passive entertainment
(YouTube autoplay, doomscrolling) does NOT count.
