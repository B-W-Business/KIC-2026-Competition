"""Credential-safe error handling for provider and agent logs."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "crtfc_key", "subscription-key", "subscription_key", "key", "token", "access_token"}


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = urlencode([(k, "[REDACTED]" if k.lower() in SENSITIVE_QUERY_KEYS else v)
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        query = query.replace("%5BREDACTED%5D", "[REDACTED]")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except Exception:
        return re.sub(r"(?i)(api[_-]?key|subscription-key|token)=([^&\s]+)", r"\1=[REDACTED]", str(url))


def safe_error(error: object) -> str:
    """Redact credentials embedded in request URLs before persistence or display."""
    value = str(error)
    value = re.sub(r"https?://[^\s'\"]+", lambda match: redact_url(match.group(0)), value)
    value = re.sub(r"(?i)(api[_-]?key|subscription-key|token)=([^&\s]+)", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)\b(sk|e2b)_[A-Za-z0-9_-]{12,}", r"\1_[REDACTED]", value)
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]{12,}", r"\1[REDACTED]", value)
    return value[:1500]
