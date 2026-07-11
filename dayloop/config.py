"""dayloop.config — FROZEN configuration loader.

Loads config.toml (stdlib tomllib), merges environment overrides, resolves
absolute paths, and creates the data directories. Works with zero config
present (default_config()). Stdlib-only; no side effects beyond mkdir.

Env overrides (always win over config.toml):
  GEMINI_API_KEY            -> config.gemini_api_key (never stored in the file)
  DAYLOOP_CONFIG            -> path to config.toml
  DAYLOOP_DATA_DIR, DAYLOOP_SCREENPIPE_URL, DAYLOOP_OLLAMA_URL,
  DAYLOOP_OLLAMA_MODEL, DAYLOOP_GEMINI_MODEL, DAYLOOP_ICLOUD_MIRROR
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Config", "load_config", "default_config", "DEFAULTS"]

DEFAULTS: dict = {
    "data_dir": "./data",
    "screenpipe_url": "http://localhost:3030",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16",
    "gemini_model": "gemini-2.5-flash",
    "gemini_price_in_per_1m": 0.30,   # USD per 1M input tokens — editable placeholder
    "gemini_price_out_per_1m": 2.50,  # USD per 1M output tokens — editable placeholder
    "projects_dir": "/Users/contains/projects",
    "github_user": "mgalpert",
    "nudge_threshold_min": 20,
    "icloud_mirror": "",  # empty = off
}

_ENV_OVERRIDES: dict[str, str] = {
    "DAYLOOP_DATA_DIR": "data_dir",
    "DAYLOOP_SCREENPIPE_URL": "screenpipe_url",
    "DAYLOOP_OLLAMA_URL": "ollama_url",
    "DAYLOOP_OLLAMA_MODEL": "ollama_model",
    "DAYLOOP_GEMINI_MODEL": "gemini_model",
    "DAYLOOP_ICLOUD_MIRROR": "icloud_mirror",
}


@dataclass
class Config:
    """Resolved runtime configuration. All *_dir / *_path fields are absolute."""

    root: str
    data_dir: str
    timeline_dir: str
    reports_dir: str
    benchmarks_dir: str
    db_path: str
    goals_path: str
    screenpipe_url: str
    ollama_url: str
    ollama_model: str
    gemini_model: str
    gemini_api_key: str | None
    gemini_price_in_per_1m: float
    gemini_price_out_per_1m: float
    projects_dir: str
    github_user: str
    nudge_threshold_min: int
    icloud_mirror: str
    raw: dict = field(default_factory=dict)  # the parsed config.toml, verbatim


def _find_config_file(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("DAYLOOP_CONFIG")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.cwd() / "config.toml")
    # repo root = parent of the dayloop package
    candidates.append(Path(__file__).resolve().parent.parent / "config.toml")
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def _build(values: dict, raw: dict, base: Path) -> Config:
    for env_key, cfg_key in _ENV_OVERRIDES.items():
        if os.environ.get(env_key):
            values[cfg_key] = os.environ[env_key]

    data_dir = Path(str(values["data_dir"])).expanduser()
    if not data_dir.is_absolute():
        data_dir = base / data_dir
    data_dir = data_dir.resolve()

    timeline_dir = data_dir / "timeline"
    reports_dir = data_dir / "reports"
    benchmarks_dir = data_dir / "benchmarks"
    for d in (data_dir, timeline_dir, reports_dir, benchmarks_dir):
        d.mkdir(parents=True, exist_ok=True)

    goals_path = Path(str(raw.get("goals_path") or (base / "goals.md"))).expanduser()

    return Config(
        root=str(base),
        data_dir=str(data_dir),
        timeline_dir=str(timeline_dir),
        reports_dir=str(reports_dir),
        benchmarks_dir=str(benchmarks_dir),
        db_path=str(data_dir / "dayloop.db"),
        goals_path=str(goals_path),
        screenpipe_url=str(values["screenpipe_url"]).rstrip("/"),
        ollama_url=str(values["ollama_url"]).rstrip("/"),
        ollama_model=str(values["ollama_model"]),
        gemini_model=str(values["gemini_model"]),
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or raw.get("gemini_api_key") or None,
        gemini_price_in_per_1m=float(values["gemini_price_in_per_1m"]),
        gemini_price_out_per_1m=float(values["gemini_price_out_per_1m"]),
        projects_dir=str(values["projects_dir"]),
        github_user=str(values["github_user"]),
        nudge_threshold_min=int(values["nudge_threshold_min"]),
        icloud_mirror=str(values["icloud_mirror"]),
        raw=raw,
    )


def load_config(path: str | None = None) -> Config:
    """Load config.toml (auto-discovered or `path`), apply env overrides,
    resolve absolute paths, create data dirs, return a Config.

    Falls back to default_config() when no config.toml exists anywhere.
    """
    cfg_file = _find_config_file(path)
    if cfg_file is None:
        return default_config()
    with open(cfg_file, "rb") as fh:
        raw = tomllib.load(fh)
    values = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw:
            values[key] = raw[key]
    return _build(values, raw, cfg_file.parent)


def default_config() -> Config:
    """Config built purely from DEFAULTS + env overrides (no config.toml).

    data_dir resolves relative to the current working directory.
    """
    return _build(dict(DEFAULTS), {}, Path.cwd())
