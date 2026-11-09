import os
import datetime
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from kic_watch.code_interpreter import DETERMINISTIC_CODE, _execute, analyze, validate_code
from kic_watch.security import safe_error
from kic_watch.agents import validate_claim_evidence
from kic_watch.workflow import _comparison_node
from kic_watch.workflow import run_review
from kic_watch.models import Assumption
from kic_watch.providers.sec import _latest_matching_facts
from kic_watch.providers import comtrade as comtrade_provider, fred as fred_provider, sam as sam_provider, sec as sec_provider
import storage


class CodeInterpreterTests(unittest.TestCase):
    def test_deterministic_code_passes_policy(self):
        self.assertEqual(validate_code(DETERMINISTIC_CODE), DETERMINISTIC_CODE)

    def test_rejects_network_and_file_access(self):
        with self.assertRaises(ValueError):
            validate_code("import os\nresult = {}\nprint(json.dumps(result))")
        with self.assertRaises(ValueError):
            validate_code("result = open('secret').read()\nprint(json.dumps(result))")

    def test_local_fallback_produces_expected_deltas(self):
        os.environ.pop("E2B_API_KEY", None)
        result = analyze({"baseline": 20, "target": 25, "tolerance": 10,
                          "observed": 12, "series": [10, 12]})
        self.assertEqual(result["execution_mode"], "local_fallback")
        self.assertEqual(result["result"]["divergence"], -13)
        self.assertEqual(result["result"]["baseline_delta"], -8)

    def test_e2b_and_openai_self_correction_path(self):
        calls = {"sandbox": 0, "llm": 0, "code": "", "prompt": ""}
        secret = "e2b_fake_secret_123456789012345"

        class FakeExecution:
            def __init__(self, fail=False):
                self.error = types.SimpleNamespace(name="RuntimeError", value="synthetic failure") if fail else None
                self.logs = types.SimpleNamespace(stdout=['{"divergence": -13, "baseline_delta": -8, "series_mean": 11}'])
                self.text = ""

        class FakeSandbox:
            def run_code(self, code, timeout=None):
                calls["sandbox"] += 1
                calls["code"] = code
                return FakeExecution(fail=calls["sandbox"] == 1)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeSandboxFactory:
            @staticmethod
            def create(**kwargs):
                self.assertEqual(kwargs["api_key"], secret)
                return FakeSandbox()

        class FakeCompletion:
            @property
            def message(self):
                return types.SimpleNamespace(content='{"code": ' + __import__('json').dumps(DETERMINISTIC_CODE) + '}')

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                calls["llm"] += 1
                calls["prompt"] = str(kwargs["messages"])
                return types.SimpleNamespace(choices=[FakeCompletion()])

        old_e2b = sys.modules.get("e2b_code_interpreter")
        old_openai = sys.modules.get("openai")
        sys.modules["e2b_code_interpreter"] = types.SimpleNamespace(Sandbox=FakeSandboxFactory)
        sys.modules["openai"] = types.SimpleNamespace(OpenAI=FakeOpenAI)
        old_e2b_key, old_openai_key = os.environ.get("E2B_API_KEY"), os.environ.get("OPENAI_API_KEY")
        os.environ["E2B_API_KEY"], os.environ["OPENAI_API_KEY"] = secret, "sk_fake_openai_secret_123456789012345"
        try:
            result = analyze({"baseline": 20, "target": 25, "tolerance": 10,
                              "observed": 12, "series": [10, 12]})
        finally:
            if old_e2b_key is None:
                os.environ.pop("E2B_API_KEY", None)
            else:
                os.environ["E2B_API_KEY"] = old_e2b_key
            if old_openai_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_openai_key
            if old_e2b is None:
                sys.modules.pop("e2b_code_interpreter", None)
            else:
                sys.modules["e2b_code_interpreter"] = old_e2b
            if old_openai is None:
                sys.modules.pop("openai", None)
            else:
                sys.modules["openai"] = old_openai
        self.assertEqual(result["execution_mode"], "e2b")
        self.assertEqual(len(result["repair_attempts"]), 1)
        self.assertEqual(calls["sandbox"], 2)
        self.assertEqual(calls["llm"], 2)
        self.assertNotIn(secret, calls["code"])
        self.assertNotIn(secret, calls["prompt"])


