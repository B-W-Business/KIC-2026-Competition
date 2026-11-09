"""SEC submissions, filing text, and XBRL Company Facts provider."""
from __future__ import annotations
import os
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, List, Optional
from urllib.parse import urljoin, urlparse
from kic_watch.agents import CLASSIFICATION_VERSION, classify_filing
from kic_watch.models import Assumption, COMPANIES, now_iso
from kic_watch.security import safe_error
from kic_watch.providers.http import SESSION, _get_json
from storage import get_filing_match, save_filing_match


def _sec_headers() -> dict:
    return {"User-Agent": os.getenv("SEC_USER_AGENT", "").strip()}


def _sec_error(exc: Exception) -> str:
    message = safe_error(exc)
    if "403" in message:
        return ("SEC가 요청을 403으로 거부했습니다. SEC_USER_AGENT 프로젝트명/연락처는 설정되어 있습니다. "
                "추가 SEC 키는 필요하지 않으며, SEC의 IP/네트워크 차단 또는 일시 제한일 수 있습니다. " + message)
    return message


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

def _atom_filing_rows(ticker: str) -> List[dict]:
    """Fallback to SEC's official company EDGAR Atom feed when data.sec.gov is denied."""
    cik = COMPANIES[ticker]["cik"]
    response = SESSION.get(
        "https://www.sec.gov/cgi-bin/browse-edgar",
        params={"action": "getcompany", "CIK": cik, "type": "", "dateb": "",
                "owner": "exclude", "count": "100", "output": "atom"},
        timeout=25, headers=_sec_headers())
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1].lower() != "entry":
            continue
        fields = {}
        alternate = ""
        form = ""
        for node in entry.iter():
            name = node.tag.rsplit("}", 1)[-1].lower()
            value = (node.text or "").strip()
            if name in {"form", "filing-date", "accession-number", "filing-href"} and value:
                fields[name] = value
            if name == "category":
                form = node.attrib.get("term", form)
            if name == "link" and node.attrib.get("rel") in ("alternate", ""):
                alternate = node.attrib.get("href", alternate)
            if name == "id" and value and "accession-number=" in value and "accession-number" not in fields:
                fields["accession-number"] = value.rsplit("accession-number=", 1)[-1]
        form = fields.get("form", form)
        accession = fields.get("accession-number", "")
        if form not in ("8-K", "10-Q", "10-K") or not accession:
            continue
        filing_url = fields.get("filing-href") or alternate
        filing_date = fields.get("filing-date", "")
        if not filing_date:
            for node in entry.iter():
                if node.tag.rsplit("}", 1)[-1].lower() == "updated":
                    filing_date = (node.text or "")[:10]
                    break
        rows.append({"form": form, "filing_date": filing_date,
                     "accession": accession, "primary_document": "", "filing_url": filing_url})
    if not rows:
        raise RuntimeError("SEC EDGAR Atom 대체 피드에서 8-K/10-Q/10-K 항목을 찾지 못했습니다.")
    return rows


class _PrimaryDocumentLinks(HTMLParser):
    def __init__(self, expected_prefix: str):
        super().__init__(convert_charrefs=True)
        self.expected_prefix = expected_prefix
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        path = urlparse(href).path
        if (path.startswith(self.expected_prefix) and re.search(r"\.html?$", path, re.I)
                and not path.lower().endswith("-index.htm")):
            self.links.append(href)


def _filing_text(ticker: str, accession: str, document: str, filing_url: str = "") -> tuple[str, str]:
    cik = str(int(COMPANIES[ticker]["cik"]))
    acc = accession.replace("-", "")
    base_url = "https://www.sec.gov/Archives/edgar/data/{}/{}/".format(cik, acc)
    if document:
        url = urljoin(base_url, document)
    else:
        detail_url = filing_url or urljoin(base_url, accession + "-index.htm")
        index_response = SESSION.get(detail_url, timeout=25, headers=_sec_headers())
        index_response.raise_for_status()
        parser = _PrimaryDocumentLinks("/Archives/edgar/data/{}/{}/".format(cik, acc))
        parser.feed(index_response.text)
        if not parser.links:
            raise RuntimeError("SEC 제출 상세 페이지에서 공시 본문 링크를 찾지 못했습니다.")
        url = urljoin("https://www.sec.gov", parser.links[0])
    response = SESSION.get(url, timeout=25, headers=_sec_headers())
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
            result = classify_filing(text, assumption)
            result["reason"] = "OpenAI 구조화 공시 분류 · " + result["reason"]
            result["classifier"] = "openai:{}:{}".format(os.getenv("OPENAI_MODEL", "gpt-4o-mini"), CLASSIFICATION_VERSION)
            return result
        except Exception as exc:
            fallback_reason = "LLM 분류 실패로 키워드 규칙 적용: {}".format(safe_error(exc))
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
            "reason": fallback_reason + ("; 일치 키워드: " + ", ".join(matches[:8]) if matches else "; 수동 검토 필요"),
            "evidence_quote": "", "numeric_claim": None, "claim_metric": "unknown", "claim_unit": "", "claim_period": "",
            "classifier": "keyword_fallback" if api_key else "keywords:" + CLASSIFICATION_VERSION}

