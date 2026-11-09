"""Evidence-first KIC portfolio assumption monitor.

All calculations are deterministic Python. The optional LLM only classifies filing
relevance and extracts a cited claim; it never supplies observed financial values.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

import requests

COMPANIES = {
    "NVDA": {"name": "NVIDIA Corporation", "cik": "0001045810"},
    "AMD": {"name": "Advanced Micro Devices, Inc.", "cik": "0000002488"},
    "AVGO": {"name": "Broadcom Inc.", "cik": "0001730168"},
    "INTC": {"name": "Intel Corporation", "cik": "0000050863"},
    "TSM": {"name": "Taiwan Semiconductor Manufacturing Company Limited", "cik": "0001046179"},
    "ASML": {"name": "ASML Holding N.V.", "cik": "0000937966"},
    "QCOM": {"name": "QUALCOMM Incorporated", "cik": "0000804328"},
    "TXN": {"name": "Texas Instruments Incorporated", "cik": "0000097476"},
    "MU": {"name": "Micron Technology, Inc.", "cik": "0000723125"},
    "AMAT": {"name": "Applied Materials, Inc.", "cik": "0000006951"},
    "LRCX": {"name": "Lam Research Corporation", "cik": "0000707549"},
    "KLAC": {"name": "KLA Corporation", "cik": "0000319201"},
    "MSFT": {"name": "Microsoft Corporation", "cik": "0000789019"},
    "GOOGL": {"name": "Alphabet Inc.", "cik": "0001652044"},
    "AMZN": {"name": "Amazon.com, Inc.", "cik": "0001018724"},
    "META": {"name": "Meta Platforms, Inc.", "cik": "0001326801"},
    "ORCL": {"name": "Oracle Corporation", "cik": "0001341439"},
    "IBM": {"name": "International Business Machines Corporation", "cik": "0000051143"},
    "DELL": {"name": "Dell Technologies Inc.", "cik": "0001571996"},
    "SMCI": {"name": "Super Micro Computer, Inc.", "cik": "0001375365"},
}

METRICS = {
    "revenue_growth": "Revenue growth",
    "gross_margin": "Gross margin",
    "capex_growth": "Capital expenditure growth",
    "fred_series": "FRED time series",
    "trade_value": "UN Comtrade trade value",
    "sam_awards": "SAM.gov procurement opportunities",
}


def metric_unit(metric: str) -> str:
    if metric in ("revenue_growth", "gross_margin", "capex_growth", "trade_value"):
        return "%"
    if metric == "sam_awards":
        return "건"
    return "series 단위"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Assumption:
    id: str
    ticker: str
    title: str
    memo: str
    metric: str
    source: str
    baseline: float
    target: float
    tolerance: float
    query: str
    created_at: str

    @classmethod
    def create(cls, **kwargs: Any) -> "Assumption":
        return cls(id=str(uuid.uuid4())[:8], created_at=now_iso(), **kwargs)


class ReviewState(TypedDict, total=False):
    assumption: Dict[str, Any]
    event: Dict[str, Any]
    observation: Dict[str, Any]
    report: Dict[str, Any]


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": os.getenv("SEC_USER_AGENT", "KIC Research research@example.org"), "Accept-Encoding": "gzip, deflate"})


def _get_json(url: str, params: Optional[dict] = None, timeout: int = 25) -> Any:
    """GET with bounded exponential backoff for transient and rate-limit errors."""
    last_error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("API 요청 실패: {} ({})".format(url, last_error))


def seed_assumptions() -> List[Assumption]:
    """Small illustrative baselines; clearly marked examples, not KIC holdings."""
    return [
        Assumption.create(ticker="NVDA", title="AI 가속기 수요가 매출 성장으로 이어짐 (예시)",
                          memo="데모용 예시 기준선입니다. 실제 투자 메모로 교체해야 합니다.",
                          metric="revenue_growth", source="SEC Company Facts", baseline=20,
                          target=25, tolerance=10, query=""),
        Assumption.create(ticker="TSM", title="반도체 공급망 교역 흐름을 추적 (예시)",
                          memo="데모용 예시입니다. Comtrade HS 코드와 교역 상대국을 직접 지정하세요.",
                          metric="trade_value", source="UN Comtrade", baseline=0,
                          target=10, tolerance=20, query="8542"),
    ]


def _filing_rows(recent: dict) -> List[dict]:
    rows = []
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form not in ("8-K", "10-Q", "10-K"):
            continue
        accession = recent["accessionNumber"][i]
        primary = recent.get("primaryDocument", [""] * len(forms))[i]
        rows.append({
            "form": form,
            "filing_date": recent.get("filingDate", [""] * len(forms))[i],
            "accession": accession,
            "primary_document": primary,
        })
    return rows


def _filing_text(ticker: str, accession: str, document: str) -> tuple[str, str]:
    cik = str(int(COMPANIES[ticker]["cik"]))
    acc = accession.replace("-", "")
    url = "https://www.sec.gov/Archives/edgar/data/{}/{}/{}".format(cik, acc, document)
    response = SESSION.get(url, timeout=25)
    response.raise_for_status()
    # Keep API payload bounded for the relevance classifier.
    clean = re.sub(r"<[^>]+>", " ", response.text)
    clean = re.sub(r"\s+", " ", clean)
    return clean[:300_000], url


def _relevance(text: str, assumption: Assumption) -> dict:
    """Use structured LLM classification when configured; otherwise keyword rules."""
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            schema = {"type": "object", "properties": {
                "related": {"type": "boolean"}, "claim": {"type": "string"},
                "reason": {"type": "string"}}, "required": ["related", "claim", "reason"], "additionalProperties": False}
            result = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "system", "content": "SEC 공시와 투자 가정을 비교하는 분류기다. 공시 텍스트에 없는 사실을 만들지 말고, 관련 문장 또는 요약을 claim에 짧게 쓰라. 불확실하면 related=false."},
                          {"role": "user", "content": "투자 가정: {}\n메모: {}\n공시: {}".format(assumption.title, assumption.memo, text[:18000])}],
                response_format={"type": "json_schema", "json_schema": {"name": "filing_relevance", "strict": True, "schema": schema}},
                temperature=0)
            return json.loads(result.choices[0].message.content)
        except Exception as exc:
            fallback_reason = "LLM 분류 실패로 키워드 규칙 적용: {}".format(exc)
    else:
        fallback_reason = "OPENAI_API_KEY 미설정: 기준선 제목/메모의 키워드 일치로 관련성 확인"
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9가-힣]{3,}", assumption.title + " " + assumption.memo)]
    words = [w for w in words if w not in {"the", "and", "that", "this", "with", "from", "예시", "따라", "이어짐"}]
    metric_terms = {
        "revenue_growth": ["revenue", "sales", "growth", "demand", "accelerator", "data center", "datacenter"],
        "gross_margin": ["gross margin", "margin", "cost of revenue", "cost of sales"],
        "capex_growth": ["capital expenditures", "capital expenditure", "capex", "property and equipment"],
        "trade_value": ["semiconductor", "chip", "supply chain", "import", "export"],
        "sam_awards": ["contract", "award", "procurement", "government"],
        "fred_series": ["economic", "interest rate", "inflation", "production", "demand"],
    }
    terms = list(dict.fromkeys(words + metric_terms.get(assumption.metric, [])))
    matches = [term for term in terms if term in text.lower()]
    related = len(matches) >= 2 or any(len(term.split()) > 1 and term in text.lower() for term in matches)
    return {"related": related,
            "claim": "키워드 일치: " + ", ".join(matches[:8]) if matches else "자동 추출된 주장 없음",
            "reason": fallback_reason + ("; 일치 키워드: " + ", ".join(matches[:8]) if matches else "; 수동 검토 필요")}


def scan_company(ticker: str, assumptions: List[Assumption]) -> dict:
    company = COMPANIES[ticker]
    try:
        data = _get_json("https://data.sec.gov/submissions/CIK{}.json".format(company["cik"]))
        rows = _filing_rows(data["filings"]["recent"])
        events, matched = [], []
        for row in rows[:20]:
            cik = str(int(company["cik"]))
            accession_path = row["accession"].replace("-", "")
            row["url"] = "https://www.sec.gov/Archives/edgar/data/{}/{}/{}".format(cik, accession_path, row["primary_document"])
            events.append(row)
            relevant_assumptions = [a for a in assumptions if a.ticker == ticker]
            if not relevant_assumptions:
                continue
            try:
                text, filing_url = _filing_text(ticker, row["accession"], row["primary_document"])
            except Exception as exc:
                continue
            for a in relevant_assumptions:
                relevance = _relevance(text, a)
                if relevance["related"]:
                    matched.append({**row, "url": filing_url, "assumption_id": a.id,
                                    "assumption_title": a.title, "reason": relevance["reason"],
                                    "claim": relevance["claim"]})
        return {"events": events, "matched": matched, "checked_at": now_iso()}
    except Exception as exc:
        return {"events": [], "matched": [], "error": "SEC 공시 조회에 실패했습니다: {}".format(exc)}


def _sec_company_fact(ticker: str, metric: str) -> dict:
    cik = COMPANIES[ticker]["cik"]
    facts = _get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK{}.json".format(cik))
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    candidates = {
        "revenue_growth": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"],
        "gross_margin": ["GrossProfit", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
        "capex_growth": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    }
    if metric == "gross_margin":
        gross = _latest_fact(usgaap, candidates["gross_margin"][0])
        revenue = _latest_fact(usgaap, next((tag for tag in candidates["gross_margin"][1:] if tag in usgaap), ""))
        if gross and revenue and revenue["value"]:
            return {"value": 100 * gross["value"] / revenue["value"], "date": gross["date"], "unit": "%", "series": []}
    tag = next((tag for tag in candidates.get(metric, []) if tag in usgaap), None)
    if not tag:
        raise RuntimeError("SEC Company Facts에서 {}에 해당하는 XBRL 항목을 찾지 못했습니다.".format(metric))
    fact = usgaap[tag]
    unit = next(iter(fact.get("units", {})), None)
    observations = fact.get("units", {}).get(unit, [])
    observations = [o for o in observations if o.get("form") in ("10-Q", "10-K") and o.get("filed")]
    observations.sort(key=lambda o: o["filed"])
    # De-duplicate amended/re-filed observations for display.
    by_period = {}
    for obs in observations:
        by_period[obs.get("end")] = obs
    observations = sorted(by_period.values(), key=lambda o: o.get("end", ""))[-12:]
    if len(observations) < 2:
        raise RuntimeError("성장률 계산에 필요한 SEC 관측값이 2개 미만입니다.")
    # Compare latest annual periods when available. Quarterly XBRL values may be YTD.
    annuals = [o for o in observations if o.get("fp") == "FY" and o.get("form") == "10-K"]
    pair = annuals[-2:] if len(annuals) >= 2 else observations[-2:]
    previous, current = pair
    rate = None if not previous.get("val") else (float(current["val"]) / float(previous["val"]) - 1) * 100
    series = [{"date": o.get("end", ""), "value": float(o["val"]) / (1e9 if unit == "USD" else 1)} for o in observations[-6:]]
    return {"value": rate, "date": current.get("end", ""), "unit": "%", "series": series,
            "detail": "{} ({}) 연간 공시 비교".format(tag, unit)}


def _latest_fact(usgaap: dict, tag: str) -> Optional[dict]:
    if not tag or tag not in usgaap:
        return None
    units = usgaap[tag].get("units", {})
    unit = "USD" if "USD" in units else next(iter(units), None)
    items = units.get(unit, []) if unit else []
    items = [x for x in items if x.get("form") in ("10-Q", "10-K") and x.get("val") is not None]
    if not items:
        return None
    item = max(items, key=lambda x: x.get("end", ""))
    return {"value": float(item["val"]), "date": item.get("end"), "unit": unit}


def _fred(query: str) -> dict:
    if not query:
        raise RuntimeError("FRED series ID가 필요합니다 (예: DGS10, INDPRO).")
    params = {"series_id": query, "file_type": "json", "sort_order": "asc", "limit": 1000}
    if os.getenv("FRED_API_KEY"):
        params["api_key"] = os.environ["FRED_API_KEY"]
    else:
        raise RuntimeError("FRED_API_KEY가 필요합니다. 키 없이 사용할 수 있는 공공 API 엔드포인트가 없습니다.")
    payload = _get_json("https://api.stlouisfed.org/fred/series/observations", params)
    observations = [o for o in payload.get("observations", []) if o.get("value") not in (None, ".")]
    if not observations:
        raise RuntimeError("FRED 관측값이 없습니다. series ID를 확인하세요.")
    latest = observations[-1]
    series = [{"date": o["date"], "value": float(o["value"])} for o in observations[-36:]]
    return {"value": float(latest["value"]), "date": latest["date"], "unit": "FRED 원자료", "series": series,
            "detail": "FRED series {} 최근 관측치".format(query)}


def _comtrade(query: str) -> dict:
    hs = query or "8542"
    year = datetime.now(timezone.utc).year - 1
    params = {"reporterCode": "842", "cmdCode": hs, "period": "{},{}".format(year - 2, year - 1),
              "flowCode": "Import", "partnerCode": "0", "motCode": "0", "aggregateBy": "6", "breakdownMode": "classic", "partner2Code": "0"}
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
    return {"value": growth, "level": value, "date": str(latest.get("period")), "unit": "% YoY",
            "series": series, "detail": "미국 수입 HS {} 세계 전체, Comtrade primaryValue 전년 대비".format(hs)}


def _sam(query: str) -> dict:
    api_key = os.getenv("SAM_API_KEY")
    if not api_key:
        raise RuntimeError("SAM_API_KEY가 필요합니다. SAM.gov 공개 API 키를 설정하세요.")
    params = {"api_key": api_key, "limit": 100, "postedFrom": "01/01/2024", "postedTo": datetime.now().strftime("%m/%d/%Y")}
    if query:
        params["q"] = query
    payload = _get_json("https://api.sam.gov/prod/opportunities/v2/search", params)
    total = int(payload.get("totalRecords", 0))
    return {"value": float(total), "date": now_iso()[:10], "unit": "건", "series": [],
            "detail": "SAM.gov 관련 계약 기회 검색 결과 {}건 (수주/award가 아닌 기회 공고 수)".format(total)}


def collect_observation(assumption: Assumption) -> dict:
    if assumption.metric in ("revenue_growth", "gross_margin", "capex_growth"):
        result = _sec_company_fact(assumption.ticker, assumption.metric)
        return {**result, "source_name": "SEC Company Facts", "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK{}.json".format(COMPANIES[assumption.ticker]["cik"])}
    if assumption.metric == "fred_series":
        result = _fred(assumption.query)
        return {**result, "source_name": "FRED", "url": "https://fred.stlouisfed.org/series/{}".format(assumption.query)}
    if assumption.metric == "trade_value":
        result = _comtrade(assumption.query)
        return {**result, "source_name": "UN Comtrade", "url": "https://comtradeapi.un.org/public/v1/preview/C/A/HS"}
    if assumption.metric == "sam_awards":
        result = _sam(assumption.query)
        return {**result, "source_name": "SAM.gov", "url": "https://sam.gov/content/opportunities"}
    raise RuntimeError("지원하지 않는 검증 지표: {}".format(assumption.metric))


def _compare(assumption: dict, observation: dict) -> dict:
    value = observation.get("value")
    if value is None:
        return {"verdict": "수치 검증 불가", "divergence": None, "baseline_delta": None,
                "summary": "외부 API는 자료를 반환했지만 선택한 지표의 수치를 산출할 수 없습니다. 기준선과 지표 정의를 재검토하세요."}
    target, tolerance = float(assumption["target"]), float(assumption["tolerance"])
    divergence = float(value) - target
    baseline_delta = float(value) - float(assumption["baseline"])
    unit = observation.get("unit", "")
    delta_unit = "%p" if unit in ("%", "% YoY") else unit
    if divergence <= -tolerance:
        verdict = "가정 훼손 위험"
        summary = "관측값 {:.2f}{}가 목표 {:.2f}{}보다 {:.2f}{} 낮아 허용 괴리 {:.2f}{}를 초과했습니다. 기준값 대비 변화는 {:+.2f}{}입니다.".format(value, unit, target, unit, -divergence, delta_unit, tolerance, delta_unit, baseline_delta, delta_unit)
    elif divergence < 0:
        verdict = "주의 관찰"
        summary = "관측값이 목표보다 {:.2f}{} 낮지만 허용 범위 안입니다. 기준값 대비 변화는 {:+.2f}{}입니다. 후속 분기에서 재확인이 필요합니다.".format(-divergence, delta_unit, baseline_delta, delta_unit)
    else:
        verdict = "가정 유지 신호"
        summary = "관측값이 목표 이상이며 기준값 대비 변화는 {:+.2f}{}입니다. 이 지표만으로 가정의 인과 관계나 지속성을 입증하지는 못합니다.".format(baseline_delta, delta_unit)
    return {"verdict": verdict, "divergence": divergence, "baseline_delta": baseline_delta,
            "delta_unit": delta_unit, "summary": summary}


def _make_report(state: ReviewState) -> ReviewState:
    assumption, event, observation = state["assumption"], state["event"], state["observation"]
    comparison = _compare(assumption, observation)
    value = observation.get("value")
    red_team = []
    if comparison["verdict"] in ("가정 훼손 위험", "주의 관찰"):
        red_team.append("경영진의 방향성 발언이 기준 목표에 미달한 수치와 공존합니다. 일회성 요인인지 구조적 변화인지 추가 분해가 필요합니다.")
    else:
        red_team.append("단일 지표의 양호한 결과는 투자 가정 전체를 검증하지 않습니다. 매출 귀속, 가격·물량, 경쟁 구도와 현금흐름을 교차 확인해야 합니다.")
    if observation.get("source_name") in ("FRED", "UN Comtrade", "SAM.gov"):
        red_team.append("거시/무역/조달 지표는 특정 기업 실적에 직접 귀속되지 않습니다. 방향성 참고치로만 해석해야 합니다.")
    if value is None:
        red_team.append("API가 수치를 제공하지 못했습니다. 결측을 중립 또는 긍정으로 간주하지 않고 판단을 보류합니다.")
    report = {
        "id": str(uuid.uuid4())[:8], "created_at": now_iso(), "ticker": assumption["ticker"],
        "title": assumption["title"], "verdict": comparison["verdict"], "summary": comparison["summary"],
        "baseline_display": "{:.2f} {}".format(assumption["baseline"], metric_unit(assumption["metric"])),
        "observed_display": "자료 없음" if value is None else "{:.2f} {}".format(value, observation.get("unit", "")),
        "divergence_display": "계산 불가" if comparison["divergence"] is None else "목표 대비 {:+.2f}{} · 기준 대비 {:+.2f}{}".format(comparison["divergence"], comparison["delta_unit"], comparison["baseline_delta"], comparison["delta_unit"]),
        "filing_form": event["form"], "filing_url": event["url"], "claim": event.get("claim", ""),
        "filing_accession": event.get("accession", ""), "assumption_id": assumption["id"],
        "source_name": observation.get("source_name", ""), "evidence": [{"label": observation.get("source_name", "원자료"), "url": observation.get("url", ""), "detail": observation.get("detail", "") + " · 조회 " + now_iso()}],
        "series": observation.get("series", []), "red_team": red_team,
        "limitations": "기준값/목표치는 운용역 입력이며 외부 자료는 대리 지표일 수 있습니다. 환율, 회계연도, 단위, 계절성, 기업별 사업 믹스 차이를 확인하세요. 본 리포트는 검토 보조이며 투자 지시가 아닙니다.",
        "raw_observation": observation,
    }
    return {**state, "report": report}


def _review_graph():
    """Return compiled LangGraph if available; otherwise caller uses deterministic nodes."""
    try:
        from langgraph.graph import END, StateGraph
        graph = StateGraph(ReviewState)
        graph.add_node("collect", lambda s: {"observation": collect_observation(Assumption(**s["assumption"]))})
        graph.add_node("compare_and_red_team", _make_report)
        graph.set_entry_point("collect")
        graph.add_edge("collect", "compare_and_red_team")
        graph.add_edge("compare_and_red_team", END)
        return graph.compile()
    except ImportError:
        return None


def run_review(assumption: Assumption, event: dict) -> dict:
    state: ReviewState = {"assumption": asdict(assumption), "event": event}
    try:
        graph = _review_graph()
        if graph:
            result = graph.invoke(state)
        else:
            state["observation"] = collect_observation(assumption)
            result = _make_report(state)
        return result["report"]
    except Exception as exc:
        observation = {"value": None, "unit": "", "series": [], "source_name": assumption.source,
                       "url": "", "detail": "자료 수집 실패: {}".format(exc)}
        state["observation"] = observation
        result = _make_report(state)["report"]
        result["verdict"] = "수집 실패 · 판단 보류"
        result["summary"] = "외부 자료 수집이 실패했습니다. 데이터가 없으므로 투자 가정의 상태를 판정하지 않았습니다. 오류: {}".format(exc)
        result["evidence"] = [{"label": assumption.source, "url": "", "detail": observation["detail"]}]
        return result
