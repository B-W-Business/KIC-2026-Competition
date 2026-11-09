# KIC Investment Assumption Watch

KIC 해외 장기 투자 가정을 기준선으로 관리하고, SEC 경영진 공시와 공공 실물·거시 지표를 연결해 명시적 주장과 실제 관측치의 괴리 및 독립적 반론을 제공하는 에이전트형 MVP입니다.

## 실행

Python 3.11 이상에서:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# .env에 개인 연락처와 사용할 API 키를 설정
```

두 프로세스를 별도 터미널에서 실행합니다.

```powershell
# 화면
streamlit run app.py

# 브라우저를 닫아도 실행되는 백그라운드 SEC 감시 서비스
uvicorn kic_watch.api:app --host 127.0.0.1 --port 8000
```

감시 서비스는 시작 직후 전체 활성 포트폴리오를 확인하고, 기본 15분 간격으로 반복합니다. 간격은 `MONITOR_INTERVAL_SECONDS`로 설정합니다. 운영 배포에서 여러 Uvicorn worker를 사용할 경우 각 worker가 중복 스케줄을 돌리지 않도록 별도의 scheduler 프로세스 한 개만 실행하세요.

백그라운드 서비스 확인은 `http://127.0.0.1:8000/health`에서 할 수 있습니다. `/`는 서비스 이름과 상태/API 문서 링크를 안내합니다.

## API 키와 사용자 설정

키를 채팅에 붙여 넣지 말고 로컬 `.env` 파일에 직접 넣으세요. `.env`와 SQLite 데이터는 `.gitignore`에 포함되어 GitHub에 올라가지 않습니다.

