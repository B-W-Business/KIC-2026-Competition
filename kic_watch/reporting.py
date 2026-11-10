"""Trustworthy presentation helpers for stored verification reports."""
from __future__ import annotations


def sandbox_status_for(analysis: dict, legacy_report: bool = False) -> str:
    """Do not label pre-upgrade partial checks as full sandbox verification."""
    if legacy_report:
        return "이전 검산 기준의 레거시 리포트 · 전체 산식 대조 미확인"
    if analysis.get("execution_mode") == "not_run":
        return "관측값 없음 · 계산 미실행"
    if "sandbox_verified" not in analysis:
        return "검산 정보 없음 · 이전 버전 리포트"
    if analysis["sandbox_verified"]:
        return "E2B 전체 산식 대조 통과"
    if analysis.get("execution_mode") == "e2b_unverified_fallback":
        return "E2B 결과 불일치 · 로컬 안전 계산으로 대체"
    return "E2B 미실행 · 로컬 결정론 계산"
