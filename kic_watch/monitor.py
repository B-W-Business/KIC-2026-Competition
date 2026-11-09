"""Reusable portfolio monitoring service shared by the UI and scheduler."""
from __future__ import annotations

from typing import Any, Iterable, List

from kic_agent import COMPANIES, Assumption, run_review, scan_company
from kic_watch.security import safe_error
from storage import claim_event, fail_event, finish_event, load_state, save_report


def monitor_ticker(ticker: str, assumptions: List[Assumption] = None) -> dict:
    if assumptions is None:
        stored, _ = load_state()
        assumptions = [Assumption(**row) for row in stored]
    active = [a for a in assumptions if a.ticker == ticker and a.enabled]
    scan = scan_company(ticker, active)
    reports = []
    errors = []
    if scan.get("error"):
        return {**scan, "reports": reports, "processing_errors": errors}
    by_id = {a.id: a for a in active}
    for event in scan.get("matched", []):
        key = (ticker, event["accession"], event["assumption_id"])
        if not claim_event(*key):
            continue
        try:
            assumption = by_id[event["assumption_id"]]
            report = run_review(assumption, event)
            reports.append(report)
            if report.get("verdict", "").startswith("수집 실패"):
                save_report(report)
                fail_event(*key, report.get("summary", "자료 수집 실패"))
            else:
                finish_event(*key, report)
        except Exception as exc:
            message = safe_error(exc)
            errors.append(message)
            fail_event(*key, message)
    return {**scan, "reports": reports, "processing_errors": errors}


def monitor_portfolio() -> dict:
    """Scan each active portfolio company once; persistent caches prevent rework."""
    stored, _ = load_state()
    assumptions = [Assumption(**row) for row in stored]
    tickers = sorted({a.ticker for a in assumptions if a.enabled})
    results = []
    for ticker in tickers:
        results.append(monitor_ticker(ticker, assumptions))
    return {"tickers_scanned": len(tickers), "new_reports": sum(len(r.get("reports", [])) for r in results),
            "errors": sum(bool(r.get("error")) or bool(r.get("processing_errors")) for r in results),
            "results": results}