| 기능 | 직접 준비할 값 | 발급/설정 방법 |
|---|---|---|
| SEC EDGAR 공시·Company Facts | API 키 불필요. `SEC_USER_AGENT`에 기관/프로젝트 이름과 연락 가능한 이메일 | 예: `SEC_USER_AGENT=KIC Research analyst@example.org`. 유효한 연락처를 넣어야 SEC 데이터 요청을 보낼 수 있습니다. |
| OpenDART 공시·정기 재무제표 | 무료 인증키를 `OPENDART_API_KEY`로 저장 | [OpenDART](https://opendart.fss.or.kr/)에서 키를 발급받으세요. MVP는 삼성전자·SK하이닉스의 정기공시 목록 및 연간 연결 재무지표 조회를 지원합니다. |
| FRED 거시 지표 | `FRED_API_KEY` | [FRED API 키 발급 안내](https://fred.stlouisfed.org/docs/api/api_key.html)에서 발급 후 `.env`에 저장 |
| UN Comtrade 무역 통계 | 기본 공개 API는 키 없이 사용 가능. 계정 키는 `COMTRADE_API_KEY`, 선택 보조 키는 `COMTRADE_SECONDARY_API_KEY` | [UN Comtrade API 포털](https://comtradeplus.un.org/)에서 계정/키를 만들고 `.env`에 저장. 기본 구현은 미국 수입(`flowCode=M`), HS 코드, 세계 전체 상대국을 조회하며, 기본 키 실패 시 보조 키로 재시도합니다. |
| OpenAI 구조화 주장 분류·Red-Team·코드 생성 | `OPENAI_API_KEY` | [OpenAI API 키 페이지](https://platform.openai.com/api-keys)에서 만들고 `.env`에 저장. API 사용 요금은 계정에 청구될 수 있습니다. |
| E2B 격리 Python 샌드박스 | `E2B_API_KEY` | [E2B 계정/API 키](https://e2b.dev/docs)에서 발급 후 `.env`에 저장. 샌드박스 사용량에 따라 비용이 발생할 수 있습니다. |
| SAM.gov 조달 기회 검색 | `SAM_API_KEY` | [SAM.gov 공개 API 안내](https://open.gsa.gov/api/)에서 키를 발급받아 `.env`에 저장 |

처음 실행은 SEC 연락처만으로 가능합니다. OpenDART 대상 한국 기업(삼성전자·SK하이닉스)을 조회하려면 별도 `OPENDART_API_KEY`가 필요합니다. OpenAI/E2B 키가 없으면 공시 키워드 분류, 규칙 기반 반론, 로컬 결정론 계산으로 계속 동작합니다. 감시 대상이 실제 포트폴리오와 다르면 샘플 가정을 편집하고 잘못된 기업은 감시를 끄세요.

OpenAI를 활성화하면 투자 메모, SEC 공시 발췌문 및 관측 자료 일부가 분류와 Red-Team 분석을 위해 OpenAI API로 전송됩니다. E2B를 활성화하면 입력값인 기준값·목표값·허용 괴리와 관측 수치/시계열이 계산을 위해 E2B 샌드박스로 전송됩니다. 조직의 데이터 반출 정책을 확인한 뒤 키를 설정하세요. 비밀 API 키와 전체 투자 메모는 E2B에 보내지 않으며, API 키는 LLM 프롬프트에도 포함되지 않습니다.

## 기능과 워크플로

1. **기준선:** 기업별 투자 메모, 측정 지표, 관측 주기, 데이터 질의값, 기준·목표·허용 괴리와 활성 여부를 SQLite에 저장합니다.
2. **SEC 이벤트:** 20개 반도체/빅테크 기업의 8-K/10-Q/10-K 최신 제출 목록을 검색하고 원문을 가져옵니다. `data.sec.gov` 제출 API가 거부되면 SEC 공식 기업별 EDGAR Atom 피드로 목록 조회를 재시도합니다. 제목/메모와의 구조화 관련성 분류 결과를 영속 캐시해 다음 스캔에서 재분류를 피합니다.
3. **명시 주장 추출:** OpenAI가 근거 문장, 숫자, 지표, 단위와 기간을 JSON Schema로 추출합니다. 숫자나 비교 지표가 분명하지 않으면 비워 둡니다. 같은 지표·단위·기간일 때만 공시 숫자와 관측치를 직접 비교합니다.
4. **공공 데이터 수집:** SEC Company Facts, FRED, UN Comtrade, SAM.gov 어댑터가 원자료·시점·출처를 수집합니다. 실패·rate limit은 지수 백오프로 재시도하며 키 값은 오류/리포트에 마스킹합니다.
5. **격리 계산과 자가 수정:** OpenAI가 짧은 분석 코드를 생성하고 AST 정책 검증을 거친 뒤 E2B에 숫자 기준값·목표값·허용 괴리와 관측 자료를 넣어 실행합니다. 오류가 나면 최대 2회 재작성·재검증합니다. API 키/환경 변수/파일 접근/네트워크 호출/루프/함수 정의는 코드 정책에서 막습니다. 코드 결과는 로컬 결정론 계산으로 대조하며 불일치 시 로컬 기준식을 판정에 사용합니다.
6. **Red-Team:** 별도 프롬프트가 대안 설명, 반론, 열린 질문과 신뢰 수준을 구조화합니다. LLM이 없으면 규칙 기반 반론을 사용합니다. 최종 경고는 계산된 괴리와 명시적 주장을 중심으로 만듭니다.
7. **중복 없는 백그라운드:** FastAPI + APScheduler가 UI가 닫힌 동안에도 감시합니다. 공시/가정 조합에 고유 키를 두고 SQLite transaction으로 한 번만 처리하며, 실패는 최대 3회 재시도합니다. 수동 조회와 백그라운드 조회는 같은 서비스를 공유합니다.

## 구조

```text
app.py                         Streamlit 대시보드
kic_agent.py                   기존 호출부 호환 및 LangGraph workflow
kic_watch/
  agents.py                    공시 주장 추출, Red-Team 에이전트
  api.py                       FastAPI 상태 API와 백그라운드 스케줄러
  code_interpreter.py          코드 정책 검증, E2B 실행, 자가수정/fallback
  monitor.py                   UI와 스케줄러가 공유하는 포트폴리오 감시 서비스
  security.py                  API 키/URL 오류 마스킹
storage.py                     SQLite 저장, 멱등 이벤트/재시도 관리
tests/                         핵심 서비스·보안 테스트
```

## 데이터 해석 범위

- **SAM.gov 연동은 수주액/award가 아닌 계약 기회 공고 수**를 반환합니다. 기업별 계약 수주 검증 자료가 아닙니다.
- FRED와 UN Comtrade 관측은 회사 실적을 직접 귀속하지 않는 경우가 있어 대리 지표 한계를 보고서에 표시합니다.
- SEC XBRL 태그는 회사와 회계기준에 따라 다릅니다. 해외 발행사(예: IFRS 보고사)의 일부 SEC Company Facts 지표는 자료가 없을 수 있습니다.
- EDGAR Atom 대체 경로는 공시 목록용입니다. `data.sec.gov` 전체 접근이 차단된 네트워크에서는 Company Facts 수치 조회도 사용할 수 없으며, 이때 앱은 임의 수치로 대체하지 않고 검증 보류를 표시합니다.
- 매출 성장/총이익률 등은 회계 기간·단위가 맞는 자료만 비교해야 합니다. 리포트는 검토 보조이며 투자 지시가 아닙니다.
- SQLite는 단일 인스턴스 개발/MVP 저장소입니다. 다중 서버 배포에는 중앙 DB와 단일 scheduler/queue가 필요합니다.

## 검증

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app.py kic_agent.py storage.py kic_watch tests
```
