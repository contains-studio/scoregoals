"""Ollama (local) analysis backend — the zero-cost default.

POST {config.ollama_url}/api/generate with model=config.ollama_model,
stream=False, format="json", low temperature, and the SHARED prompt from
analyze.base.build_prompt — identical input to the gemini backend so the
benchmark comparison is apples-to-apples.

Measurement:
- latency_s  : time.monotonic() wall clock around the HTTP call
- tokens_in  : `prompt_eval_count` from the ollama response (estimate fallback)
- tokens_out : `eval_count` from the ollama response (estimate fallback)
- cost_usd   : 0.0 — local inference is free

The default model (huihui_ai/qwen3-abliterated:4b-thinking-2507-fp16) is a
*thinking* model: any <think>...</think> block (or separate `thinking` field)
is stripped before the JSON reply is parsed. HTTP goes through `requests`
when installed, else stdlib urllib — both imported lazily inside the call so
this module imports on a bare system python. If the ollama server is
unreachable a clear RuntimeError is raised — benchmark.run catches
per-backend failures, so the pipeline never dies.
"""

from __future__ import annotations

import json
import re
import time

from ..config import Config
from ..models import DayTimeline, Goal, GoalAlignment, Report
from .base import AnalysisBackend, build_prompt, estimate_tokens

_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> dict | None:
    """Best-effort extraction of the first JSON object in `text`.

    Handles <think> blocks, ```json fences, and leading/trailing prose.
    Returns None when no JSON object can be decoded.
    """
    if not text:
        return None
    cleaned = _THINK_RE.sub("", text)
    candidates = [m.group(1) for m in _FENCE_RE.finditer(cleaned)]
    candidates.append(cleaned)
    decoder = json.JSONDecoder()
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        idx = cand.find("{")
        while idx != -1:
            try:
                obj, _ = decoder.raw_decode(cand, idx)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
            idx = cand.find("{", idx + 1)
    return None


def _coerce_fields(parsed: dict) -> tuple[str, int, list[str], list[str]]:
    """Pull (narrative, overall_score, drift_flags, suggestions) out of the
    model's parsed JSON, tolerating type sloppiness."""
    narrative = str(parsed.get("narrative") or "").strip()
    try:
        score = int(round(float(parsed.get("overall_score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    def strlist(value: object) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return []

    return narrative, score, strlist(parsed.get("drift_flags")), strlist(parsed.get("suggestions"))


def _post_json(
    url: str, payload: dict, context: str, connect_timeout: float = 5.0, read_timeout: float = 600.0
) -> tuple[int, str]:
    """POST `payload` as JSON to `url`; return (status_code, body_text).

    Uses `requests` when installed, else stdlib urllib (both lazy). Raises
    RuntimeError mentioning `context` when the server is unreachable or the
    call times out.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        import requests  # lazy optional-at-runtime; urllib fallback below
    except ImportError:
        requests = None  # type: ignore[assignment]

    if requests is not None:
        try:
            resp = requests.post(
                url, data=body, headers=headers, timeout=(connect_timeout, read_timeout)
            )
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"{context}: request timed out after {read_timeout:.0f}s") from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"{context}: not reachable ({exc.__class__.__name__})") from exc
        return resp.status_code, resp.text

    import socket
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=read_timeout) as resp:  # noqa: S310 (localhost)
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
        raise RuntimeError(f"{context}: not reachable ({exc})") from exc


class OllamaBackend(AnalysisBackend):
    """Local backend: Ollama + qwen3 (free, private, slower)."""

    name = "ollama"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model = config.ollama_model

    def analyze(
        self,
        timeline: DayTimeline,
        goals: list[Goal],
        kind: str,
        alignments: list[GoalAlignment],
    ) -> Report:
        """Run the shared prompt through the local ollama model; return a
        populated Report (see module docstring for measurement rules)."""
        prompt = build_prompt(timeline, goals, kind, alignments)
        url = f"{self.config.ollama_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2,
                "num_ctx": int(self.config.raw.get("ollama_num_ctx", 8192)),
            },
        }

        context = f"ollama at {self.config.ollama_url} — start it with `ollama serve`"
        t0 = time.monotonic()
        status, body = _post_json(url, payload, context)
        latency = time.monotonic() - t0

        if status != 200:
            detail = body[:200]
            try:
                detail = json.loads(body).get("error", detail)
            except ValueError:
                pass
            raise RuntimeError(
                f"ollama HTTP {status} for model {self.model}: {detail}"
                f" (try `ollama pull {self.model}`)"
            )

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise RuntimeError(f"ollama returned non-JSON envelope: {body[:200]}") from exc

        text = str(data.get("response") or "")
        tokens_in = int(data.get("prompt_eval_count") or 0) or estimate_tokens(prompt)
        tokens_out = int(data.get("eval_count") or 0) or estimate_tokens(text)
        metered = bool(data.get("prompt_eval_count")) and bool(data.get("eval_count"))

        raw: dict = {
            "provider": "ollama",
            "endpoint": url,
            "model_output": text,
            "thinking": data.get("thinking"),
            "prompt_eval_count": data.get("prompt_eval_count"),
            "eval_count": data.get("eval_count"),
            "total_duration_ns": data.get("total_duration"),
            "load_duration_ns": data.get("load_duration"),
            "metered": metered,
        }

        parsed = _extract_json(text)
        if parsed is None:
            # Salvage: keep the (real) latency/token measurements, surface the
            # unparseable text so the run still counts in the benchmark.
            narrative = f"[unparseable model output] {_THINK_RE.sub('', text).strip()[:600]}"
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
            cost_usd=0.0,  # local inference is free
            latency_s=round(latency, 3),
            raw=raw,
        )