class CredentialRedactionTests(unittest.TestCase):
    def test_redacts_provider_keys_from_exception_urls(self):
        value = safe_error("bad request https://api.example.test/?api_key=secret123&series_id=ABC")
        self.assertNotIn("secret123", value)
        self.assertIn("[REDACTED]", value)
        self.assertIn("series_id=ABC", value)
        self.assertNotIn("sk_fake_openai_secret_123456789012345", safe_error("Bearer sk_fake_openai_secret_123456789012345"))


class ComparisonTests(unittest.TestCase):
    def test_unverified_numeric_claim_is_never_used(self):
        claim = {"evidence_quote": "Revenue grew 30% in FY2025.", "numeric_claim": 30,
                 "claim_unit": "%", "claim_period": "FY2025", "reason": ""}
        validate_claim_evidence("Management said revenue grew 20% in FY2025.", claim)
        self.assertFalse(claim["claim_verified"])
        self.assertIsNone(claim["numeric_claim"])

    def test_quote_number_unit_period_are_verified(self):
        claim = {"evidence_quote": "Revenue grew 30% in FY2025.", "numeric_claim": 30,
                 "claim_unit": "%", "claim_period": "FY2025", "reason": ""}
        validate_claim_evidence("Statement: Revenue grew 30% in FY2025. Additional text.", claim)
        self.assertTrue(claim["claim_verified"])
        self.assertEqual(claim["numeric_claim"], 30)

    def test_fiscal_period_is_verified_against_natural_language_quote(self):
        claim = {"evidence_quote": "Revenue grew 30% for the fiscal year ended January 26, 2025.",
                 "numeric_claim": 30, "claim_unit": "%", "claim_period": "FY2025", "reason": ""}
        validate_claim_evidence("Management reported: Revenue grew 30% for the fiscal year ended January 26, 2025.", claim)
        self.assertEqual(claim["claim_period"], "FY2025")
        self.assertTrue(claim["claim_verified"])

    def test_claim_requires_matching_period_metric_and_unit(self):
        assumption = {"id": "a", "ticker": "NVDA", "metric": "revenue_growth", "baseline": 20,
                      "target": 25, "tolerance": 5, "title": "revenue", "memo": ""}
        event = {"claim": "revenue grew 30%", "numeric_claim": 30, "claim_metric": "revenue_growth",
                 "claim_unit": "%", "claim_period": "FY2025"}
        state = {"assumption": assumption, "event": event,
                 "observation": {"value": 10, "unit": "%", "period_key": "FY2024"}}
        result = _comparison_node(state)
        self.assertFalse(result["comparison"]["claim_comparable"])
        state["observation"]["period_key"] = "FY2025"
        result = _comparison_node(state)
        self.assertTrue(result["comparison"]["claim_comparable"])
        self.assertEqual(result["comparison"]["verdict"], "경영진 주장 미달 위험")

    def test_gross_margin_matches_same_xbrl_period_and_duration(self):
        facts = {
            "GrossProfit": {"units": {"USD": [
                {"val": 40, "start": "2024-01-01", "end": "2024-12-31", "fy": 2024, "fp": "FY", "form": "10-K"},
                {"val": 50, "start": "2025-01-01", "end": "2025-12-31", "fy": 2025, "fp": "FY", "form": "10-K"}]}},
            "Revenue": {"units": {"USD": [
                {"val": 100, "start": "2024-01-01", "end": "2024-12-31", "fy": 2024, "fp": "FY", "form": "10-K"},
                {"val": 200, "start": "2025-01-01", "end": "2025-12-31", "fy": 2025, "fp": "FY", "form": "10-K"}]}}
        }
        gross, revenue = _latest_matching_facts(facts, "GrossProfit", "Revenue")
        self.assertEqual(gross["period_key"], "FY2025")
        self.assertEqual(revenue["value"], 200)


