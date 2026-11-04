"""GET /analyses, /analyses/{id}, /recommendations."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AnalysisOutput
from ba2_trade_platform.core.types import AnalysisUseCase, MarketAnalysisStatus
from tests.api_helpers import v1_client
from tests.factories import (create_account_definition, create_expert_instance, create_market_analysis,
                             create_recommendation)

pytestmark = pytest.mark.api_db


def _setup():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    done = create_market_analysis(symbol="AAPL", expert_instance_id=e.id,
                                  status=MarketAnalysisStatus.COMPLETED, state={"k": "v"})
    skipped = create_market_analysis(symbol="MSFT", expert_instance_id=e.id,
                                     status=MarketAnalysisStatus.SKIPPED,
                                     subtype=AnalysisUseCase.OPEN_POSITIONS,
                                     state={"skipped": True, "skip_reason": "insufficient_balance"})
    return e, done, skipped


def test_analyses_list_filters_and_skip_reason():
    e, done, skipped = _setup()
    c = v1_client()
    body = c.get("/analyses").json()
    by_sym = {i["symbol"]: i for i in body["items"]}
    assert by_sym["MSFT"]["skip_reason"] == "insufficient_balance"
    assert by_sym["AAPL"]["skip_reason"] is None
    assert "state" not in by_sym["AAPL"]                      # list rows stay small
    assert c.get(f"/analyses?expert={e.id}&symbol=AAPL").json()["total"] == 1
    assert c.get("/analyses?status=skipped").json()["total"] == 1
    assert c.get("/analyses?subtype=open_positions").json()["total"] == 1
    assert c.get("/analyses?status=bogus").status_code == 422
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert c.get("/analyses", params={"from": future}).json()["total"] == 0


def test_analysis_detail_state_outputs_and_recommendations():
    e, done, _ = _setup()
    add_instance(AnalysisOutput(market_analysis_id=done.id, name="AAPL_news", type="news",
                                text="x" * 50, blob=b"\x00\x01", provider_metadata={}),
                 expunge_after_flush=True)
    create_recommendation(e.id, symbol="AAPL", market_analysis_id=done.id)
    c = v1_client()
    d = c.get(f"/analyses/{done.id}").json()
    assert d["state"] == {"k": "v"}
    out = d["outputs"][0]
    assert out["name"] == "AAPL_news" and out["text_length"] == 50
    assert "text" not in out and "blob" not in out
    assert d["recommendations"][0]["symbol"] == "AAPL"
    assert c.get(f"/analyses/{done.id}?include_text=true").json()["outputs"][0]["text"] == "x" * 50
    assert c.get("/analyses/99999").status_code == 404


def test_recommendations_list():
    e, done, _ = _setup()
    create_recommendation(e.id, symbol="AAPL", confidence=78.1)
    create_recommendation(e.id, symbol="NVDA")
    c = v1_client()
    assert c.get("/recommendations").json()["total"] == 2
    item = c.get(f"/recommendations?expert={e.id}&symbol=AAPL").json()["items"][0]
    assert item["confidence"] == 78.1 and item["recommended_action"]
    assert c.get("/recommendations?expert=999").json()["total"] == 0
