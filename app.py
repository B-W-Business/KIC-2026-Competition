from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from kic_agent import COMPANIES, Assumption, run_review, seed_assumptions
from kic_watch.monitor import monitor_ticker
from storage import load_state, save_assumption, save_report
st.set_page_config(page_title="KIC Investment Assumption Watch", page_icon="🛡️", layout="wide")

METRIC_LABELS = {
    "revenue_growth": "연간 매출 성장률 · 기업 공시 재무제표",
    "gross_margin": "매출총이익률 · 기업 공시 재무제표",
    "capex_growth": "설비투자 성장률 · 기업 공시 재무제표",
    "fred_series": "거시 지표 · FRED",
    "trade_value": "반도체 무역 성장률 · UN Comtrade",
    "sam_awards": "미 정부 계약 기회 수 · SAM.gov",
}
METRIC_SOURCES = {
    "revenue_growth": "SEC Company Facts", "gross_margin": "SEC Company Facts",
    "capex_growth": "SEC Company Facts", "fred_series": "FRED",
    "trade_value": "UN Comtrade", "sam_awards": "SAM.gov",
}


def company_fact_source(ticker: str) -> str:
    return "OpenDART 연결 재무제표" if COMPANIES[ticker].get("market") == "KR" else "SEC Company Facts"


def create_quickstart_assumption(ticker: str) -> Assumption:
    """Create an explicitly illustrative first-run assumption for the selected issuer."""
    if COMPANIES[ticker].get("market") == "KR":
        return Assumption.create(
            ticker=ticker, title="[체험용] 연간 매출 성장률 확인",
            memo="체험용 예시 기준선입니다. 실제 투자 메모·기업 전망치가 아니며, 등록 전에 검증할 목표와 허용 괴리를 직접 수정하세요.",
            metric="revenue_growth", source="OpenDART 연결 재무제표", baseline=0.0,
            target=5.0, tolerance=5.0, query="")
    return Assumption.create(
        ticker=ticker, title="[체험용] 미국 10년물 금리 추적",
        memo="체험용 거시 대리 지표입니다. 선택 기업의 매출이나 실적을 직접 나타내지 않습니다. 실제 투자 가정으로 교체하세요.",
        metric="fred_series", source="FRED", baseline=4.0, target=4.5,
        tolerance=1.0, query="DGS10")


@st.cache_data(ttl=10, show_spinner=False)
def background_service_status(base_url: str) -> dict:
    """Keep sidebar checks fast and avoid blocking every Streamlit rerun."""
    import requests

    response = requests.get(base_url.rstrip("/") + "/health", timeout=(0.2, 0.2))
    response.raise_for_status()
    return response.json()

if "assumptions" not in st.session_state:
    stored_assumptions, stored_reports = load_state()
    st.session_state.assumptions = [Assumption(**a) for a in stored_assumptions] if stored_assumptions else seed_assumptions()
    st.session_state.reports = stored_reports
    if not stored_assumptions:
        for baseline in st.session_state.assumptions:
            save_assumption(baseline)
if "reports" not in st.session_state:
    st.session_state.reports = []
else:
    _, persisted_reports = load_state()
    merged_reports = {r.get("id"): r for r in persisted_reports}
    merged_reports.update({r.get("id"): r for r in st.session_state.reports})
    st.session_state.reports = sorted(merged_reports.values(), key=lambda r: r.get("created_at", ""), reverse=True)
if "events" not in st.session_state:
    st.session_state.events = []

st.title("KIC 해외 포트폴리오 투자 가정 감시")
st.caption("투자 가정을 기준선으로 저장하고, 공시·공공 데이터와 직접 검증합니다.")
st.caption("FRED 자료는 Federal Reserve Bank of St. Louis의 보증이나 인증을 의미하지 않습니다. [FRED API 이용 약관](https://fred.stlouisfed.org/docs/api/terms_of_use.html)")

