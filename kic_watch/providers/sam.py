"""SAM.gov contract opportunity API adapter."""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from kic_watch.models import now_iso
from kic_watch.providers.http import _get_json


def _sam(query: str) -> dict:
    api_key = os.getenv("SAM_API_KEY")
    if not api_key:
        raise RuntimeError("SAM_API_KEY가 필요합니다. SAM.gov 공개 API 키를 설정하세요.")
    posted_to = datetime.now().date()
    # SAM validates calendar-year ranges; stay safely inside the one-year cap.
    posted_from = posted_to - timedelta(days=360)
    params = {"api_key": api_key, "limit": 100,
              "postedFrom": posted_from.strftime("%m/%d/%Y"),
              "postedTo": posted_to.strftime("%m/%d/%Y")}
    if query:
        params["title"] = query
    payload = _get_json("https://api.sam.gov/opportunities/v2/search", params)
    total = int(payload.get("totalRecords", 0))
    return {"value": float(total), "date": now_iso()[:10], "period_key": "", "unit": "건", "series": [],
            "detail": "SAM.gov 관련 계약 기회 검색 결과 {}건 (수주/award가 아닌 기회 공고 수)".format(total)}

