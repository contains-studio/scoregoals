"""scoregoals.config — FROZEN configuration loader.

Loads config.toml (stdlib tomllib), merges environment overrides, resolves
absolute paths, and creates the data directories. Works with zero config
present (default_config()). Stdlib-only; no side effects beyond mkdir.

Env overrides (always win over config.toml). The primary prefix is SCOREGOALS_*;
the legacy DAYLOOP_* prefix is still honored as a fallback (checked only when the
SCOREGOALS_* form is unset) so pre-rebrand shells/launchd envs keep working:
  GEMINI_API_KEY            -> config.gemini_api_key (unprefixed; never stored)
  SCREENPIPE_API_KEY        -> config.screenpipe_api_key (unprefixed)
  SCOREGOALS_CONFIG (DAYLOOP_CONFIG)  -> path to config.toml
  SCOREGOALS_DATA_DIR, SCOREGOALS_SCREENPIPE_URL, SCOREGOALS_OLLAMA_URL,
  SCOREGOALS_OLLAMA_MODEL, SCOREGOALS_GEMINI_MODEL, SCOREGOALS_ICLOUD_MIRROR
  (each with a legacy DAYLOOP_* fallback)
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Config",
    "load_config",
    "default_config",
    "DEFAULTS",
    "SETTINGS_KEYS",
    "SECRET_KEYS",
    "SETTINGS_FILENAME",
    "load_settings",
    "set_setting",
    "get_setting",
    "effective_settings",
]


def _default_projects_dir() -> str:
    """Home-relative default: ~/projects if it exists, else the home dir itself.

    Portable across machines/users — never a hardcoded absolute path.
    """
    home = Path.home()
    candidate = home / "projects"
    return str(candidate if candidate.is_dir() else home)


DEFAULTS: dict = {
    "data_dir": "./data",
    "screenpipe_url": "http://localhost:3030",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16",
    "gemini_model": "gemini-3.5-flash",  # served by agy (Antigravity); SDK path too when keyed
    "gemini_price_in_per_1m": 0.30,   # USD per 1M input tokens — editable placeholder
    "gemini_price_out_per_1m": 2.50,  # USD per 1M output tokens — editable placeholder
    "projects_dir": _default_projects_dir(),
    "github_user": "",  # empty = auto-detect from `gh api user`
    "nudge_threshold_min": 20,
    "icloud_mirror": "",  # empty = off
    # --- app-mutable runtime settings (menu bar app writes these) ------------
    "default_backend": "ollama",  # ollama | gemini | both
    "nudges_enabled": True,       # nudge honors this
    "capture_paused": False,      # capture honors this (skips when true)
    "refresh_seconds": 30,        # app poll cadence (advisory; used by the app)
    "llm_classify": True,         # local-LLM classification tier (classify.py) on/off
    "audit_port": 5030,           # port the always-on audit server binds on 127.0.0.1
}

# Config-key env overrides, keyed by prefix-less SUFFIX. Each suffix is looked up
# under every prefix in _ENV_PREFIXES (SCOREGOALS_ first, legacy DAYLOOP_ second).
_ENV_OVERRIDES: dict[str, str] = {
    "DATA_DIR": "data_dir",
    "SCREENPIPE_URL": "screenpipe_url",
    "OLLAMA_URL": "ollama_url",
    "OLLAMA_MODEL": "ollama_model",
    "GEMINI_MODEL": "gemini_model",
    "ICLOUD_MIRROR": "icloud_mirror",
    "DEFAULT_BACKEND": "default_backend",
    "NUDGES_ENABLED": "nudges_enabled",
    "CAPTURE_PAUSED": "capture_paused",
    "REFRESH_SECONDS": "refresh_seconds",
    "LLM_CLASSIFY": "llm_classify",
    "AUDIT_PORT": "audit_port",
}

# Env-var prefixes in precedence order: primary SCOREGOALS_*, then the legacy
# DAYLOOP_* fallback (honored so pre-rebrand environments keep working).
_ENV_PREFIXES: tuple[str, ...] = ("SCOREGOALS_", "DAYLOOP_")


def _env_lookup(suffix: str) -> str | None:
    """First set value for `suffix` across _ENV_PREFIXES (SCOREGOALS_ wins over the
    legacy DAYLOOP_ fallback), or None when neither is set."""
    for prefix in _ENV_PREFIXES:
        val = os.environ.get(prefix + suffix)
        if val:
            return val
    return None

# JSON overlay file (under data_dir) the app can write to mutate settings without
# touching config.toml. Precedence: DEFAULTS < config.toml < settings.json < env.
SETTINGS_FILENAME = "settings.json"

# Keys the app is allowed to read/write through `scoregoals config`. Each maps to
# the coercion applied to string inputs (from `config set` / env vars).
SETTINGS_KEYS: dict[str, str] = {
    "default_backend": "backend",  # ollama | gemini | both
    "nudges_enabled": "bool",
    "capture_paused": "bool",
    "llm_classify": "bool",
    "refresh_seconds": "int",
    "audit_port": "int",
    "ollama_url": "str",
    "gemini_model": "str",
    "projects_dir": "str",  # scanned for local git activity (setup writes this)
    "github_user": "str",   # "" = auto-detect via gh
}

# Secret keys the app/setup can WRITE (via `scoregoals config set`) but that are
# never echoed back, listed by `config` / `effective_settings`, or stored in
# config.toml. They live only in data/settings.json (which is under data/ and
# gitignored). Resolved-value precedence: env GEMINI_API_KEY > settings.json >
# config.toml. Writing an empty value clears the stored secret.
SECRET_KEYS: frozenset = frozenset({"gemini_api_key", "screenpipe_api_key"})


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # tolerate "30", "30.0", 30.0
    except (TypeError, ValueError):
        return default


def _as_backend(value: object) -> str:
    v = str(value).strip().lower()
    return v if v in ("ollama", "gemini", "both") else "ollama"


def coerce_setting(key: str, value: object):
    """Coerce a raw (possibly string) value to the type `key` expects."""
    kind = SETTINGS_KEYS.get(key)
    if kind == "bool" or key in ("nudges_enabled", "capture_paused", "llm_classify"):
        return _as_bool(value)
    if kind == "int" or key in ("refresh_seconds", "nudge_threshold_min"):
        return _as_int(value, int(DEFAULTS.get(key, 0) or 0))
    if kind == "backend" or key == "default_backend":
        return _as_backend(value)
    if key in ("gemini_price_in_per_1m", "gemini_price_out_per_1m"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULTS.get(key, 0.0) or 0.0)
    return str(value)


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
    # app-mutable runtime settings (see SETTINGS_KEYS); all have safe defaults so
    # older callers that build Config without them keep working.
    default_backend: str = "ollama"
    nudges_enabled: bool = True
    capture_paused: bool = False
    llm_classify: bool = True
    refresh_seconds: int = 30
    audit_port: int = 5030
    # screenpipe API auth (the CLI requires Bearer auth for /search since ~v0.4):
    # env SCREENPIPE_API_KEY > settings.json > config.toml > auto `screenpipe auth token`.
    screenpipe_api_key: str | None = None
    settings_path: str = ""  # absolute path to data/settings.json
    raw: dict = field(default_factory=dict)  # the parsed config.toml, verbatim


def _find_config_file(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = _env_lookup("CONFIG")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.cwd() / "config.toml")
    # repo root = parent of the scoregoals package
    candidates.append(Path(__file__).resolve().parent.parent / "config.toml")
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def _apply_env(values: dict) -> None:
    for suffix, cfg_key in _ENV_OVERRIDES.items():
        val = _env_lookup(suffix)
        if val:
            values[cfg_key] = val


def load_settings(data_dir: str | Path) -> dict:
    """Read the data/settings.json overlay -> dict. Missing or invalid file
    (bad JSON, not an object) yields {} with a one-line stderr warning; never
    raises, so a corrupt overlay can't take the whole CLI down."""
    path = Path(data_dir) / SETTINGS_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[scoregoals.config] warning: ignoring bad {path} ({exc})", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _build(values: dict, raw: dict, base: Path) -> Config:
    # Env first so SCOREGOALS_DATA_DIR (or legacy DAYLOOP_DATA_DIR) is honored
    # when locating settings.json.
    _apply_env(values)

    data_dir = Path(str(values["data_dir"])).expanduser()
    if not data_dir.is_absolute():
        data_dir = base / data_dir
    data_dir = data_dir.resolve()

    timeline_dir = data_dir / "timeline"
    reports_dir = data_dir / "reports"
    benchmarks_dir = data_dir / "benchmarks"
    for d in (data_dir, timeline_dir, reports_dir, benchmarks_dir):
        d.mkdir(parents=True, exist_ok=True)

    # settings.json overlay: sits above config.toml, below env. data_dir itself
    # is never relocated from the overlay (the file lives inside data_dir).
    overlay = load_settings(data_dir)
    for k, v in overlay.items():
        if k in DEFAULTS and k != "data_dir":
            values[k] = v
    # Re-apply env so it wins over the overlay.
    _apply_env(values)

    goals_path = Path(str(raw.get("goals_path") or (base / "goals.md"))).expanduser()

    # Gemini API key (BYOK): env wins, then the settings.json overlay (written by
    # setup / the menu bar app), then config.toml. Never defaulted; never logged.
    overlay_key = overlay.get("gemini_api_key")
    gemini_api_key = (
        os.environ.get("GEMINI_API_KEY")
        or (str(overlay_key) if overlay_key else None)
        or raw.get("gemini_api_key")
        or None
    )

    # screenpipe API key: same secret handling as the Gemini key. When all three
    # sources are empty, sources/screenpipe.py auto-resolves it at fetch time by
    # shelling out to `screenpipe auth token` (cached per process).
    sp_overlay_key = overlay.get("screenpipe_api_key")
    screenpipe_api_key = (
        os.environ.get("SCREENPIPE_API_KEY")
        or (str(sp_overlay_key) if sp_overlay_key else None)
        or raw.get("screenpipe_api_key")
        or None
    )

    return Config(
        root=str(base),
        data_dir=str(data_dir),
        timeline_dir=str(timeline_dir),
        reports_dir=str(reports_dir),
        benchmarks_dir=str(benchmarks_dir),
        # NOTE: the sqlite filename stays "dayloop.db" (legacy name kept as-is —
        # it is live data; renaming the file would orphan the existing database).
        db_path=str(data_dir / "dayloop.db"),
        goals_path=str(goals_path),
        screenpipe_url=str(values["screenpipe_url"]).rstrip("/"),
        ollama_url=str(values["ollama_url"]).rstrip("/"),
        ollama_model=str(values["ollama_model"]),
        gemini_model=str(values["gemini_model"]),
        gemini_api_key=gemini_api_key,
        gemini_price_in_per_1m=float(values["gemini_price_in_per_1m"]),
        gemini_price_out_per_1m=float(values["gemini_price_out_per_1m"]),
        projects_dir=str(values["projects_dir"]),
        github_user=str(values["github_user"]),
        nudge_threshold_min=int(values["nudge_threshold_min"]),
        icloud_mirror=str(values["icloud_mirror"]),
        default_backend=_as_backend(values["default_backend"]),
        nudges_enabled=_as_bool(values["nudges_enabled"]),
        capture_paused=_as_bool(values["capture_paused"]),
        llm_classify=_as_bool(values["llm_classify"]),
        refresh_seconds=_as_int(values["refresh_seconds"], 30),
        audit_port=_as_int(values["audit_port"], 5030),
        screenpipe_api_key=screenpipe_api_key,
        settings_path=str(data_dir / SETTINGS_FILENAME),
        raw=raw,
    )


