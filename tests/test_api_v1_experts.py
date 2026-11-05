"""GET /experts and /experts/{id}."""
import pytest

from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.services import experts as svc
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_expert_instance, create_market_analysis

pytestmark = pytest.mark.api_db


class FakeExpert:
    settings = {"overalloc_min_res_pct": 10.0, "execution_schedule_enter_market": "{\"days\": [\"monday\"]}",
                "openai_api_key": "fake-secret", "min_confidence": 70}

    @classmethod
    def get_merged_settings_definitions(cls):
        return {"openai_api_key": {"type": "str", "secret": True}, "min_confidence": {"type": "int"}}

    def describe_capital_mapping(self):
        return {"expert_id": 1, "virtual_equity_pct": 50.0, "virtual_balance": 5000.0,
                "used_balance": 1200.0, "available_balance": 3800.0,
                "equivalent_unlevered_balance": 15000.0}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(svc, "get_expert_instance_from_id", lambda expert_id: FakeExpert())


def test_list_has_rows_and_last_analysis_without_touching_the_broker():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id, alias="Alpha")
    create_market_analysis(symbol="AAPL", expert_instance_id=e.id, status=MarketAnalysisStatus.COMPLETED)
    body = v1_client().get(f"/experts?account={acct.id}").json()
    item = body["items"][0]
    assert item["alias"] == "Alpha" and item["last_analysis"]["symbol"] == "AAPL"
    assert item["last_analysis"]["status"] == "completed"


def test_detail_masks_secrets_and_reports_balance_and_reserve(fake):
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id, virtual_equity_pct=50.0)
    d = v1_client().get(f"/experts/{e.id}").json()
    assert d["settings"]["openai_api_key"] == {"set": True}
    assert "fake-secret" not in str(d)
    assert d["settings"]["min_confidence"] == 70
    assert d["overalloc_min_res_pct"] == 10.0
    assert d["capital"]["available_balance"] == 3800.0 and d["capital"]["virtual_balance"] == 5000.0
    assert d["schedule"] == {"execution_schedule_enter_market": "{\"days\": [\"monday\"]}"}
    assert v1_client().get("/experts/99999").status_code == 404


def test_capital_refusal_is_unmeasurable_not_zero(monkeypatch):
    class Refusing(FakeExpert):
        def describe_capital_mapping(self):
            return {"expert_id": 1, "error": "broker published no multiplier"}
    monkeypatch.setattr(svc, "get_expert_instance_from_id", lambda expert_id: Refusing())
    e = create_expert_instance(account_id=create_account_definition().id)
    r = v1_client().get(f"/experts/{e.id}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
