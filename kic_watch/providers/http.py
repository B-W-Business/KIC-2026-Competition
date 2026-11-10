"""HTTP session and retry/backoff policy for public data providers."""
from __future__ import annotations
import os
import time
from typing import Any, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KIC-Investment-Assumption-Watch/1.0", "Accept-Encoding": "gzip, deflate"})

def _get_json(url: str, params: Optional[dict] = None, timeout: int = 25,
              headers: Optional[dict] = None) -> Any:
    """GET with bounded exponential backoff for transient and rate-limit errors."""
    last_error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, params=params, timeout=timeout, headers=headers)
            if response.status_code in (429, 500, 502, 503, 504):
                # Never persist response.url: provider query parameters can contain API credentials.
                last_error = "HTTP {}".format(response.status_code)
                try:
                    wait = min(float(response.headers.get("Retry-After", 2 ** attempt)), 8)
                except (TypeError, ValueError):
                    wait = min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            # A 403/404 is a definitive access/resource response, not a transient outage.
            # Repeating denied SEC requests can worsen fair-access restrictions.
            status = getattr(getattr(exc, "response", None), "status_code", getattr(response, "status_code", "HTTP error"))
            raise RuntimeError("API 요청 실패: {} ({})".format(url.split("?", 1)[0], status)) from exc
        except (requests.RequestException, ValueError) as exc:
            # Requests exceptions often embed the full URL, including secret query parameters.
            # Surface only a sanitized exception class; detailed networking traces stay out of UI/logs.
            last_error = type(exc).__name__
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("API 요청 실패: {} ({})".format(url, last_error))

