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

from kic_agent import COMPANIES, Assumption, seed_assumptions
from kic_watch.monitor import monitor_ticker
from storage import load_state, save_assumption
st.set_page_config(page_title="KIC Investment Assumption Watch", page_icon="🛡️", layout="wide")


@st.cache_data(ttl=10, show_spinner=False)
def background_service_status(base_url: str) -> dict:
    """Keep sidebar checks fast and avoid blocking every Streamlit rerun."""
    import requests

    response = requests.get(base_url.rstrip("/") + "/health", timeout=(0.2, 0.2))
    response.raise_for_status()
    return response.json()

st.markdown("""
<style>
.stApp {background: #f5f7fb;}
section[data-testid="stSidebar"] {background: #102238;}
section[data-testid="stSidebar"] * {color: #f4f7fb !important;}
.hero {padding: 1.3rem 1.6rem; border-radius: 16px; color:#fff; background:linear-gradient(115deg,#102238,#155d75); margin-bottom:1rem;}
.hero p {color:#d6e6ec; margin-bottom:0;}
div[data-testid="stMetric"] {background:#fff; border:1px solid #e5eaf0; padding:14px; border-radius:12px;}
</style>
""", unsafe_allow_html=True)

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

st.markdown("<div class='hero'><h1>KIC 해외 포트폴리오 투자 가정 감시</h1><p>공시 주장과 공공 데이터의 간극을 추적하고, 독립적 반론을 기록합니다.</p></div>", unsafe_allow_html=True)
st.markdown("**FRED 안내:** 본 서비스는 FRED API를 사용하지만 Federal Reserve Bank of St. Louis의 보증 또는 인증을 받지 않았습니다. [FRED API 이용 약관](https://fred.stlouisfed.org/docs/api/terms_of_use.html)")

with st.sidebar:
    st.header("감시 설정")
    ticker = st.selectbox("기업", list(COMPANIES), format_func=lambda t: f"{t} · {COMPANIES[t]['name']}")
    auto_refresh = st.toggle("정기 재조회", value=False, help="페이지가 열려 있는 동안 SEC 최신 제출 내역을 주기적으로 확인합니다.")
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
    st.caption("SEC 공시 API는 별도 API 키 없이 프로젝트명과 연락처가 포함된 User-Agent를 사용합니다.")
    user_agent = os.getenv("SEC_USER_AGENT", "").lower()
    if not user_agent or "your-contact" in user_agent or "example.org" in user_agent or "example.com" in user_agent:
        st.warning("SEC_USER_AGENT에 프로젝트명과 연락 가능한 이메일을 설정하세요.")
    if st.button("백그라운드 감시 상태 확인"):
        try:
            service = background_service_status(os.getenv("MONITOR_API_URL", "http://127.0.0.1:8000"))
            if service.get("scheduler_running"):
                st.success("백그라운드 감시 실행 중 · {}초 간격".format(service.get("interval_seconds")))
            else:
                st.warning("백그라운드 감시 서비스가 시작 중입니다.")
        except Exception:
            st.warning("백그라운드 API가 연결되지 않았습니다. 터미널에서 `uvicorn kic_watch.api:app` 실행이 필요합니다.")

tab_monitor, tab_baseline, tab_reports, tab_about = st.tabs(["모니터링", "투자 가정", "검증 리포트", "운영 안내"])

with tab_monitor:
    company = COMPANIES[ticker]
    st.subheader(f"{company['name']} ({ticker})")
    col1, col2, col3 = st.columns(3)
    col1.metric("활성 투자 가정", sum(a.ticker == ticker and a.enabled for a in st.session_state.assumptions))
    col2.metric("검증 리포트", sum(r.get("ticker") == ticker for r in st.session_state.reports))
    col3.metric("마지막 확인", st.session_state.get("last_scan", "아직 확인 전"))
    scan_col, _ = st.columns([1, 3])
    with scan_col:
        scan_now = st.button("SEC 최신 공시 확인", type="primary", width="stretch")
    should_scan = scan_now or (auto_refresh and poll_count > 0 and poll_count != st.session_state.get("last_poll_count", -1))
    if should_scan:
        with st.spinner("SEC 제출 내역을 확인하고 등록된 투자 가정을 대조합니다..."):
            result = monitor_ticker(ticker, st.session_state.assumptions)
        for report in result.get("reports", []):
            st.session_state.reports.insert(0, report)
        st.session_state.last_scan = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        st.session_state.last_poll_count = poll_count
        st.session_state.events = result.get("events", [])
        if result.get("error"):
            st.error(result["error"])
        else:
            st.success(f"최근 공시 {len(result['events'])}건 확인 · 가정 연관 이벤트 {len(result['matched'])}건")
            if result.get("source_warning"):
                st.warning(result["source_warning"])
            for warning in result.get("warnings", []):
                st.warning("일부 공시 본문을 처리하지 못했습니다: " + warning)
            for event in result["matched"]:
                st.warning(f"{event['form']} · {event['filing_date']} — {event['assumption_title']}")
                st.link_button("SEC 원문 열기", event["url"])
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
    st.info("운영 팁: 투자 가정에 검증 지표, 기준값, 목표값, 허용 괴리율과 공개 데이터 출처를 함께 등록하면 반복 검증이 가능합니다.")

