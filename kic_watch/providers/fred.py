"""FRED time-series API adapter."""
from __future__ import annotations
import os
from kic_watch.providers.http import _get_json


def _fred(query: str) -> dict:
    if not query:
        raise RuntimeError("FRED series ID가 필요합니다 (예: DGS10, INDPRO).")
    params = {"series_id": query, "file_type": "json", "sort_order": "desc", "limit": 1000}
    if os.getenv("FRED_API_KEY"):
        params["api_key"] = os.environ["FRED_API_KEY"]
    else:
        raise RuntimeError("FRED_API_KEY가 필요합니다. 키 없이 사용할 수 있는 공공 API 엔드포인트가 없습니다.")
    payload = _get_json("https://api.stlouisfed.org/fred/series/observations", params)
    observations = [o for o in payload.get("observations", []) if o.get("value") not in (None, ".")]
    if not observations:
        raise RuntimeError("FRED 관측값이 없습니다. series ID를 확인하세요.")
    observations.sort(key=lambda o: o["date"])
    latest = observations[-1]
    series = [{"date": o["date"], "value": float(o["value"])} for o in observations[-36:]]
    return {"value": float(latest["value"]), "date": latest["date"], "period_key": latest["date"], "unit": "FRED 원자료", "series": series,
            "detail": "FRED series {} 최근 관측치".format(query)}

