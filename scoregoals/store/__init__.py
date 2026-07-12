"""scoregoals.store — FROZEN persistence layer (sqlite + human-readable JSON)."""

from .db import connect, load_timeline, save_benchmark, save_report, save_timeline

__all__ = ["connect", "save_timeline", "load_timeline", "save_report", "save_benchmark"]
