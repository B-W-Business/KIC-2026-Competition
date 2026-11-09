"""Provider routing by normalized assumption metric."""
from kic_watch.models import Assumption, COMPANIES
from kic_watch.providers.comtrade import _comtrade
from kic_watch.providers.fred import _fred
from kic_watch.providers.sam import _sam
from kic_watch.providers.sec import _sec_company_fact
from kic_watch.providers import opendart


def collect_observation(assumption: Assumption) -> dict:
    if assumption.metric in ("revenue_growth", "gross_margin", "capex_growth"):
        if COMPANIES[assumption.ticker].get("market") == "KR":
            result = opendart.company_fact(assumption.ticker, assumption.metric)
            return {**result, "source_name": "OpenDART 연결 재무제표"}
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


def scan_issuer(ticker: str, assumptions: list) -> dict:
    if COMPANIES[ticker].get("market") == "KR":
        return opendart.scan_company(ticker, assumptions)
    from kic_watch.providers.sec import scan_company
    return scan_company(ticker, assumptions)