with st.sidebar:
    st.header("감시 설정")
    ticker = st.selectbox("기업", list(COMPANIES), format_func=lambda t: f"{t} · {COMPANIES[t]['name']}")
    issuer_label = "OpenDART" if COMPANIES[ticker].get("market") == "KR" else "SEC"
    auto_refresh = st.toggle("정기 재조회", value=False, help=f"페이지가 열려 있는 동안 {issuer_label} 최신 공시 내역을 주기적으로 확인합니다.")
    interval = st.select_slider("재조회 간격", options=[5, 15, 30, 60], value=15, format_func=lambda n: f"{n}분")
    poll_count = 0
    if auto_refresh:
        try:
            from streamlit_autorefresh import st_autorefresh
            poll_count = st_autorefresh(interval=interval * 60_000, key="sec_poll")
            st.caption("페이지가 열려 있을 때만 동작합니다.")
        except ImportError:
            st.warning("자동 새로고침 기능을 사용하려면 streamlit-autorefresh를 설치하세요. '공시 확인' 버튼은 계속 사용할 수 있습니다.")
    st.divider()
    with st.expander("연결 설정 상태 (키 값은 표시하지 않음)"):
        user_agent = os.getenv("SEC_USER_AGENT", "").lower()
        sec_configured = bool(user_agent and "your-contact" not in user_agent and "example.org" not in user_agent and "example.com" not in user_agent)
        st.write("SEC User-Agent", "설정됨" if sec_configured else "설정 필요")
        if not sec_configured:
            st.caption("SEC는 API 키 대신 프로젝트명과 연락 가능한 이메일이 담긴 User-Agent를 사용합니다.")
        st.write("OpenDART", "키 설정됨" if os.getenv("OPENDART_API_KEY") else "키 필요 (한국 기업 공시)")
        for label, variable in (("FRED", "FRED_API_KEY"), ("SAM.gov", "SAM_API_KEY"),
                                ("OpenAI", "OPENAI_API_KEY"), ("E2B", "E2B_API_KEY")):
            st.write(label, "키 설정됨" if os.getenv(variable) else "키 없음")
        st.write("UN Comtrade", "키 설정됨" if (os.getenv("COMTRADE_API_KEY") or os.getenv("COMTRADE_SECONDARY_API_KEY")) else "공개 키 없는 경로 사용")
        st.caption("여기에는 설정 여부만 표시합니다. 키가 실제로 서비스에서 승인되었는지는 ‘바로 검증’으로 확인하세요.")
    if st.button("백그라운드 감시 상태 확인"):
        try:
            service = background_service_status(os.getenv("MONITOR_API_URL", "http://127.0.0.1:8000"))
            if service.get("scheduler_running"):
                st.success("백그라운드 감시 실행 중 · {}초 간격".format(service.get("interval_seconds")))
            else:
                st.warning("백그라운드 감시 서비스가 시작 중입니다.")
        except Exception:
            st.warning("백그라운드 API가 연결되지 않았습니다. 터미널에서 `uvicorn kic_watch.api:app` 실행이 필요합니다.")

tab_monitor, tab_baseline, tab_reports, tab_about = st.tabs(["모니터링", "투자 가정", "검증 리포트", "도움말"])

