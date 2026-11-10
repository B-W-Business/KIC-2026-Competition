"""LangGraph investment-verification workflow and deterministic fallback."""
from __future__ import annotations
import json
import os
import uuid
from dataclasses import asdict
from typing import Any, Dict
from kic_watch.agents import normalize_period, red_team_review
from kic_watch.code_interpreter import analyze as analyze_in_sandbox
from kic_watch.models import Assumption, ReviewState, metric_unit, now_iso
from kic_watch.providers import collect_observation
from kic_watch.security import safe_error


def _compare(assumption: dict, observation: dict) -> dict:
    value = observation.get("value")
    if value is None:
        basis = assumption.get("tolerance_basis") or ("relative" if assumption.get("metric") in ("revenue_growth", "gross_margin", "capex_growth", "trade_value") else "absolute")
        if float(assumption["target"]) == 0 and basis == "relative":
            basis = "absolute"
        return {"verdict": "수치 검증 불가", "divergence": None, "baseline_delta": None,
                "relative_gap_pct": None, "tolerance_exceeded": False, "tolerance_basis": basis,
                "tolerance": float(assumption["tolerance"]), "threshold_gap": None,
                "threshold_unit": "%" if basis == "relative" else ("%p" if observation.get("unit") in ("%", "% YoY") else observation.get("unit", "")),
                "delta_unit": "%p" if observation.get("unit") in ("%", "% YoY") else observation.get("unit", ""),
                "summary": "외부 API는 자료를 반환했지만 선택한 지표의 수치를 산출할 수 없습니다. 기준선과 지표 정의를 재검토하세요."}
    target, tolerance = float(assumption["target"]), float(assumption["tolerance"])
    divergence = float(value) - target
    baseline_delta = float(value) - float(assumption["baseline"])
    unit = observation.get("unit", "")
    delta_unit = "%p" if unit in ("%", "% YoY") else unit
    relative_metric = assumption.get("metric") in ("revenue_growth", "gross_margin", "capex_growth", "trade_value")
    tolerance_basis = assumption.get("tolerance_basis") or ("relative" if relative_metric else "absolute")
    if target == 0 and tolerance_basis == "relative":
        tolerance_basis = "absolute"
    relative_gap_pct = (divergence / abs(target) * 100) if target != 0 else None
    tolerance_exceeded = (relative_gap_pct < -tolerance if tolerance_basis == "relative" else divergence < -tolerance)
    threshold_gap = relative_gap_pct if tolerance_basis == "relative" else divergence
    threshold_unit = "%" if tolerance_basis == "relative" else delta_unit
    if tolerance_exceeded:
        verdict = "가정 훼손 위험"
        summary = "관측값 {:.2f}{}는 목표 {:.2f}{}보다 {:.2f}{} 낮습니다. 목표 기준 상대 미달은 {}이며 허용 {} {}를 넘어 한도를 초과했습니다. 기준값 대비 변화는 {:+.2f}{}입니다.".format(
            value, unit, target, unit, -divergence, delta_unit,
            "계산 불가" if relative_gap_pct is None else "{:.2f}%".format(-relative_gap_pct),
            "상대 미달률" if tolerance_basis == "relative" else "절대 차이",
            "{:.2f}{}".format(tolerance, threshold_unit), baseline_delta, delta_unit)
    elif divergence < 0:
        verdict = "주의 관찰"
        summary = "관측값 {:.2f}{}는 목표 {:.2f}{}보다 {:.2f}{} 낮습니다. 목표 기준 상대 미달은 {}이며 허용 {} {} 이내입니다. 기준값 대비 변화는 {:+.2f}{}입니다.".format(
            value, unit, target, unit, -divergence, delta_unit,
            "계산 불가" if relative_gap_pct is None else "{:.2f}%".format(-relative_gap_pct),
            "상대 미달률" if tolerance_basis == "relative" else "절대 차이",
            "{:.2f}{}".format(tolerance, threshold_unit), baseline_delta, delta_unit)
    else:
        verdict = "가정 유지 신호"
        summary = "관측값 {:.2f}{}가 목표 {:.2f}{} 이상입니다. 기준값 대비 변화는 {:+.2f}{}입니다. 이 지표만으로 가정의 인과 관계나 지속성을 입증하지는 못합니다.".format(value, unit, target, unit, baseline_delta, delta_unit)
    return {"verdict": verdict, "divergence": divergence, "baseline_delta": baseline_delta,
            "relative_gap_pct": relative_gap_pct, "tolerance_exceeded": tolerance_exceeded,
            "tolerance_basis": tolerance_basis, "tolerance": tolerance, "threshold_gap": threshold_gap,
            "threshold_unit": threshold_unit, "delta_unit": delta_unit, "summary": summary}

