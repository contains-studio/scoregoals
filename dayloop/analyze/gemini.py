"""Gemini analysis backend.

Call priority inside analyze():
1. google-genai SDK — only if config.gemini_api_key is set AND the optional
   `google-genai` package is installed (imported lazily, never at top).
   Real token counts come from usage_metadata.prompt_token_count /
   candidates_token_count.
2. The `gemini` CLI (OAuth via ~/.gemini) as a subprocess fallback — what
   works on Michael's machine today (GEMINI_API_KEY is NOT set). Invocation:
   the shared prompt is piped on stdin to `gemini -m <model>` (per
   `gemini --help`: "-p, --prompt ... Appended to input on stdin"; stdin
   alone runs one-shot non-interactive). Override the argv tail with
   `gemini_cli_args` in config.toml if the CLI's flags ever change.
   The CLI reports no usage, so tokens are base.estimate_tokens() estimates
   and raw["metered"] = false.
3. Neither available -> RuntimeError with a clear message; benchmark.run
   catches per-backend failures so the pipeline never dies.

Measurement:
- latency_s  : time.monotonic() wall clock around the SDK call / subprocess
- tokens_in/out: real usage metadata (SDK) or estimate_tokens (CLI)
- cost_usd   : base.estimate_cost with config.gemini_price_in_per_1m /
  gemini_price_out_per_1m installed into base.PRICING for this model

The prompt comes from analyze.base.build_prompt so gemini and ollama get
identical input (fair benchmark).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import Config
from ..models import DayTimeline, Goal, GoalAlignment, Report
from .base import PRICING, AnalysisBackend, build_prompt, estimate_cost, estimate_tokens
from .ollama import _coerce_fields, _extract_json


def _find_gemini_cli() -> str | None:
    """Locate the gemini CLI (PATH first, then Homebrew dirs — launchd-safe)."""
    found = shutil.which("gemini")
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = Path(d) / "gemini"
        if p.exists():
            return str(p)
    return None


class GeminiBackend(AnalysisBackend):
    """Cloud backend: Google Gemini (SDK if keyed, else the OAuth CLI)."""

    name = "gemini"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model = config.gemini_model

    def analyze(
        self,
        timeline: DayTimeline,
        goals: list[Goal],
        kind: str,
        alignments: list[GoalAlignment],
    ) -> Report:
        """Run the shared prompt through Gemini; return a populated Report."""
        prompt = build_prompt(timeline, goals, kind, alignments)
        # Install config pricing for this model so estimate_cost uses it.
        PRICING[self.model] = {
            "input": self.config.gemini_price_in_per_1m,
            "output": self.config.gemini_price_out_per_1m,
        }

        if self.config.gemini_api_key:
            try:
                text, tokens_in, tokens_out, metered, latency, raw = self._call_sdk(prompt)
                return self._build_report(
                    timeline, kind, alignments, text, tokens_in, tokens_out, metered, latency, raw
                )
            except ImportError:
                print(
                    "warning: GEMINI_API_KEY set but google-genai not installed"
                    " (`uv pip install google-genai`) — falling back to the gemini CLI",
                    file=sys.stderr,
                )

        text, tokens_in, tokens_out, metered, latency, raw = self._call_cli(prompt)
        return self._build_report(
            timeline, kind, alignments, text, tokens_in, tokens_out, metered, latency, raw
        )

    # --- call paths -----------------------------------------------------------

    def _call_sdk(self, prompt: str) -> tuple[str, int, int, bool, float, dict]:
        """API path: google-genai SDK with real usage metadata."""
        from google import genai  # lazy optional extra — may raise ImportError

        client = genai.Client(api_key=self.config.gemini_api_key)
        t0 = time.monotonic()
        try:
            resp = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={"temperature": 0.2, "response_mime_type": "application/json"},
            )
        except Exception as exc:  # network / quota / auth — clear, catchable
            raise RuntimeError(f"gemini API call failed ({exc.__class__.__name__}): {exc}") from exc
        latency = time.monotonic() - t0

        try:
            text = resp.text or ""
        except Exception:
            text = ""
        if not text:
            raise RuntimeError("gemini API returned an empty response (blocked or no candidates)")

        usage = getattr(resp, "usage_metadata", None)
        tin = getattr(usage, "prompt_token_count", None)
        tout = getattr(usage, "candidates_token_count", None)
        metered = tin is not None and tout is not None
        tokens_in = int(tin) if tin else estimate_tokens(prompt)
        tokens_out = int(tout) if tout else estimate_tokens(text)

        raw: dict = {
            "provider": "gemini-sdk",
            "model_output": text,
            "usage": {
                "prompt_token_count": tin,
                "candidates_token_count": tout,
                "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
            },
            "metered": metered,
        }
        return text, tokens_in, tokens_out, metered, latency, raw

    def _call_cli(self, prompt: str) -> tuple[str, int, int, bool, float, dict]:
        """OAuth path: pipe the prompt on stdin to `gemini -m <model>`."""
        exe = _find_gemini_cli()
        if exe is None:
            raise RuntimeError(
                "gemini backend unavailable: GEMINI_API_KEY is not set and no `gemini` CLI"
                " was found — set the key (with `uv pip install google-genai`) or"
                " `npm install -g @google/gemini-cli`. The ollama backend still works."
            )

        extra = self.config.raw.get("gemini_cli_args")
        if isinstance(extra, str) and extra.strip():
            args = shlex.split(extra)
        elif isinstance(extra, list) and extra:
            args = [str(a) for a in extra]
        else:
            args = ["-m", self.model]
        cmd = [exe, *args]

        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
        timeout_s = float(self.config.raw.get("gemini_cli_timeout_s", 240))

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=timeout_s, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gemini CLI timed out after {timeout_s:.0f}s") from exc
        except OSError as exc:
            raise RuntimeError(f"could not run gemini CLI at {exe}: {exc}") from exc
        latency = time.monotonic() - t0

        text = (proc.stdout or "").strip()
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:300]
            raise RuntimeError(f"gemini CLI exited {proc.returncode}: {err or text[:300]}")
        if not text:
            raise RuntimeError(
                f"gemini CLI produced no output: {(proc.stderr or '').strip()[:300]}"
            )

        # The CLI reports no usage — estimate both sides, flag as unmetered.
        tokens_in = estimate_tokens(prompt)
        tokens_out = estimate_tokens(text)
        raw: dict = {
            "provider": "gemini-cli",
            "command": cmd,
            "model_output": text,
            "metered": False,
        }
        return text, tokens_in, tokens_out, False, latency, raw

    # --- shared assembly --------------------------------------------------------

    def _build_report(
        self,
        timeline: DayTimeline,
        kind: str,
        alignments: list[GoalAlignment],
        text: str,
        tokens_in: int,
        tokens_out: int,
        metered: bool,
        latency: float,
        raw: dict,
    ) -> Report:
        parsed = _extract_json(text)
        if parsed is None:
            narrative = f"[unparseable model output] {text[:600]}"
            score, flags, suggestions = 0, [], []
            raw["parse_error"] = "no JSON object found in model output"
        else:
            narrative, score, flags, suggestions = _coerce_fields(parsed)
            raw["parsed"] = parsed

        return Report(
            date=timeline.date,
            kind=kind,
            backend=self.name,
            model=self.model,
            narrative=narrative,
            alignments=list(alignments),
            overall_score=score,
            drift_flags=flags,
            suggestions=suggestions,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(estimate_cost(self.model, tokens_in, tokens_out), 6),
            latency_s=round(latency, 3),
            raw=raw,
        )
