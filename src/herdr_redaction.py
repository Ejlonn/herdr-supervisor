"""The single redaction policy for everything that leaves the supervisor: Telegram text, registered Markdown
derivatives, artifact previews, status lines. Conservative by design: secrets become `[redacted]`, control
characters are removed, output is bounded, and `has_sensitive_remainder` fails closed on anything that still
looks like credential material after redaction. Depends on nothing else in the project."""

from __future__ import annotations

import re
from typing import Any

CHUNK = 3800
REDACTED = "[redacted]"

_SECRET_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"https?://api\.telegram\.org/(?:file/)?bot[^/\s]+", re.IGNORECASE),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    # header-style credentials redact the rest of the line, whichever separator a log used
    re.compile(r"(?i)\b(cookie|authorization|x-api-key)\s*[:=]\s*[^\n]{6,}"),
    re.compile(r"(?i)(?:['\"]?(?:api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret|access[_-]?key|private[_-]?key|aws[_-]?access[_-]?key[_-]?id|aws[_-]?secret[_-]?access[_-]?key|github[_-]?token|telegram[_-]?bot[_-]?token|database[_-]?url|connection[_-]?string|dsn|cookie|authorization)['\"]?\s*[:=]\s*)(?:['\"][^'\"\r\n]+['\"]|[^\s,;}\]\r\n]+)"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----[\s\S]*?-----END [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:sk|xox[bpsa]|ghp|gho)[-_][A-Za-z0-9_-]{16,}"),
    # userinfo in any URL/DSN scheme (postgres://, redis://, amqp://, https://, ...)
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@[^\s]+"),
    re.compile(r"(?i)(^|[\s/])(\.env(\.[a-z]+)?|id_rsa|id_ed25519|\.netrc|credentials\.json|bot-token)\b[^\n]*"),
]
_SENSITIVE_REMAINDER_PATTERNS = [
    re.compile(r"(?i)(?:['\"]?(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|aws[_-]?secret[_-]?access[_-]?key)['\"]?\s*[:=])"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
]
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


def redact(text: Any, *, limit: int = CHUNK) -> str:
    """Conservative defence-in-depth redaction for anything rendered to Telegram or a derivative."""
    value = str(text) if text is not None else ""
    value = CONTROL_RE.sub("", value)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def has_sensitive_remainder(text: str) -> bool:
    """Conservative artifact-only check after redaction; uncertainty blocks document exposure."""
    return any(pattern.search(text) for pattern in _SENSITIVE_REMAINDER_PATTERNS)


def credential_value_present(text: Any) -> bool:
    """True when the text carries what looks like an actual credential VALUE (a bearer/basic token, a
    key=value or header-style secret, PEM material, a DSN/URL with userinfo, a bot token, a cloud key id).
    Prose about secret handling ("how is the bot token read?") has no value part and passes. Used to refuse
    input before it is persisted or forwarded to a provider — redaction of output is not a substitute."""
    value = CONTROL_RE.sub("", str(text) if text is not None else "")
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS) or has_sensitive_remainder(value)




__all__ = ["CHUNK", "CONTROL_RE", "REDACTED", "redact", "has_sensitive_remainder", "credential_value_present"]