def scan_company(ticker: str, assumptions: List[Assumption]) -> dict:
    company = COMPANIES[ticker]
    feed_fallback = False
    try:
        try:
            data = _get_json("https://data.sec.gov/submissions/CIK{}.json".format(company["cik"]), headers=_sec_headers())
            rows = _filing_rows(data["filings"]["recent"])
        except Exception as submissions_error:
            try:
                rows = _atom_filing_rows(ticker)
            except Exception as atom_error:
                raise RuntimeError("data.sec.gov 제출 API 실패: {} · SEC 공식 Atom 대체 피드 실패: {}".format(
                    safe_error(submissions_error), safe_error(atom_error))) from atom_error
            feed_fallback = True
        events, matched, warnings = [], [], []
        for row in rows[:20]:
            cik = str(int(company["cik"]))
            accession_path = row["accession"].replace("-", "")
            row["url"] = row.get("filing_url") or "https://www.sec.gov/Archives/edgar/data/{}/{}/{}".format(cik, accession_path, row["primary_document"])
            events.append(row)
            relevant_assumptions = [a for a in assumptions if a.ticker == ticker and getattr(a, "enabled", True)]
            if not relevant_assumptions:
                continue
            pending = []
            expected_classifier = ("openai:{}:{}".format(os.getenv("OPENAI_MODEL", "gpt-4o-mini"), CLASSIFICATION_VERSION)
                                   if os.getenv("OPENAI_API_KEY") else "keywords:" + CLASSIFICATION_VERSION)
            for a in relevant_assumptions:
                cached = get_filing_match(ticker, row["accession"], a.id)
                if cached is not None and cached.get("classifier") != expected_classifier:
                    cached = None
                if cached is None:
                    pending.append(a)
                elif cached.get("related"):
                    matched.append({**row, **cached, "assumption_id": a.id, "assumption_title": a.title})
            filing_url = row["url"]
            if pending:
                try:
                    text, filing_url = _filing_text(ticker, row["accession"], row["primary_document"], row.get("filing_url", ""))
                except Exception as exc:
                    warnings.append("{} 공시 본문 조회 실패: {}".format(row["accession"], _sec_error(exc)))
                    continue
                for a in pending:
                    relevance = _relevance(text, a)
                    save_filing_match(ticker, row["accession"], a.id, relevance)
                    if relevance["related"]:
                        matched.append({**row, **relevance, "url": filing_url, "assumption_id": a.id,
                                        "assumption_title": a.title})
        result = {"events": events, "matched": matched, "warnings": warnings, "checked_at": now_iso()}
        if feed_fallback:
            result["source_warning"] = "data.sec.gov 제출 API가 거부되어 SEC 공식 EDGAR Atom 피드로 공시 목록을 조회했습니다."
        return result
    except Exception as exc:
        return {"events": [], "matched": [], "error": "SEC 기본 API와 공식 EDGAR Atom 대체 조회가 모두 실패했습니다. SEC_USER_AGENT 설정은 확인되었으며, SEC/네트워크의 접근 거부일 수 있습니다. {}".format(_sec_error(exc))}

def _sec_company_fact(ticker: str, metric: str) -> dict:
    cik = COMPANIES[ticker]["cik"]
    try:
        facts = _get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK{}.json".format(cik), headers=_sec_headers())
    except Exception as exc:
        raise RuntimeError(_sec_error(exc)) from exc
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    candidates = {
        "revenue_growth": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"],
        "gross_margin": ["GrossProfit", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
        "capex_growth": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    }
    if metric == "gross_margin":
        gross, revenue = _latest_matching_facts(
            usgaap, candidates["gross_margin"][0],
            next((tag for tag in candidates["gross_margin"][1:] if tag in usgaap), ""))
        if gross and revenue and revenue["value"]:
            return {"value": 100 * gross["value"] / revenue["value"], "date": gross["date"],
                    "period_key": gross["period_key"], "unit": "%", "series": []}
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
    period_key = "FY{}".format(current.get("fy")) if current.get("fp") == "FY" and current.get("fy") else "{}-{}".format(current.get("fy", ""), current.get("fp", "")).strip("-")
    return {"value": rate, "date": current.get("end", ""), "period_key": period_key, "unit": "%", "series": series,
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
    period_key = "FY{}".format(item.get("fy")) if item.get("fp") == "FY" and item.get("fy") else "{}-{}".format(item.get("fy", ""), item.get("fp", "")).strip("-")
    return {"value": float(item["val"]), "date": item.get("end"), "unit": unit, "period_key": period_key}

def _latest_matching_facts(usgaap: dict, numerator_tag: str, denominator_tag: str):
    """Return gross-profit and revenue XBRL facts from an identical period/duration."""
    def usable(tag):
        if tag not in usgaap:
            return []
        values = usgaap[tag].get("units", {}).get("USD", [])
        return [fact for fact in values if fact.get("form") in ("10-Q", "10-K") and fact.get("val") is not None]
    left, right = usable(numerator_tag), usable(denominator_tag)
    right_index = {(x.get("start"), x.get("end"), x.get("fy"), x.get("fp"), x.get("form")): x for x in right}
    pairs = []
    for numerator in left:
        key = (numerator.get("start"), numerator.get("end"), numerator.get("fy"), numerator.get("fp"), numerator.get("form"))
        denominator = right_index.get(key)
        if denominator:
            pairs.append((numerator, denominator))
    if not pairs:
        return None, None
    numerator, denominator = max(pairs, key=lambda pair: pair[0].get("end", ""))
    period_key = "FY{}".format(numerator.get("fy")) if numerator.get("fp") == "FY" and numerator.get("fy") else "{}-{}".format(numerator.get("fy", ""), numerator.get("fp", "")).strip("-")
    return ({"value": float(numerator["val"]), "date": numerator.get("end"), "unit": "USD", "period_key": period_key},
            {"value": float(denominator["val"]), "date": denominator.get("end"), "unit": "USD", "period_key": period_key})

