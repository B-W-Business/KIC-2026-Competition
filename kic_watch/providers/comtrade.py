"""UN Comtrade public preview API adapter."""
from __future__ import annotations
import os
import time
import math
from datetime import datetime, timezone
from kic_watch.security import safe_error
from kic_watch.providers.http import _get_json


def _comtrade(query: str) -> dict:
    hs = query or "8542"
    latest_target_year = datetime.now(timezone.utc).year - 1
    requested_years = (latest_target_year - 1, latest_target_year)
    base_params = {"reporterCode": "842", "cmdCode": hs,
              "flowCode": "M", "partnerCode": "0"}
    keys = list(dict.fromkeys(value.strip() for name in ("COMTRADE_API_KEY", "COMTRADE_SECONDARY_API_KEY")
                              if (value := os.getenv(name, "").strip())))
    # Public API can be called keyless; when credentials are configured, try
    # primary then secondary before surfacing a sanitized provider error.
    key_candidates = keys or [None]
    errors = []
    rows_by_year = {}
    for index, requested_year in enumerate(requested_years):
        if index:
            # Leave room for the public API's per-second quota and short burst limits.
            time.sleep(2.1)
        payload = None
        for api_key in key_candidates:
            request_params = {**base_params, "period": str(requested_year)}
            if api_key:
                request_params["subscription-key"] = api_key
            try:
                candidate = _get_json("https://comtradeapi.un.org/public/v1/preview/C/A/HS", request_params)
                if candidate.get("error"):
                    raise RuntimeError(candidate["error"])
                payload = candidate
                break
            except Exception as exc:
                errors.append(safe_error(exc))
        if payload is None:
            raise RuntimeError("UN Comtrade {}년 요청 실패: {}".format(requested_year, " | ".join(errors)))
        yearly_rows = payload.get("data", [])
        if not yearly_rows:
            raise RuntimeError("Comtrade 응답에 {}년 교역값이 없습니다. HS 코드를 확인하세요.".format(requested_year))
        raw_values = [row.get("primaryValue") for row in yearly_rows]
        if any(value in (None, "") for value in raw_values):
            raise RuntimeError("Comtrade {}년 응답에 금액(primaryValue)이 없어 무게(netWgt)로 대체하지 않았습니다.".format(requested_year))
        values = [float(value) for value in raw_values]
        if any(not math.isfinite(value) for value in values):
            raise RuntimeError("Comtrade {}년 응답에 유효하지 않은 금액(primaryValue)이 포함되어 있습니다.".format(requested_year))
        rows_by_year[requested_year] = sum(values)
    previous_year, latest_year = sorted(rows_by_year)
    previous_value, value = rows_by_year[previous_year], rows_by_year[latest_year]
    growth = None if not previous_value else (value / previous_value - 1) * 100
    # The headline observation is YoY percent growth; the chart series are trade-value levels.
    # Preserve that distinction so the UI does not label USD billions as percent.
    series = [{"date": str(period), "value": amount / 1e9} for period, amount in sorted(rows_by_year.items())]
    return {"value": growth, "level": value, "date": str(latest_year), "period_key": str(latest_year), "unit": "% YoY",
            "series": series, "series_label": "미국 수입액 · HS {}".format(hs), "series_unit": "USD billion",
            "detail": "미국 수입 HS {} 세계 전체, Comtrade primaryValue 전년 대비".format(hs)}

