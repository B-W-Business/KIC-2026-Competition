"""Domain models and portfolio constants."""
from __future__ import annotations
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, TypedDict


COMPANIES = {
    # OpenDART corp_code values; these are Korean issuer records, not KIC holdings.
    "005930": {"name": "Samsung Electronics Co., Ltd.", "market": "KR", "corp_code": "00126380", "stock_code": "005930"},
    "000660": {"name": "SK hynix Inc.", "market": "KR", "corp_code": "00164779", "stock_code": "000660"},
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
    enabled: bool = True
    # Relative percent of target for growth/margin metrics; absolute units otherwise.
    tolerance_basis: str = ""

    @classmethod
    def create(cls, **kwargs: Any) -> "Assumption":
        if not kwargs.get("tolerance_basis"):
            kwargs["tolerance_basis"] = "relative" if kwargs.get("metric") in {
                "revenue_growth", "gross_margin", "capex_growth", "trade_value"} else "absolute"
        return cls(id=str(uuid.uuid4())[:8], created_at=now_iso(), **kwargs)

class ReviewState(TypedDict, total=False):
    assumption: Dict[str, Any]
    event: Dict[str, Any]
    observation: Dict[str, Any]
    analysis: Dict[str, Any]
    comparison: Dict[str, Any]
    claim: Dict[str, Any]
    red_team: Dict[str, Any]
    report: Dict[str, Any]

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