with tab_monitor:
    company = COMPANIES[ticker]
    st.subheader(f"{company['name']} ({ticker})")
    active_assumptions = [a for a in st.session_state.assumptions if a.ticker == ticker and a.enabled]
    active_count = len(active_assumptions)
    report_count = sum(r.get("ticker") == ticker for r in st.session_state.reports)
    with st.container(horizontal=True):
        st.metric("감시 중인 가정", active_count, border=True)
        st.metric("저장된 검증 리포트", report_count, border=True)
        st.metric("마지막 공시 확인", st.session_state.get("last_scan", "아직 확인 전"), border=True)

    if not active_count:
        with st.container(border=True):
            st.subheader("이 기업에는 활성 감시 가정이 없습니다")
            st.write("체험용 기준을 추가하거나 ‘투자 가정’ 탭에서 실제 가정을 등록하세요. 체험 기준은 투자 메모가 아니므로 등록 후 목표와 허용 괴리를 확인하세요.")
            if st.button("체험용 가정 추가", type="primary", key="quickstart_assumption"):
                assumption = create_quickstart_assumption(ticker)
                st.session_state.assumptions.append(assumption)
                save_assumption(assumption)
                st.rerun()
    else:
        assumption_to_check = st.selectbox(
            "즉시 검증할 가정", active_assumptions,
            format_func=lambda a: f"{a.title} · {METRIC_LABELS.get(a.metric, a.metric)}",
            key=f"assumption_to_check_{ticker}")
        st.caption("즉시 검증은 SEC 공시 감지와 별도로 데이터 수집·계산·Red-Team 흐름을 실행합니다. OpenAI/E2B 키 사용 시 사용량 과금이 발생할 수 있습니다.")
        verify_col, scan_col = st.columns(2)
        with verify_col:
            verify_now = st.button("선택 가정 바로 검증", type="primary", width="stretch", key="verify_assumption")
        with scan_col:
            scan_now = st.button(f"{issuer_label} 최신 공시 조회", width="stretch", key="scan_sec")
        if verify_now:
            manual_event = {"form": "수동 검증", "url": "", "claim": "", "evidence_quote": "",
                            "numeric_claim": None, "claim_metric": "unknown", "claim_unit": "", "claim_period": ""}
            with st.spinner("선택한 출처를 조회하고 검증 리포트를 만드는 중입니다…"):
                report = run_review(assumption_to_check, manual_event)
            save_report(report)
            st.session_state.reports.insert(0, report)
            st.success("검증이 끝났습니다. ‘검증 리포트’ 탭에서 결과를 확인하세요.")
            st.rerun()
    if not active_count:
        scan_now = st.button(f"{issuer_label} 최신 공시 목록만 조회", type="primary", width="stretch", key="scan_sec_empty")
    should_scan = scan_now or (auto_refresh and poll_count > 0 and poll_count != st.session_state.get("last_poll_count", -1))
    if should_scan:
        with st.spinner(f"{issuer_label} 제출 내역을 확인하고 등록된 투자 가정을 대조합니다..."):
            result = monitor_ticker(ticker, st.session_state.assumptions)
        for report in result.get("reports", []):
            st.session_state.reports.insert(0, report)
        st.session_state.last_scan = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        st.session_state.last_poll_count = poll_count
        st.session_state.events = result.get("events", [])
        if result.get("error"):
            st.error(result["error"], title="공시 목록을 가져오지 못했습니다")
            st.info("공시 조회가 막혀도 FRED·UN Comtrade·SAM.gov 지표는 ‘선택 가정 바로 검증’으로 사용할 수 있습니다.")
        else:
            st.success(f"최근 공시 {len(result['events'])}건 확인 · 가정 연관 이벤트 {len(result['matched'])}건")
            if result.get("source_warning"):
                st.warning(result["source_warning"])
            for warning in result.get("warnings", []):
                st.warning("일부 공시 본문을 처리하지 못했습니다: " + warning)
            for event in result["matched"]:
                st.warning(f"{event['form']} · {event['filing_date']} — {event['assumption_title']}")
                st.link_button(f"{issuer_label} 원문 열기", event["url"])
                st.caption(event["reason"])
            for error in result.get("processing_errors", []):
                st.error("이벤트 처리 실패. 제한된 재시도 대상으로 기록했습니다: " + error)
            if result["matched"]:
                st.success("관련 공시의 팩트체크와 Red-Team 리포트를 자동 생성했습니다. 검증 리포트 탭에서 확인하세요.")
            with st.expander("확인한 SEC 공시"):
                for event in result["events"]:
                    st.markdown(f"- [{event['form']} · {event['filing_date']}]({event['url']})")
    if not should_scan and st.session_state.events:
        with st.expander("마지막 공시 확인 결과"):
            for event in st.session_state.events:
                st.markdown(f"- [{event['form']} · {event['filing_date']}]({event['url']})")
    if active_count:
        st.caption("이 결과는 투자 판단을 대신하지 않습니다. 대리 지표와 회계 기간 차이를 리포트에서 함께 확인하세요.")

