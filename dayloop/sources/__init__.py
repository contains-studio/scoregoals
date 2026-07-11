"""dayloop.sources — sensor modules (screenpipe, calendar, github, granola).

Each module exposes a single fetch() with a frozen signature and MUST:
- import third-party libs (requests) lazily inside fetch(), never at top
- degrade gracefully: unreachable/missing tool -> one-line stderr warning + []
- never raise out of fetch() for environmental problems
"""