def _analysis_node(state: ReviewState) -> ReviewState:
    assumption, observation = state["assumption"], state["observation"]
    value = observation.get("value")
    if value is None:
        return {**state, "analysis": {"execution_mode": "not_run", "fallback_reason": "관측 수치 없음", "repair_attempts": []}}
    analysis = analyze_in_sandbox({
        "baseline": float(assumption["baseline"]), "target": float(assumption["target"]),
        "tolerance": float(assumption["tolerance"]), "observed": float(value),
        "tolerance_basis": assumption.get("tolerance_basis") or ("relative" if assumption.get("metric") in ("revenue_growth", "gross_margin", "capex_growth", "trade_value") else "absolute"),
        "series": [float(point["value"]) for point in observation.get("series", []) if point.get("value") is not None],
    })
    return {**state, "analysis": analysis}

def _comparison_node(state: ReviewState) -> ReviewState:
    assumption, event, observation = state["assumption"], state["event"], state["observation"]
    comparison = _compare(assumption, observation)
    value = observation.get("value")
    analysis = state.get("analysis", {})
    sandbox_result = analysis.get("result", {})
    sandbox_verified = bool(analysis.get("sandbox_verified"))
    for key, expected in (("divergence", comparison.get("divergence")),
                          ("baseline_delta", comparison.get("baseline_delta")),
                          ("relative_gap_pct", comparison.get("relative_gap_pct"))):
        actual = sandbox_result.get(key)
        if actual is None or expected is None:
            sandbox_verified = sandbox_verified and actual is expected
        else:
            sandbox_verified = sandbox_verified and abs(float(actual) - float(expected)) <= 1e-6
    sandbox_verified = sandbox_verified and sandbox_result.get("tolerance_exceeded") is comparison.get("tolerance_exceeded")
    comparison["sandbox_verified"] = sandbox_verified
    claim = {key: event.get(key) for key in ("claim", "evidence_quote", "numeric_claim", "claim_metric", "claim_unit", "claim_period")}
    numeric_claim = claim.get("numeric_claim")
    claim_unit = str(claim.get("claim_unit") or "").strip().lower()
    observed_unit = str(observation.get("unit") or "").strip().lower()
    percent_claim = claim_unit in ("%", "percent", "percentage", "percentage points", "%p")
    percent_observation = observed_unit in ("%", "% yoy")
    unit_compatible = (percent_claim and percent_observation) or (claim_unit and claim_unit == observed_unit)
    claim_period = normalize_period(str(claim.get("claim_period") or ""))
    observation_period = normalize_period(str(observation.get("period_key") or ""))
    period_aligned = bool(claim_period and observation_period and claim_period == observation_period)
    claim_comparable = (numeric_claim is not None and claim.get("claim_metric") == assumption["metric"]
                        and unit_compatible and period_aligned and value is not None)
    if claim_comparable:
        claim_gap = float(value) - float(numeric_claim)
        comparison["claim_gap"] = claim_gap
        comparison["claim_comparable"] = True
        claim_relative_gap = claim_gap / abs(float(numeric_claim)) * 100 if float(numeric_claim) != 0 else None
        claim_breach = (claim_relative_gap < -float(assumption["tolerance"])
                        if comparison["tolerance_basis"] == "relative" and claim_relative_gap is not None
                        else claim_gap < -float(assumption["tolerance"]))
        if claim_breach:
            comparison["verdict"] = "경영진 주장 미달 위험"
            comparison["summary"] += " 공시의 명시적 수치 주장 {:.2f}{}보다 관측값이 {:.2f}{} 낮습니다.".format(
                numeric_claim, observation.get("unit", ""), -claim_gap,
                "%p" if percent_observation else observation.get("unit", ""))
    else:
        comparison["claim_comparable"] = False
    comparison["claim_period_aligned"] = period_aligned
    return {**state, "comparison": comparison, "claim": claim}

def _red_team_node(state: ReviewState) -> ReviewState:
    red_team = red_team_review(state["assumption"], state.get("claim", {}), state["observation"],
                               state["comparison"]["verdict"], state["comparison"])
    return {**state, "red_team": red_team}

