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

def _analysis_node(state: ReviewState) -> ReviewState:
    assumption, observation = state["assumption"], state["observation"]
    value = observation.get("value")
    if value is None:
        return {**state, "analysis": {"execution_mode": "not_run", "fallback_reason": "관측 수치 없음", "repair_attempts": []}}
    analysis = analyze_in_sandbox({
        "baseline": float(assumption["baseline"]), "target": float(assumption["target"]),
        "tolerance": float(assumption["tolerance"]), "observed": float(value),
        "series": [float(point["value"]) for point in observation.get("series", []) if point.get("value") is not None],
    })
    return {**state, "analysis": analysis}

def _comparison_node(state: ReviewState) -> ReviewState:
    assumption, event, observation = state["assumption"], state["event"], state["observation"]
    comparison = _compare(assumption, observation)
    value = observation.get("value")
    analysis = state.get("analysis", {})
    sandbox_gap = analysis.get("result", {}).get("divergence")
    comparison["sandbox_verified"] = (sandbox_gap is not None and comparison.get("divergence") is not None
                                      and abs(float(sandbox_gap) - comparison["divergence"]) <= 1e-6)
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
        if claim_gap <= -float(assumption["tolerance"]):
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
        "title": assumption["title"], "verdict": comparison["verdict"], "summary": comparison["summary"],
        "baseline_display": "{:.2f} {}".format(assumption["baseline"], metric_unit(assumption["metric"])),
        "observed_display": "자료 없음" if value is None else "{:.2f} {}".format(value, observation.get("unit", "")),
        "divergence_display": "계산 불가" if comparison.get("divergence") is None else "목표 대비 {:+.2f}{} · 기준 대비 {:+.2f}{}".format(comparison["divergence"], comparison["delta_unit"], comparison["baseline_delta"], comparison["delta_unit"]),
        "filing_form": event["form"], "filing_url": event["url"], "claim": event.get("claim", ""),
        "claim_evidence_quote": event.get("evidence_quote", ""), "claim_numeric": claim.get("numeric_claim"),
        "claim_metric": event.get("claim_metric", "unknown"), "claim_unit": event.get("claim_unit", ""),
        "claim_period": event.get("claim_period", ""), "claim_comparable": comparison["claim_comparable"],
        "claim_period_aligned": comparison["claim_period_aligned"], "claim_gap": comparison.get("claim_gap"),
        "filing_accession": event.get("accession", ""), "assumption_id": assumption["id"],
        "source_name": observation.get("source_name", ""), "evidence": [{"label": observation.get("source_name", "원자료"), "url": observation.get("url", ""), "detail": observation.get("detail", "") + " · 조회 " + now_iso()}],
        "series": observation.get("series", []), "red_team": red_team.get("counterarguments", []),
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

