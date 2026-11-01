"""Scrub secrets and volatile identifiers out of log text.

Two separate jobs:

* :func:`redact` — runs before any log text enters a prompt or a PR body.
  Spark drivers routinely echo JDBC URLs, bearer tokens and signed GCS URLs
  into stderr; none of that should leave the process.
* :func:`normalise_for_fingerprint` — strips the parts of a log that vary
  between runs (timestamps, container IDs, paths with dates) so the *same*
  recurring bug hashes to the same fingerprint.
"""

from __future__ import annotations

import re

_REDACTION = "«redacted»"

# Ordered: more specific patterns first, since substitutions are applied in turn.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic / OpenAI style keys
    ("api-key", re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{16,}", re.I)),
    # GitHub tokens
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    # AWS access key IDs / secrets
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    # Google API keys (canonical length is 35 trailing chars; stay tolerant)
    ("gcp-key", re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")),
    # JWTs / OAuth bearer tokens
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # Ordered so 'Authorization: Bearer <tok>' is consumed whole — matching
    # 'authorization:' alone would stop at 'Bearer' and leave the token behind.
    ("bearer", re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer\s+)?\S+")),
    ("bearer-bare", re.compile(r"(?i)\bbearer\s+\S+")),
    # PEM private key blocks
    (
        "private-key",
        re.compile(r"-----BEGIN[^-]{0,40}PRIVATE KEY-----.*?-----END[^-]{0,40}PRIVATE KEY-----", re.S),
    ),
    # Credentials embedded in URIs: scheme://user:password@host
    ("uri-credentials", re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)(?=@)")),
    # key=value / key: value pairs whose key smells secret
    (
        "secret-kv",
        re.compile(
            r"(?i)\b([\w.\-]*(?:password|passwd|secret|token|apikey|api_key|credential|private_key)[\w.\-]*)"
            r"\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)"
        ),
    ),
    # GCS/S3 signed URLs
    ("signed-url", re.compile(r"(?i)([?&](?:X-Goog-Signature|X-Amz-Signature|Signature)=)[^&\s]+")),
]


def redact(text: str) -> str:
    """Remove credential-shaped substrings from ``text``."""
    if not text:
        return text

    out = text
    for name, pattern in _SECRET_PATTERNS:
        if name == "uri-credentials":
            out = pattern.sub(lambda m: f"{m.group(1)}:{_REDACTION}", out)
        elif name == "secret-kv":
            out = pattern.sub(lambda m: f"{m.group(1)}={_REDACTION}", out)
        elif name == "signed-url":
            out = pattern.sub(lambda m: f"{m.group(1)}{_REDACTION}", out)
        else:
            out = pattern.sub(_REDACTION, out)
    return out


def redaction_findings(text: str) -> list[str]:
    """Names of the patterns that matched. Used for logging and tests."""
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(text or "")]


# --- fingerprinting ---------------------------------------------------------

_VOLATILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
    # Spark's own log4j default: 'YY/MM/DD HH:MM:SS'
    re.compile(r"\b\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\b"),
    re.compile(r"\b\d{2}/\d{2}/\d{2}\b"),
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),
    re.compile(r"\bapplication_\d+_\d+\b"),
    re.compile(r"\bcontainer_(?:e\d+_)?\d+_\d+_\d+_\d+\b"),
    re.compile(r"\battempt_\d+_\d+_[a-z]_\d+_\d+\b"),
    re.compile(r"\btask \d+\.\d+ in stage \d+\.\d+\b", re.I),
    re.compile(r"\bTID \d+\b"),
    re.compile(r"\bexecutor \d+\b", re.I),
    re.compile(r"\bpartition \d+\b", re.I),
    re.compile(r"\b(?:job|stage|attempt) \d+(?:\.\d+)?\b", re.I),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"),
    re.compile(r"\bdt=\d{4}-?\d{2}-?\d{2}\b"),
    re.compile(r"\b0x[0-9a-f]{6,}\b", re.I),
]

# A Spark log line puts the level after a timestamp ("26/07/22 03:00:11 INFO ..."),
# so the level cannot be anchored to the start of the line.
_LOW_LEVEL = re.compile(r"\b(?:INFO|DEBUG|TRACE)\b")
_SIGNAL = re.compile(
    r"\b(?:ERROR|WARN|FATAL|Exception|Error|Caused by|Traceback|SQLSTATE)\b|^\s+at\s"
)


def _is_noise(line: str) -> bool:
    """An INFO/DEBUG line with no error signal in it carries nothing we hash."""
    return bool(_LOW_LEVEL.search(line)) and not _SIGNAL.search(line)


def normalise_for_fingerprint(text: str) -> str:
    """Collapse run-specific detail so recurring failures hash identically.

    Keeps only lines carrying error signal, then blanks out timestamps,
    container IDs, UUIDs, IPs and hex addresses.
    """
    if not text:
        return ""

    kept = [
        line.strip() for line in text.splitlines() if line.strip() and not _is_noise(line)
    ]

    out = "\n".join(kept)
    for pattern in _VOLATILE_PATTERNS:
        out = pattern.sub("§", out)
    return re.sub(r"\s+", " ", out).strip()