with tab_baseline:
    st.subheader("초기 투자 가정 기준선")
    st.write("가정 하나에 검증 지표 하나를 연결하세요. 기준값·목표값·허용 괴리를 정해 공시와 별도로 바로 검증할 수 있습니다.")
    company_assumptions = [a for a in st.session_state.assumptions if a.ticker == ticker]
    if not company_assumptions:
        st.info("첫 사용이라면 체험용 가정으로 전체 검증 흐름을 먼저 실행할 수 있습니다. 체험 가정은 실제 투자 메모가 아닙니다.", title="빠르게 시작하기")
        quickstart_label = "OpenDART 매출 성장률 체험 가정 추가" if COMPANIES[ticker].get("market") == "KR" else "미국 10년물 금리 체험 가정 추가"
        if st.button(quickstart_label, key="baseline_quickstart"):
            assumption = create_quickstart_assumption(ticker)
            st.session_state.assumptions.append(assumption)
            save_assumption(assumption)
            st.rerun()
    st.markdown("#### 직접 가정 등록")
    a_metric = st.selectbox("검증 지표", list(METRIC_LABELS),
                            format_func=lambda metric: METRIC_LABELS[metric], key="assumption_metric")
    a_source = company_fact_source(ticker) if a_metric in ("revenue_growth", "gross_margin", "capex_growth") else METRIC_SOURCES[a_metric]
    st.caption("연결 출처: " + a_source)
    with st.form("assumption_form", clear_on_submit=True):
        a_title = st.text_input("가정 제목", placeholder="예: AI 서버 수요에 따라 데이터센터 매출이 연 25% 이상 성장")
        a_text = st.text_area("투자 메모와 근거", placeholder="투자 당시 근거, 전제, 경영진 주장을 입력하세요.")
        c1, c2, c3 = st.columns(3)
        unit_label = "%" if a_metric in ("revenue_growth", "gross_margin", "capex_growth", "trade_value") else ("건" if a_metric == "sam_awards" else "원자료 단위")
        a_baseline = c1.number_input(f"기준값 ({unit_label})", value=0.0, step=1.0)
        a_target = c2.number_input(f"목표값 ({unit_label})", value=20.0, step=1.0)
        a_tolerance = c3.number_input(f"허용 괴리 ({unit_label})", value=10.0, min_value=0.0, step=1.0)
        query_defaults = {"fred_series": "DGS10", "trade_value": "8542", "sam_awards": COMPANIES[ticker]["name"]}
        query_placeholder = {"fred_series": "예: DGS10 (미국 10년물 금리)", "trade_value": "예: 8542 (반도체)",
                             "sam_awards": "예: 기업명 또는 조달 키워드", "revenue_growth": "별도 키 불필요",
                             "gross_margin": "별도 키 불필요", "capex_growth": "별도 키 불필요"}[a_metric]
        a_note = st.text_input("출처 조회 키", value=query_defaults.get(a_metric, ""), placeholder=query_placeholder,
                               key=f"assumption_query_{a_metric}")
        submitted = st.form_submit_button("기준선 등록", type="primary")
    query_required = a_metric in ("fred_series", "trade_value", "sam_awards")
    if submitted and a_title.strip() and (not query_required or a_note.strip()):
        assumption = Assumption.create(
            ticker=ticker, title=a_title.strip(), memo=a_text.strip(), metric=a_metric,
            source=a_source, baseline=a_baseline, target=a_target,
            tolerance=a_tolerance, query=a_note.strip())
        st.session_state.assumptions.append(assumption)
        save_assumption(assumption)
        st.success("투자 가정을 기준선에 등록했습니다.")
    elif submitted and not a_title.strip():
        st.error("가정 제목을 입력하세요.")
    elif submitted:
        st.error("선택한 출처의 조회 키를 입력하세요.")
    selected = [a for a in st.session_state.assumptions if a.ticker == ticker]
    if selected:
        rows = [{"감시": a.enabled, "제목": a.title, "지표": a.metric, "출처": a.source, "기준값": a.baseline, "목표": a.target, "허용 괴리": a.tolerance, "등록": a.created_at} for a in selected]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        with st.expander("가정 메모와 세부값"):
            for a in selected:
                enabled = st.checkbox("감시 활성화", value=a.enabled, key=f"assumption_active_{a.id}")
                if enabled != a.enabled:
                    a.enabled = enabled
                    save_assumption(a)
                st.markdown(f"**{a.title}**  \n{a.memo or '메모 없음'} · ID `{a.id}` · 쿼리 `{a.query or '기본값'}`")
    else:
        st.caption("선택한 기업에 등록된 기준선이 없습니다.")

