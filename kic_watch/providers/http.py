"""HTTP session and retry/backoff policy for public data providers."""
from __future__ import annotations
import os
import time
from typing import Any, Optional
import requests
from dotenv import load_dotenv
from kic_watch.security import safe_error

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
                last_error = "HTTP {} from {}".format(response.status_code, response.url)
                try:
                    wait = min(float(response.headers.get("Retry-After", 2 ** attempt)), 8)
                except (TypeError, ValueError):
                    wait = min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = safe_error(exc)
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("API 요청 실패: {} ({})".format(url, last_error))