with tab_baseline:
    st.subheader("초기 투자 가정 기준선")
    st.write("이 기준선은 이후 검증에서 바뀌지 않는 비교 기준으로 사용됩니다. 가정 하나에 관측 가능한 지표 하나를 연결하세요.")
    with st.form("assumption_form", clear_on_submit=True):
        a_title = st.text_input("가정 제목", placeholder="예: AI 서버 수요에 따라 데이터센터 매출이 연 25% 이상 성장")
        a_text = st.text_area("투자 메모 / 핵심 주장", placeholder="투자 당시 근거와 전제가 된 경영진 주장을 기록")
        a_metric = st.selectbox("검증 지표", ["revenue_growth", "gross_margin", "capex_growth", "fred_series", "trade_value", "sam_awards"])
        a_source = {"revenue_growth": "SEC Company Facts", "gross_margin": "SEC Company Facts",
                    "capex_growth": "SEC Company Facts", "fred_series": "FRED",
                    "trade_value": "UN Comtrade", "sam_awards": "SAM.gov"}[a_metric]
        st.caption("선택 지표의 데이터 출처: " + a_source)
        c1, c2, c3 = st.columns(3)
        unit_label = "%" if a_metric in ("revenue_growth", "gross_margin", "capex_growth", "trade_value") else ("건" if a_metric == "sam_awards" else "지표 단위")
        a_baseline = c1.number_input(f"기준값 ({unit_label})", value=0.0, step=1.0)
        a_target = c2.number_input(f"목표값 ({unit_label})", value=20.0, step=1.0)
        a_tolerance = c3.number_input(f"허용 괴리 ({unit_label})", value=10.0, min_value=0.0, step=1.0)
        a_note = st.text_input("추가 검증 키 (예: FRED series ID, Comtrade HS 코드)", help="FRED는 series ID, Comtrade는 HS 코드. SAM.gov는 키워드.")
        submitted = st.form_submit_button("기준선 등록", type="primary")
    if submitted and a_title.strip():
        assumption = Assumption.create(
            ticker=ticker, title=a_title.strip(), memo=a_text.strip(), metric=a_metric,
            source=a_source, baseline=a_baseline, target=a_target,
            tolerance=a_tolerance, query=a_note.strip())
        st.session_state.assumptions.append(assumption)
        save_assumption(assumption)
        st.success("투자 가정을 기준선에 등록했습니다.")
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
        st.caption("아직 검증 리포트가 없습니다. 모니터링 탭에서 SEC 공시를 확인하세요.")
    for report in reports:
        verdict = report.get("verdict", "검토 필요")
        with st.container(border=True):
            st.markdown(f"### {report['title']}")
            st.caption(f"{report['created_at']} · {report['source_name']} · {report['filing_form']} · [SEC 공시 원문]({report['filing_url']})")
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
                st.markdown(f"- [{evidence['label']}]({evidence['url']}) · {evidence['detail']}")
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
- **Event:** SEC 8-K/10-Q/10-K 공시를 찾고 관련 주장을 구조화 추출합니다. 공시 매칭은 로컬 SQLite에 캐시되어 반복 LLM 호출을 피합니다.
- **Main Engine:** SEC/FRED/Comtrade/SAM 어댑터로 데이터를 수집하고, OpenAI가 만든 계산 코드를 AST 정책 검증 후 E2B 격리 환경에서 실행합니다. 오류 시 최대 2회 자가수정하고 결과를 결정론 기준식과 대조합니다.
- **Verdict:** 독립 Red-Team 에이전트가 대안 설명·반론·열린 질문을 생성하고, 구조화 수치와 출처를 포함한 리포트를 남깁니다. 일치하는 단위/기간이 없으면 명시적 공시 수치의 직접 비교를 보류합니다.
- **Background:** 별도 FastAPI/ APScheduler 서비스가 Streamlit 화면을 닫은 뒤에도 주기적으로 감시합니다. 서비스 상태와 마지막 주기는 사이드바에서 확인합니다.

OpenAI를 켜면 투자 메모, SEC 공시 발췌문과 관측 자료 일부가 OpenAI API로 전송되어 분류·분석됩니다. 조직의 데이터 처리 정책을 먼저 확인하세요. 비밀 키는 프롬프트나 샌드박스에 포함하지 않습니다.

SEC `User-Agent`에는 실제 연락처가 필요합니다. FRED, SAM.gov, OpenAI와 E2B 키는 `.env`에 직접 넣으세요. Comtrade는 키 없는 공개 엔드포인트를 기본 사용합니다. 무료 API에는 호출 제한과 지연이 있습니다.
""")