with tab_reports:
    st.subheader("Red-Team 검증 기록")
    reports = [r for r in st.session_state.reports if r.get("ticker") == ticker]
    if not reports:
        st.info("아직 리포트가 없습니다. 모니터링 탭에서 가정을 선택하고 ‘바로 검증’을 누르세요.", title="첫 검증 실행")
    for report in reports:
        verdict = report.get("verdict", "검토 필요")
        with st.container(border=True):
            st.markdown(f"### {report['title']}")
            origin = report.get("filing_form", "")
            filing_url = report.get("filing_url", "")
            metadata = f"{report['created_at']} · {report['source_name']} · {origin}"
            metadata += f" · [공시 원문]({filing_url})" if filing_url else " · SEC 공시를 거치지 않은 직접 검증"
            st.caption(metadata)
            if report.get("claim"):
                st.write("**공시에서 포착한 주장:** " + report["claim"])
            if report.get("claim_evidence_quote"):
                st.caption("공시 근거 문장: “{}”".format(report["claim_evidence_quote"]))
            if report.get("claim_numeric") is not None:
                comparison_text = "같은 지표·단위·기간의 실제 수치와 비교" if report.get("claim_comparable") else "지표·단위·기간 중 하나가 불일치/미확인이므로 직접 비교하지 않음"
                st.caption("명시 수치 주장: {} {} · {} · 기간 {}".format(report["claim_numeric"], report.get("claim_unit", ""), report.get("claim_metric", ""), report.get("claim_period", "미상")) + " · " + comparison_text)
            st.markdown(f"**판정: {verdict}**")
            st.write(report["summary"])
            c1, c2, c3 = st.columns(3)
            c1.metric("기준값", report.get("baseline_display", "자료 없음"))
            c2.metric("관측값", report.get("observed_display", "자료 없음"))
            c3.metric("괴리", report.get("divergence_display", "계산 불가"))
            st.markdown("**핵심 반론**")
            for point in report.get("red_team", []):
                st.markdown(f"- {point}")
            red_team_analysis = report.get("red_team_analysis", {})
            if red_team_analysis.get("alternative_explanations") or red_team_analysis.get("open_questions"):
                with st.expander("대안 설명 및 열린 질문"):
                    for point in red_team_analysis.get("alternative_explanations", []):
                        st.markdown("- 대안: " + point)
                    for question in red_team_analysis.get("open_questions", []):
                        st.markdown("- 확인 질문: " + question)
            st.markdown("**한계와 다음 확인**")
            st.write(report.get("limitations", ""))
            for evidence in report.get("evidence", []):
                evidence_url = evidence.get("url", "")
                if evidence_url:
                    st.markdown(f"- [{evidence['label']}]({evidence_url}) · {evidence['detail']}")
                else:
                    st.markdown(f"- {evidence['label']} · {evidence['detail']}")
            series = report.get("series", [])
            if len(series) > 1:
                frame = pd.DataFrame(series)
                st.plotly_chart(px.line(frame, x="date", y="value", title="수집 시계열 (원자료)", markers=True), width="stretch")
            analysis = report.get("analysis") or {}
            st.caption("분석 실행: {} · 샌드박스 결과 검산: {} · 자가수정 횟수: {}".format(
                analysis.get("execution_mode", "사용 안 함"),
                "통과" if report.get("sandbox_verified") else "로컬 기준식 사용",
                len(analysis.get("repair_attempts", []))))
            if analysis.get("fallback_reason"):
                st.caption("샌드박스 fallback 사유: " + analysis["fallback_reason"])
            with st.expander("계산 코드와 실행 기록"):
                st.code(analysis.get("code", "계산 자료 없음"), language="python")
                for attempt in analysis.get("repair_attempts", []):
                    st.warning("자가수정 기록: " + attempt)
            with st.expander("구조화 결과(JSON)"):
                st.code(json.dumps(report, ensure_ascii=False, indent=2), language="json")

