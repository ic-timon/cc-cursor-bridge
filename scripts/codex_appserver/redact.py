"""Redact secrets in logs without erasing token-usage metrics.

Keys whose names are usage/metric fields (inputTokens, total_tokens, ...) are
kept as numbers. Keys that look like credentials are replaced. String values
matching common token patterns are redacted. This corrects the 2026-11-11
experiment logger, which treated any key containing 'token' as a secret and
erased usage counts.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Mapping, Sequence

REDACTED = "[redacted]"

# Credential-ish key names. `token` alone is not enough (see usage keys).
SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|authorization|credential|cookie|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|private[_-]?key|"
    r"chatgpt[_-]?auth|auth[_-]?token|bearer)$",
    re.I,
)
# Explicit allowlist of usage/metric keys (camel or snake, any nesting).
USAGE_KEY_RE = re.compile(
    r"(^(total|input|output|cached|cache_?write|reasoning(_?output)?)_?tokens$"
    r"|^token_?usage$|^tokens_used$|^model_?context_?window$|"
    r"^cache_?write_?input_?tokens$)",
    re.I,
)

SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gh[ousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]+"
    r"|bearer\s+[A-Za-z0-9._\-]+)",
    re.I,
)

# Never log these env values if provided as an extra secret list.
DEFAULT_SECRET_ENV = (
    "CC_BRIDGE_CURSOR_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CHATGPT_ACCESS_TOKEN",
)


def _is_usage_key(key: str) -> bool:
    compact = key.replace("-", "_")
    return USAGE_KEY_RE.search(compact) is not None or USAGE_KEY_RE.search(key) is not None


def _is_secret_key(key: str) -> bool:
    if _is_usage_key(key):
        return False
    # auth.json-style keys, tokens that are not usage
    if SECRET_KEY_RE.search(key):
        return True
    lowered = key.lower()
    if lowered in {"authorization", "token", "auth", "api_key", "apikey"}:
        return True
    return False


def redact_text(text: str, extra_secrets: Sequence[str] = ()) -> str:
    out = SECRET_VALUE_RE.sub(REDACTED, text)
    for secret in extra_secrets:
        if secret and secret in out:
            out = out.replace(secret, REDACTED)
    return out


def secrets_from_environ(environ: Mapping[str, str]) -> List[str]:
    values: List[str] = []
    for name in DEFAULT_SECRET_ENV:
        value = environ.get(name) or ""
        if value:
            values.append(value)
    return values


def redact(obj: Any, extra_secrets: Sequence[str] = ()) -> Any:
    if isinstance(obj, Mapping):
        out = {}
        for key, value in obj.items():
            key_s = str(key)
            if _is_secret_key(key_s):
                out[key] = REDACTED
            elif _is_usage_key(key_s):
                # Recurse into usage objects, but keep numeric leaves intact
                # even if nested keys include the word "token".
                out[key] = _redact_usage(value, extra_secrets)
            else:
                out[key] = redact(value, extra_secrets)
        return out
    if isinstance(obj, list):
        return [redact(item, extra_secrets) for item in obj]
    if isinstance(obj, tuple):
        return type(obj)(redact(item, extra_secrets) for item in obj)
    if isinstance(obj, str):
        return redact_text(obj, extra_secrets)
    return obj


def _redact_usage(value: Any, extra_secrets: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        out = {}
        for key, inner in value.items():
            if _is_secret_key(str(key)) and not _is_usage_key(str(key)):
                out[key] = REDACTED
            else:
                out[key] = _redact_usage(inner, extra_secrets)
        return out
    if isinstance(value, list):
        return [_redact_usage(item, extra_secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, extra_secrets)
    return value
