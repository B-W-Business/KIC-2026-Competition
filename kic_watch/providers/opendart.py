"""OpenDART issuer filings and annual financial statements for Korean companies."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
import os
from typing import Optional

from kic_watch.models import COMPANIES, now_iso
from kic_watch.providers.http import _get_json

BASE_URL = "https://opendart.fss.or.kr/api"
ANNUAL_REPORT = "11011"


def _api_key() -> str:
    key = os.getenv("OPENDART_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENDART_API_KEY가 없습니다. OpenDART에서 무료 인증키를 발급받아 .env에 설정하세요.")
    return key


@lru_cache(maxsize=64)
def _annual_accounts(corp_code: str, year: int) -> tuple:
    payload = _get_json(BASE_URL + "/fnlttSinglAcntAll.json", {
        "crtfc_key": _api_key(), "corp_code": corp_code,
        "bsns_year": str(year), "reprt_code": ANNUAL_REPORT, "fs_div": "CFS",
    })
    if payload.get("status") == "000":
        return tuple(payload.get("list", []))
    # A valid key with no report for that fiscal year is normal (e.g. current year).
    if payload.get("status") == "013":
        return ()
    raise RuntimeError("OpenDART 재무 API 오류 {}: {}".format(payload.get("status", "?"), payload.get("message", "알 수 없는 오류")))


def _amount(row: dict, key: str = "thstrm_amount") -> Optional[float]:
    raw = str(row.get(key, "")).replace(",", "").strip()
    if not raw or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _account(rows: tuple, kind: str) -> Optional[dict]:
    # Prefer standardized IFRS account IDs, then Korean account names.
    ids = {
        "revenue": ("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"),
        "gross_profit": ("ifrs-full_GrossProfit",),
        "capex": ("ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",),
    }[kind]
    named = {
        "revenue": ("매출액", "수익(매출액)", "영업수익"),
        "gross_profit": ("매출총이익",),
        "capex": ("유형자산의 취득", "유형자산 취득", "유형자산의 취득으로 인한 현금유출"),
    }[kind]
    statement = "CF" if kind == "capex" else "IS"
    candidates = [row for row in rows if _amount(row) is not None and row.get("sj_div") == statement]
    for account_id in ids:
        found = next((r for r in candidates if r.get("account_id") == account_id), None)
        if found:
            return found
    for name in named:
        found = next((r for r in candidates if (r.get("account_nm") or "").strip() == name), None)
        if found:
            return found
    return None


def _year_rows(corp_code: str, max_years: int = 7) -> list[tuple[int, tuple]]:
    year = date.today().year
    found = []
    for candidate_year in range(year, year - max_years, -1):
        rows = _annual_accounts(corp_code, candidate_year)
        if rows:
            found.append((candidate_year, rows))
            if len(found) == 6:
                break
    return found


def company_fact(ticker: str, metric: str) -> dict:
    company = COMPANIES[ticker]
    corp_code = company["corp_code"]
    yearly = _year_rows(corp_code)
    if not yearly:
        raise RuntimeError("OpenDART에 최근 연결 사업보고서 재무자료가 없습니다.")
    year, rows = yearly[0]
    url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
    if metric == "gross_margin":
        gross, revenue = _account(rows, "gross_profit"), _account(rows, "revenue")
        gross_value, revenue_value = _amount(gross) if gross else None, _amount(revenue) if revenue else None
        if gross_value is None or not revenue_value:
            raise RuntimeError("OpenDART에서 같은 연도의 매출총이익·매출 계정을 찾지 못했습니다.")
        value = 100 * gross_value / revenue_value
        detail = "{}년 연결 재무제표: 매출총이익 {} / 매출 {}".format(year, gross.get("account_nm"), revenue.get("account_nm"))
    else:
        kind = "capex" if metric == "capex_growth" else "revenue"
        current_row = _account(rows, kind)
        current = _amount(current_row) if current_row else None
        if current is None:
            raise RuntimeError("OpenDART에서 {} 계정을 찾지 못했습니다.".format("설비투자" if kind == "capex" else "매출"))
        if len(yearly) < 2:
            raise RuntimeError("연간 성장률 계산에 필요한 전년도 사업보고서가 없습니다.")
        previous_year, previous_rows = yearly[1]
        previous_row = _account(previous_rows, kind)
        previous = _amount(previous_row) if previous_row else None
        if previous is None or previous == 0:
            raise RuntimeError("전년도 동일 계정이 없어 성장률을 계산할 수 없습니다.")
        value = (current / previous - 1) * 100
        detail = "연결 {} 연간 계정 비교: {}년 {} / {}년 {}".format(
            "설비투자" if kind == "capex" else "매출", year,
            current_row.get("account_nm", ""), previous_year,
            previous_row.get("account_nm", ""))
    series = []
    ordered_years = list(reversed(yearly))
    kind = "gross_profit" if metric == "gross_margin" else ("capex" if metric == "capex_growth" else "revenue")
    for index, (series_year, series_rows) in enumerate(ordered_years):
        numerator_row = _account(series_rows, kind)
        numerator = _amount(numerator_row) if numerator_row else None
        if metric == "gross_margin":
            revenue_row = _account(series_rows, "revenue")
            revenue = _amount(revenue_row) if revenue_row else None
            rate = 100 * numerator / revenue if numerator is not None and revenue else None
        elif index > 0:
            previous_rows = ordered_years[index - 1][1]
            previous_row = _account(previous_rows, kind)
            previous = _amount(previous_row) if previous_row else None
            rate = (numerator / previous - 1) * 100 if numerator is not None and previous else None
        else:
            rate = None
        if rate is not None:
            series.append({"date": "{}-12-31".format(series_year), "value": rate})
    receipt = next((row.get("rcept_no") for row in rows if row.get("rcept_no")), "")
    source_url = ("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt) if receipt else url
    series_label = {"revenue_growth": "매출 연간 성장률", "gross_margin": "연간 매출총이익률",
                    "capex_growth": "설비투자 연간 성장률"}[metric]
    return {"value": value, "date": "{}-12-31".format(year), "period_key": "FY{}".format(year),
            "unit": "%", "series": series, "series_label": series_label, "series_unit": "%",
            "url": source_url, "detail": detail + " · 연결 연간 계정으로 계산"}


def scan_company(ticker: str, assumptions: list) -> dict:
    company = COMPANIES[ticker]
    end = date.today()
    start = end - timedelta(days=365)
    payload = _get_json(BASE_URL + "/list.json", {
        "crtfc_key": _api_key(), "corp_code": company["corp_code"],
        "bgn_de": start.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
        "page_count": "100", "sort": "date", "sort_mth": "desc",
    })
    if payload.get("status") == "013":
        rows = []
    elif payload.get("status") != "000":
        raise RuntimeError("OpenDART 공시 API 오류 {}: {}".format(payload.get("status", "?"), payload.get("message", "알 수 없는 오류")))
    else:
        rows = payload.get("list", [])
    events, matched = [], []
    for item in rows[:30]:
        row = {"accession": item.get("rcept_no", ""), "filing_date": item.get("rcept_dt", ""),
               "form": item.get("report_nm", "공시"), "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + item.get("rcept_no", ""),
               "claim": "", "evidence_quote": "", "numeric_claim": None, "claim_metric": "unknown",
               "claim_unit": "", "claim_period": ""}
        events.append(row)
        report_name = row["form"]
        if any(term in report_name for term in ("사업보고서", "분기보고서", "반기보고서")):
            for assumption in assumptions:
                if assumption.ticker == ticker and assumption.enabled:
                    matched.append({**row, "assumption_id": assumption.id, "assumption_title": assumption.title,
                                    "related": True, "reason": "정기 재무 공시 감지 · 원문 주장 자동 추출은 미지원"})
    return {"events": events, "matched": matched, "warnings": [], "checked_at": now_iso(),
            "source_warning": "OpenDART 공시목록·정기 재무자료를 사용합니다. 공시 본문의 주장 문장 자동 추출은 아직 지원하지 않으며, 수치 검증은 연간 연결 재무자료 기준입니다."}