with tab_about:
    st.subheader("에이전트 구조와 데이터 출처")
    st.markdown("""
- **Baseline:** 투자 메모, 측정 지표, 기준·목표·허용 괴리와 감시 활성 여부를 저장합니다.
- **Event:** 미국 기업은 SEC 8-K/10-Q/10-K, 삼성전자·SK하이닉스는 OpenDART 공시목록을 조회합니다. 한국 기업 공시 본문의 주장 자동 추출은 아직 지원하지 않습니다.
- **Main Engine:** SEC/OpenDART/FRED/Comtrade/SAM 어댑터로 데이터를 수집하고, OpenAI가 만든 계산 코드를 AST 정책 검증 후 E2B 격리 환경에서 실행합니다. 오류 시 최대 2회 자가수정하고 결과를 결정론 기준식과 대조합니다.
- **Verdict:** 독립 Red-Team 에이전트가 대안 설명·반론·열린 질문을 생성하고, 구조화 수치와 출처를 포함한 리포트를 남깁니다. 일치하는 단위/기간이 없으면 명시적 공시 수치의 직접 비교를 보류합니다.
- **Background:** 별도 FastAPI/ APScheduler 서비스가 Streamlit 화면을 닫은 뒤에도 주기적으로 감시합니다. 서비스 상태와 마지막 주기는 사이드바에서 확인합니다.

SEC 공시가 차단되더라도 ‘선택 가정 바로 검증’은 공시 트리거를 건너뛰어 외부 지표·계산·Red-Team을 확인합니다. 설정된 OpenAI/E2B 키로 실행하면 해당 API 사용량이 발생할 수 있습니다.

OpenAI를 켜면 투자 메모, SEC 공시 발췌문과 관측 자료 일부가 OpenAI API로 전송되어 분류·분석됩니다. 조직의 데이터 처리 정책을 먼저 확인하세요. 비밀 키는 프롬프트나 샌드박스에 포함하지 않습니다.

SEC `User-Agent`에는 연락 가능한 이메일이 필요합니다. 한국 기업 공시·재무자료에는 `OPENDART_API_KEY`가 필요합니다. FRED, SAM.gov, OpenAI와 E2B 키는 `.env`에 직접 넣으세요. 무료 API에는 호출 제한과 지연이 있습니다.

#### API 한 줄 설명
- **SEC EDGAR:** 미국 상장사의 공시와 XBRL 재무 수치를 제공합니다.
- **OpenDART:** 한국 상장사의 공시와 정기 재무제표를 제공합니다.
- **FRED:** 미국 연방준비은행 세인트루이스의 거시경제 시계열을 제공합니다.
- **UN Comtrade:** 국가 간 품목별 수출입 통계를 제공합니다.
- **SAM.gov:** 미국 연방정부의 조달·입찰 공고를 제공합니다.
- **OpenAI:** 공시 주장 추출과 독립 반론 생성을 돕습니다.
- **E2B:** 분석 코드를 격리된 클라우드 샌드박스에서 실행합니다.
""")
