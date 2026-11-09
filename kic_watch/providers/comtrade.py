"""UN Comtrade public preview API adapter."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from kic_watch.providers.http import _get_json


def _comtrade(query: str) -> dict:
    hs = query or "8542"
    year = datetime.now(timezone.utc).year - 1
    params = {"reporterCode": "842", "cmdCode": hs, "period": "{},{}".format(year - 2, year - 1),
              "flowCode": "M", "partnerCode": "0", "motCode": "0", "aggregateBy": "6", "breakdownMode": "classic", "partner2Code": "0"}
    if os.getenv("COMTRADE_API_KEY"):
        params["subscription-key"] = os.environ["COMTRADE_API_KEY"]
    payload = _get_json("https://comtradeapi.un.org/public/v1/preview/C/A/HS", params)
    rows = payload.get("data", [])
    rows = sorted(rows, key=lambda o: int(o.get("period", 0)))
    if not rows:
        raise RuntimeError("Comtrade 응답에 교역값이 없습니다. HS 코드를 확인하세요.")
    latest = rows[-1]
    value = float(latest.get("primaryValue") or latest.get("netWgt") or 0)
    series = [{"date": str(o.get("period")), "value": float(o.get("primaryValue") or 0) / 1e9} for o in rows]
    prev = float(rows[-2].get("primaryValue") or 0) if len(rows) > 1 else None
    growth = None if not prev else (value / prev - 1) * 100
    return {"value": growth, "level": value, "date": str(latest.get("period")), "period_key": str(latest.get("period")), "unit": "% YoY",
            "series": series, "detail": "미국 수입 HS {} 세계 전체, Comtrade primaryValue 전년 대비".format(hs)}

