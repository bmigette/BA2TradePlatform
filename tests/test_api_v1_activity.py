"""GET /activity."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import ActivityLog
from ba2_trade_platform.core.types import ActivityLogSeverity, ActivityLogType
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_expert_instance

pytestmark = pytest.mark.api_db


def _log(type_, severity=ActivityLogSeverity.INFO, **kw):
    add_instance(ActivityLog(severity=severity, type=type_, description=f"{type_.value}", data=kw.pop("data", {}),
                             **kw), expunge_after_flush=True)


def test_activity_filters():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    _log(ActivityLogType.ORDER_FILLED, source_expert_id=e.id, source_account_id=acct.id)
    _log(ActivityLogType.TP_SL_ADJUSTED, severity=ActivityLogSeverity.WARNING, data={"source": "api"})
    c = v1_client()
    assert c.get("/activity").json()["total"] == 2
    assert c.get("/activity?type=order_filled").json()["total"] == 1
    assert c.get("/activity?type=ORDER_FILLED").json()["total"] == 1
    assert c.get("/activity?severity=warning").json()["total"] == 1
    assert c.get(f"/activity?expert={e.id}").json()["total"] == 1
    assert c.get(f"/activity?account={acct.id}").json()["total"] == 1
    assert c.get("/activity?type=bogus").status_code == 422
    item = c.get("/activity?type=tp_sl_adjusted").json()["items"][0]
    assert item["data"] == {"source": "api"} and item["severity"] == "warning"
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert c.get("/activity", params={"from": future}).json()["total"] == 0


def test_activity_bound_accepts_aware_timestamps():
    _log(ActivityLogType.ORDER_FILLED)
    plus5 = timezone(timedelta(hours=5))
    now = datetime.now(timezone.utc)
    c = v1_client()
    assert c.get("/activity", params={"from": (now + timedelta(hours=1)).astimezone(plus5).isoformat()}).json()["total"] == 0
    assert c.get("/activity", params={"from": (now - timedelta(hours=1)).astimezone(plus5).isoformat()}).json()["total"] == 1