class WorkflowIntegrationTests(unittest.TestCase):
    def test_langgraph_review_graph_runs_all_nodes(self):
        assumption = Assumption.create(ticker="NVDA", title="Revenue", memo="", metric="revenue_growth",
                                       source="SEC", baseline=20, target=25, tolerance=5, query="")
        observation = {"value": 12.0, "unit": "%", "period_key": "FY2025", "series": [],
                       "source_name": "SEC Facts", "url": "https://data.sec.gov/", "detail": "fixture"}
        event = {"form": "10-Q", "url": "https://www.sec.gov/Archives/fixture", "accession": "0001",
                 "claim": "revenue grew", "evidence_quote": "Revenue grew 12% in FY2025.",
                 "numeric_claim": 12, "claim_metric": "revenue_growth", "claim_unit": "%", "claim_period": "FY2025"}
        old_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            with patch("kic_watch.workflow.collect_observation", return_value=observation):
                report = run_review(assumption, event)
        finally:
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
        self.assertTrue(report["claim_comparable"])
        self.assertEqual(report["verdict"], "가정 훼손 위험")
        self.assertEqual(report["filing_accession"], "0001")
        self.assertTrue(report["sandbox_verified"])  # local deterministic calculation matches comparison
        self.assertTrue(report["red_team"])


class BackgroundServiceTests(unittest.TestCase):
    def test_api_root_points_to_health_and_docs(self):
        from kic_watch import api

        payload = api.root()
        self.assertEqual(payload["health"], "/health")
        self.assertEqual(payload["docs"], "/docs")

    def test_health_endpoint_reports_scheduler_without_exposing_portfolio_data(self):
        from kic_watch import api

        class RunningScheduler:
            running = True

            @staticmethod
            def get_job(_job_id):
                return types.SimpleNamespace(next_run_time=None)

        with patch.object(api, "scheduler", RunningScheduler()):
            payload = api.health()
        self.assertTrue(payload["scheduler_running"])
        self.assertNotIn("reports", payload)
        self.assertNotIn("assumptions", payload)


