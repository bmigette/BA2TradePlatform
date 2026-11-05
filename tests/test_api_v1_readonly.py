"""A management-API GET must not change live trading state: the buying-power failure memory that live sizing
reads, and no retry sleeps on the request thread."""
import threading

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface as MEI
from ba2_trade_platform.core.account_types import AccountSnapshot
from ba2_trade_platform.services import experts as svc
from ba2_trade_platform.services._readonly import readonly_balance_reads
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import create_account_definition, create_expert_instance

pytestmark = pytest.mark.api_db

KEY = 777


@pytest.fixture(autouse=True)
def _clean_memory(monkeypatch):
    MEI._BP_FAILED_READS.pop(KEY, None)
    monkeypatch.setattr(MEI, "_ACTUAL_BP_SLEEP", staticmethod(lambda s: (_ for _ in ()).throw(
        AssertionError("slept on a read-only path"))))
    yield
    MEI._BP_FAILED_READS.pop(KEY, None)


def _failing_account():
    a = FakeAccount(KEY)
    a.snapshot = AccountSnapshot()          # the adapter's "could not read the broker" value
    return a


def test_failed_read_in_scope_raises_and_leaves_the_memory_untouched():
    with readonly_balance_reads():
        with pytest.raises(ValueError):
            MEI._get_actual_available_balance(_failing_account(), expert_id=1)
    assert MEI._remembered_bp_failure(KEY) is None


def test_failed_read_in_scope_keeps_an_existing_entry_exactly():
    MEI._remember_bp_failure(KEY, 3, "live failure")
    before = dict(MEI._BP_FAILED_READS)
    with readonly_balance_reads():
        with pytest.raises(ValueError):
            MEI._get_actual_available_balance(_failing_account(), expert_id=1)
    assert MEI._BP_FAILED_READS == before


def test_successful_read_in_scope_does_not_clear_an_existing_failure():
    MEI._remember_bp_failure(KEY, 3, "live failure")
    with readonly_balance_reads():
        assert MEI._get_actual_available_balance(FakeAccount(KEY), expert_id=1) == 20000.0
    assert MEI._remembered_bp_failure(KEY) is not None


def test_outside_the_scope_live_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setattr(MEI, "_ACTUAL_BP_SLEEP", staticmethod(lambda s: None))
    assert MEI._get_actual_available_balance(_failing_account(), expert_id=1) is None
    assert MEI._remembered_bp_failure(KEY) is not None            # live path still remembers the failure


def test_scope_is_per_thread():
    seen = {}

    def other():
        MEI._ACTUAL_BP_SLEEP = staticmethod(lambda s: None)
        seen["v"] = MEI._get_actual_available_balance(_failing_account(), expert_id=1)

    with readonly_balance_reads():
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert seen["v"] is None                                      # the other thread kept live semantics
    assert MEI._remembered_bp_failure(KEY) is not None


def test_get_expert_with_a_failing_bp_read_is_unmeasurable_and_leaves_the_memory(monkeypatch):
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    MEI._remember_bp_failure(acct.id, 2, "pre-existing")
    before = dict(MEI._BP_FAILED_READS)

    class Expert:
        settings = {"overalloc_min_res_pct": 0.0}

        @classmethod
        def get_merged_settings_definitions(cls):
            return {}

        def describe_capital_mapping(self):
            # what the real method does: ask the (failing) account for its buying power inside the pass
            try:
                MEI._get_actual_available_balance(_failing_account(), expert_id=1)
            except ValueError as err:
                return {"expert_id": 1, "error": str(err)}
            return {"expert_id": 1}

    monkeypatch.setattr(svc, "get_expert_instance_from_id", lambda expert_id: Expert())
    r = v1_client().get(f"/experts/{e.id}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
    assert MEI._BP_FAILED_READS == before


def test_unknown_expert_type_is_422_invalid_not_500(monkeypatch):
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)

    def boom(expert_id):
        raise ValueError("Unknown expert type: Nope")
    monkeypatch.setattr(svc, "get_expert_instance_from_id", boom)
    r = v1_client().get(f"/experts/{e.id}")
    assert r.status_code == 422 and r.json()["error"] == "invalid"