def _report_node(state: ReviewState) -> ReviewState:
    assumption, event, observation = state["assumption"], state["event"], state["observation"]
    comparison, red_team = state["comparison"], state["red_team"]
    value, claim = observation.get("value"), state.get("claim", {})
    report = {
        "id": str(uuid.uuid4())[:8], "created_at": now_iso(), "ticker": assumption["ticker"],
        "title": assumption["title"], "metric": assumption["metric"],
        "verdict": comparison["verdict"], "summary": comparison["summary"],
        "baseline_display": "{:.2f} {}".format(assumption["baseline"], metric_unit(assumption["metric"])),
        "observed_display": "자료 없음" if value is None else "{:.2f} {}".format(value, observation.get("unit", "")),
        "target_display": "{:.2f} {}".format(assumption["target"], metric_unit(assumption["metric"])),
        "tolerance_display": "{:.2f} {}".format(comparison["tolerance"], "%" if comparison["tolerance_basis"] == "relative" else comparison["delta_unit"]),
        "period_display": observation.get("period_key", observation.get("date", "기간 미상")),
        "tolerance_basis": comparison["tolerance_basis"], "tolerance_exceeded": comparison["tolerance_exceeded"],
        "target_gap": comparison.get("divergence"), "target_gap_unit": comparison.get("delta_unit", ""),
        "relative_gap_pct": comparison.get("relative_gap_pct"),
        "divergence_display": "계산 불가" if comparison.get("divergence") is None else "목표 대비 {:+.2f}{} · 목표 기준 미달 {} · 기준 대비 {:+.2f}{}".format(
            comparison["divergence"], comparison["delta_unit"],
            "계산 불가" if comparison.get("relative_gap_pct") is None else "{:+.2f}%".format(comparison["relative_gap_pct"]),
            comparison["baseline_delta"], comparison["delta_unit"]),
        "filing_form": event["form"], "filing_url": event["url"], "claim": event.get("claim", ""),
        "claim_evidence_quote": event.get("evidence_quote", ""), "claim_numeric": claim.get("numeric_claim"),
        "claim_metric": event.get("claim_metric", "unknown"), "claim_unit": event.get("claim_unit", ""),
        "claim_period": event.get("claim_period", ""), "claim_comparable": comparison["claim_comparable"],
        "claim_period_aligned": comparison["claim_period_aligned"], "claim_gap": comparison.get("claim_gap"),
        "filing_accession": event.get("accession", ""), "assumption_id": assumption["id"],
        "source_name": observation.get("source_name", ""), "evidence": [{"label": observation.get("source_name", "원자료"), "url": observation.get("url", ""), "detail": observation.get("detail", "") + " · 조회 " + now_iso()}],
        "series": observation.get("series", []), "series_label": observation.get("series_label", "관측 시계열"),
        "series_unit": observation.get("series_unit", observation.get("unit", "")),
        "red_team": red_team.get("counterarguments", []),
        "red_team_analysis": red_team, "analysis": state.get("analysis"),
        "sandbox_verified": comparison.get("sandbox_verified", False),
        "limitations": "수치 주장은 지표·단위·기간이 정확히 일치할 때만 직접 비교합니다. 기준값/목표치는 운용역 입력이며 외부 자료는 대리 지표일 수 있습니다. 회계연도, 계절성, 사업 믹스와 귀속을 추가 확인하세요. 본 리포트는 투자 검토 보조이며 투자 지시가 아닙니다.",
        "raw_observation": observation,
    }
    return {**state, "report": report}

def _review_graph():
    """Return compiled LangGraph if available; otherwise caller uses deterministic nodes."""
    try:
        from langgraph.graph import END, StateGraph
        graph = StateGraph(ReviewState)
        graph.add_node("collect", lambda s: {"observation": collect_observation(Assumption(**s["assumption"]))})
        graph.add_node("sandbox_analysis", _analysis_node)
        graph.add_node("compare", _comparison_node)
        graph.add_node("red_team", _red_team_node)
        graph.add_node("report", _report_node)
        graph.set_entry_point("collect")
        graph.add_edge("collect", "sandbox_analysis")
        graph.add_edge("sandbox_analysis", "compare")
        graph.add_edge("compare", "red_team")
        graph.add_edge("red_team", "report")
        graph.add_edge("report", END)
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
            state = _analysis_node(state)
            state = _comparison_node(state)
            state = _red_team_node(state)
            result = _report_node(state)
        return result["report"]
    except Exception as exc:
        observation = {"value": None, "unit": "", "series": [], "source_name": assumption.source,
                       "url": "", "detail": "자료 수집 실패: {}".format(safe_error(exc))}
        state["observation"] = observation
        state = _analysis_node(state)
        state = _comparison_node(state)
        state = _red_team_node(state)
        result = _report_node(state)["report"]
        result["verdict"] = "수집 실패 · 판단 보류"
        result["summary"] = "외부 자료 수집이 실패했습니다. 데이터가 없으므로 투자 가정의 상태를 판정하지 않았습니다. 오류: {}".format(safe_error(exc))
        result["evidence"] = [{"label": assumption.source, "url": "", "detail": observation["detail"]}]
        return result