class ProviderMappingTests(unittest.TestCase):
    def test_sec_403_error_explains_user_agent_or_network_cause(self):
        message = sec_provider._sec_error(RuntimeError("HTTP 403 Forbidden"))
        self.assertIn("SEC_USER_AGENT", message)
        self.assertIn("403", message)

    def test_sec_atom_fallback_parses_official_company_feed(self):
        xml = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>urn:tag:sec.gov,2008:accession-number=0000937966-26-000001</id>
            <category term="10-Q"/><content><form>10-Q</form><filing-date>2026-11-09</filing-date>
              <accession-number>0000937966-26-000001</accession-number>
              <filing-href>https://www.sec.gov/Archives/edgar/data/937966/000093796626000001/0000937966-26-000001-index.htm</filing-href>
            </content></entry></feed>'''
        original = sec_provider.SESSION.get
        sec_provider.SESSION.get = lambda *args, **kwargs: types.SimpleNamespace(content=xml, raise_for_status=lambda: None)
        try:
            rows = sec_provider._atom_filing_rows("ASML")
        finally:
            sec_provider.SESSION.get = original
        self.assertEqual(rows[0]["form"], "10-Q")
        self.assertEqual(rows[0]["accession"], "0000937966-26-000001")
        self.assertIn("-index.htm", rows[0]["filing_url"])

    def test_sec_scan_uses_atom_when_submissions_api_is_forbidden(self):
        rows = [{"form": "10-Q", "filing_date": "2026-11-09", "accession": "0000937966-26-000001",
                 "primary_document": "", "filing_url": "https://www.sec.gov/Archives/edgar/data/937966/example-index.htm"}]
        with patch.object(sec_provider, "_get_json", side_effect=RuntimeError("HTTP 403 Forbidden")), \
                patch.object(sec_provider, "_atom_filing_rows", return_value=rows):
            result = sec_provider.scan_company("ASML", [])
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("Atom", result["source_warning"])

    def test_sec_provider_sends_configured_user_agent(self):
        captured = {}
        original = sec_provider._get_json

        def fake_get_json(url, params=None, timeout=25, headers=None):
            captured.update(url=url, headers=headers)
            return {"facts": {"us-gaap": {}}}

        sec_provider._get_json = fake_get_json
        old_user_agent = os.environ.get("SEC_USER_AGENT")
        os.environ["SEC_USER_AGENT"] = "KIC Research analyst@institution.org"
        try:
            with self.assertRaises(RuntimeError):
                sec_provider._sec_company_fact("NVDA", "revenue_growth")
        finally:
            sec_provider._get_json = original
            if old_user_agent is None:
                os.environ.pop("SEC_USER_AGENT", None)
            else:
                os.environ["SEC_USER_AGENT"] = old_user_agent
        self.assertEqual(captured["headers"]["User-Agent"], "KIC Research analyst@institution.org")

    def test_fred_selects_latest_observation_after_fetching_recent_window(self):
        captured = {}
        original = fred_provider._get_json
        fred_provider._get_json = lambda url, params: (captured.update(url=url, params=params) or {"observations": [
            {"date": "2026-11-10", "value": "4.1"}, {"date": "2026-11-09", "value": "4.2"}]})
        old_key = os.environ.get("FRED_API_KEY")
        os.environ["FRED_API_KEY"] = "fake-fred-key"
        try:
            result = fred_provider._fred("DGS10")
        finally:
            fred_provider._get_json = original
            if old_key is None:
                os.environ.pop("FRED_API_KEY", None)
            else:
                os.environ["FRED_API_KEY"] = old_key
        self.assertEqual(captured["params"]["sort_order"], "desc")
        self.assertEqual(result["date"], "2026-11-10")
        self.assertEqual(result["value"], 4.1)

    def test_sam_uses_supported_title_filter_and_one_year_window(self):
        captured = {}
        original = sam_provider._get_json
        sam_provider._get_json = lambda url, params: (captured.update(url=url, params=params) or {"totalRecords": 3})
        old_key = os.environ.get("SAM_API_KEY")
        os.environ["SAM_API_KEY"] = "fake-sam-key-for-tests"
        try:
            result = sam_provider._sam("NVIDIA accelerator")
        finally:
            sam_provider._get_json = original
            if old_key is None:
                os.environ.pop("SAM_API_KEY", None)
            else:
                os.environ["SAM_API_KEY"] = old_key
        posted_from = datetime.datetime.strptime(captured["params"]["postedFrom"], "%m/%d/%Y").date()
        posted_to = datetime.datetime.strptime(captured["params"]["postedTo"], "%m/%d/%Y").date()
        self.assertLessEqual((posted_to - posted_from).days, 365)
        self.assertEqual(captured["params"]["title"], "NVIDIA accelerator")
        self.assertNotIn("q", captured["params"])
        self.assertEqual(captured["url"], "https://api.sam.gov/opportunities/v2/search")
        self.assertEqual(result["value"], 3)

    def test_comtrade_uses_api_import_flow_code(self):
        captured = []
        original = comtrade_provider._get_json
        original_sleep = comtrade_provider.time.sleep

        def fake_get_json(url, params):
            captured.append((url, dict(params)))
            value = 100 if len(captured) == 1 else 110
            return {"data": [{"period": params["period"], "primaryValue": value}]}

        comtrade_provider._get_json = fake_get_json
        comtrade_provider.time.sleep = lambda _seconds: None
        old_primary = os.environ.get("COMTRADE_API_KEY")
        old_secondary = os.environ.get("COMTRADE_SECONDARY_API_KEY")
        os.environ["COMTRADE_API_KEY"] = "fake-primary-key"
        os.environ["COMTRADE_SECONDARY_API_KEY"] = "fake-secondary-key"
        try:
            result = comtrade_provider._comtrade("8542")
        finally:
            comtrade_provider._get_json = original
            comtrade_provider.time.sleep = original_sleep
            if old_primary is None:
                os.environ.pop("COMTRADE_API_KEY", None)
            else:
                os.environ["COMTRADE_API_KEY"] = old_primary
            if old_secondary is None:
                os.environ.pop("COMTRADE_SECONDARY_API_KEY", None)
            else:
                os.environ["COMTRADE_SECONDARY_API_KEY"] = old_secondary
        self.assertEqual(len(captured), 2)
        periods = [int(params["period"]) for _, params in captured]
        self.assertEqual(periods[1] - periods[0], 1)
        self.assertTrue(all(params["flowCode"] == "M" for _, params in captured))
        self.assertTrue(all(params["subscription-key"] == "fake-primary-key" for _, params in captured))
        self.assertAlmostEqual(result["value"], 10)

    def test_comtrade_retries_with_secondary_key(self):
        captured = []
        original = comtrade_provider._get_json
        original_sleep = comtrade_provider.time.sleep

        def fake_get_json(url, params):
            captured.append((params["subscription-key"], params["period"]))
            if len(captured) == 1:
                raise RuntimeError("primary rejected")
            value = 100 if params["period"] == captured[1][1] else 110
            return {"data": [{"period": params["period"], "primaryValue": value}]}

        comtrade_provider._get_json = fake_get_json
        comtrade_provider.time.sleep = lambda _seconds: None
        old_primary = os.environ.get("COMTRADE_API_KEY")
        old_secondary = os.environ.get("COMTRADE_SECONDARY_API_KEY")
        os.environ["COMTRADE_API_KEY"] = "fake-primary-key"
        os.environ["COMTRADE_SECONDARY_API_KEY"] = "fake-secondary-key"
        try:
            result = comtrade_provider._comtrade("8542")
        finally:
            comtrade_provider._get_json = original
            comtrade_provider.time.sleep = original_sleep
            if old_primary is None:
                os.environ.pop("COMTRADE_API_KEY", None)
            else:
                os.environ["COMTRADE_API_KEY"] = old_primary
            if old_secondary is None:
                os.environ.pop("COMTRADE_SECONDARY_API_KEY", None)
            else:
                os.environ["COMTRADE_SECONDARY_API_KEY"] = old_secondary
        self.assertEqual([key for key, _ in captured], ["fake-primary-key", "fake-secondary-key", "fake-primary-key"])
        self.assertAlmostEqual(result["value"], 10)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = storage.DB_PATH
        storage.DB_PATH = Path(self.temp.name) / "state.sqlite3"

    def tearDown(self):
        storage.DB_PATH = self.original
        self.temp.cleanup()

    def test_event_claim_is_persistent_and_idempotent(self):
        key = ("NVDA", "0001-01-01", "assumption-1")
        self.assertTrue(storage.claim_event(*key))
        self.assertFalse(storage.claim_event(*key))
        report = {"id": "report-1", "created_at": "2026-11-09T00:00:00+00:00",
                  "ticker": "NVDA", "verdict": "주의 관찰"}
        storage.finish_event(*key, report)
        self.assertFalse(storage.claim_event(*key))
        assumptions, reports = storage.load_state()
        self.assertEqual(reports[0]["id"], "report-1")

    def test_failed_event_has_bounded_retries(self):
        key = ("NVDA", "0001-01-02", "assumption-1")
        self.assertTrue(storage.claim_event(*key, max_attempts=2))
        storage.fail_event(*key, "temporary issue")
        self.assertTrue(storage.claim_event(*key, max_attempts=2))
        storage.fail_event(*key, "still failed")
        self.assertFalse(storage.claim_event(*key, max_attempts=2))


if __name__ == "__main__":
    unittest.main()