def effective_settings(config: Config) -> dict:
    """The effective (merged) values of the app-mutable settings — exactly the
    keys `scoregoals config` exposes, in a stable order."""
    return {
        "default_backend": config.default_backend,
        "nudges_enabled": config.nudges_enabled,
        "capture_paused": config.capture_paused,
        "llm_classify": config.llm_classify,
        "refresh_seconds": config.refresh_seconds,
        "audit_port": config.audit_port,
        "ollama_url": config.ollama_url,
        "gemini_model": config.gemini_model,
        "projects_dir": config.projects_dir,
        "github_user": config.github_user,
    }


def get_setting(config: Config, key: str):
    """Return the effective value of one app-mutable setting."""
    if key not in SETTINGS_KEYS:
        raise KeyError(key)
    return effective_settings(config)[key]


def set_setting(config: Config, key: str, value: object) -> dict:
    """Coerce and persist one setting into data/settings.json (merging with
    whatever is already there). Returns the new overlay dict. Raises KeyError
    for unknown keys.

    Secret keys (SECRET_KEYS, e.g. gemini_api_key) are stored verbatim as a
    string; an empty string clears the stored secret. The value is never logged.
    """
    if key not in SETTINGS_KEYS and key not in SECRET_KEYS:
        raise KeyError(key)
    path = Path(config.settings_path or (Path(config.data_dir) / SETTINGS_FILENAME))
    overlay = load_settings(config.data_dir)
    if key in SECRET_KEYS:
        text = str(value)
        if text == "":
            overlay.pop(key, None)  # empty value clears the stored secret
        else:
            overlay[key] = text
    else:
        overlay[key] = coerce_setting(key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overlay, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return overlay


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
