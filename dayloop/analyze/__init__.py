"""dayloop.analyze — pluggable LLM analysis backends + benchmark harness.

Import backends lazily (from .gemini / .ollama inside functions) so the core
pipeline never pays for, or breaks on, optional dependencies.
"""
