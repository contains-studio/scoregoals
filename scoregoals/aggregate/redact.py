"""Redact secrets/PII from text before storage or LLM submission.

Everything captured by screen OCR can contain passwords, API keys, OTP codes,
account numbers. Redaction runs BEFORE the timeline is persisted or sent to
any backend (including the local one — defense in depth).

Pure + deterministic: stdlib `re` only, no network, same input -> same output.
"""

from __future__ import annotations

import copy
import re

from ..models import ActivityRecord, DayTimeline

# --- patterns, applied in order ----------------------------------------------

# 1. PEM private-key blocks (and a dangling BEGIN line with no END captured).
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_PRIVATE_KEY_LINE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[^\n]*")

# 2. Well-known API-key shapes.
_KNOWN_KEYS = re.compile(
    r"""
    \b(
        sk-[A-Za-z0-9_-]{16,}                 # OpenAI / Anthropic (sk-, sk-proj-, sk-ant-)
      | gh[pousr]_[A-Za-z0-9]{20,}            # GitHub ghp_/gho_/ghu_/ghs_/ghr_
      | github_pat_[A-Za-z0-9_]{20,}          # GitHub fine-grained PAT
      | xox[baprs]-[A-Za-z0-9-]{10,}          # Slack tokens
      | (?:AKIA|ASIA)[0-9A-Z]{16}             # AWS access key id
      | AIza[0-9A-Za-z_-]{30,40}              # Google API key
    )\b
    """,
    re.VERBOSE,
)

# 3. JWTs (three base64url segments).
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

# 4. Bearer <token>.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{16,}=*")

# 5. .env-style KEY=VALUE where the key looks secret (…_KEY=, …_TOKEN=, …).
_ENV_SECRET = re.compile(
    r"\b((?:[A-Z][A-Z0-9]*_)+(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIALS?|AUTH))"
    r"\s*=\s*([\"']?)[^\s\"']{4,}"
)

# 6. "password: hunter2" / "token = abc…" prose or config lines.
_KEYWORD_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[ _-]?key|apikey"
    r"|access[ _-]?key|client[ _-]?secret)\b(\s*[:=]\s*)([\"']?)[^\s\"']{4,}"
)

# 7. Long hex blobs (48+ so 40-hex git commit SHAs survive).
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{48,}\b")

# 8. Long base64-ish blobs. Pure-hex runs shorter than the hex threshold are
#    left alone (40-hex git commit SHAs are common, public, and harmless).
_LONG_B64 = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,3}")
_PURE_HEX = re.compile(r"[0-9a-fA-F]+\Z")

# 9. SSN shape.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# 10. Credit-card-shaped digit runs (validated with Luhn in a callback).
_CARDISH = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# 11. 6-digit OTP codes in OTP-ish contexts.
_OTP = re.compile(r"(?i)\b(code|otp|verification|passcode|2fa)\b(\D{0,24}?)\b\d{6}\b")


def _luhn_ok(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_card(m: re.Match) -> str:
    digits = re.sub(r"[ -]", "", m.group(0))
    return "[REDACTED:card]" if _luhn_ok(digits) else m.group(0)


def _redact_b64(m: re.Match) -> str:
    blob = m.group(0)
    if _PURE_HEX.fullmatch(blob) and len(blob) < 48:
        return blob  # e.g. a 40-hex git commit SHA — public, not a secret
    return "[REDACTED:blob]"


def redact_text(text: str) -> str:
    """Return `text` with sensitive spans replaced by [REDACTED:<tag>]."""
    if not text:
        return text
    out = text
    out = _PRIVATE_KEY_BLOCK.sub("[REDACTED:private-key]", out)
    out = _PRIVATE_KEY_LINE.sub("[REDACTED:private-key]", out)
    out = _KNOWN_KEYS.sub("[REDACTED:api-key]", out)
    out = _JWT.sub("[REDACTED:jwt]", out)
    out = _BEARER.sub(r"\1 [REDACTED:token]", out)
    out = _ENV_SECRET.sub(r"\1=\2[REDACTED:env]", out)
    out = _KEYWORD_VALUE.sub(r"\1\2\3[REDACTED:credential]", out)
    out = _LONG_HEX.sub("[REDACTED:hex]", out)
    out = _LONG_B64.sub(_redact_b64, out)
    out = _SSN.sub("[REDACTED:ssn]", out)
    out = _CARDISH.sub(_redact_card, out)
    out = _OTP.sub(r"\1\2[REDACTED:otp]", out)
    return out


def _redact_record(rec: ActivityRecord) -> None:
    rec.text = redact_text(rec.text)
    if rec.title:
        rec.title = redact_text(rec.title)


def redact_timeline(tl: DayTimeline) -> DayTimeline:
    """Return a redacted copy of `tl`: redact_text applied to every session
    text_excerpt/summary/title and every ActivityRecord text/title in
    calendar/github/meetings. The input timeline is not mutated."""
    clean = copy.deepcopy(tl)
    for sess in clean.sessions:
        sess.text_excerpt = redact_text(sess.text_excerpt)
        if sess.summary:
            sess.summary = redact_text(sess.summary)
        if sess.title:
            sess.title = redact_text(sess.title)
    for rec in (*clean.calendar, *clean.github, *clean.meetings):
        _redact_record(rec)
    return clean
