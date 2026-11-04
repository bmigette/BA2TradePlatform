"""services.experts writes: PATCH instance, PATCH settings, the shared market-gate check."""
import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.services import experts as svc
from ba2_trade_platform.services.errors import Conflict, Invalid, NotFound
from tests.api_helpers import SK
from tests.factories import create_account_definition, create_expert_instance

pytestmark = pytest.mark.api_db

SAVED = []


class FakeExpert:
    DEFS = {"min_confidence": {"type": "int"}, "threshold": {"type": "float"}, "use_news": {"type": "bool"},
            "mode": {"type": "str", "valid_values": ["fast", "slow"]}, "openai_api_key": {"type": "str", "secret": True},
            "harmless_name": {"type": "str", "sensitive": True}, "universe": {"type": "list"},
            "limits": {"type": "json"}, svc.MARKET_CONDITION_PROFILE_SETTING: {"type": "str"}}

    def __init__(self, expert_id=1):
        self.id = expert_id
        self.settings = {"min_confidence": 70, "threshold": 0.5, "openai_api_key": SK + "OLDKEY123456",
                         "use_news": True}

    @classmethod
    def get_merged_settings_definitions(cls):
        return cls.DEFS

    def save_setting(self, key, value, setting_type=None):
        SAVED.append((self.id, key, value, setting_type))


@pytest.fixture
def env(monkeypatch):
    SAVED.clear()
    refreshed, dropped = [], []
    monkeypatch.setattr(svc, "_live_expert", lambda expert_id: FakeExpert(expert_id))
    monkeypatch.setattr(svc, "refresh_schedules", refreshed.append)
    monkeypatch.setattr(svc, "_drop_caches", dropped.append)
    return refreshed, dropped


def _expert(**kw):
    return create_expert_instance(account_id=create_account_definition().id, **kw)


# --- update_expert -----------------------------------------------------------------------------------------------

def test_update_expert_writes_and_refreshes(env):
    refreshed, dropped = env
    e = _expert(enabled=True, virtual_equity_pct=100.0)
    out = svc.update_expert(e.id, enabled=False, virtual_equity_pct=50.0, dry_run=False)
    row = get_instance(ExpertInstance, e.id)
    assert (row.enabled, row.virtual_equity_pct) == (False, 50.0)
    assert out["changed"] == ["enabled", "virtual_equity_pct"] and out["previous"]["enabled"] is True
    assert refreshed == [e.id] and dropped == [e.id]


def test_update_expert_no_change_writes_nothing(env):
    refreshed, _ = env
    e = _expert(enabled=True, virtual_equity_pct=100.0)
    out = svc.update_expert(e.id, enabled=True, virtual_equity_pct=100.0, dry_run=False)
    assert out["changed"] == [] and refreshed == []


def test_update_expert_dry_run_writes_nothing(env):
    refreshed, _ = env
    e = _expert(enabled=True)
    out = svc.update_expert(e.id, enabled=False, virtual_equity_pct=None, dry_run=True)
    assert out["changed"] == ["enabled"] and get_instance(ExpertInstance, e.id).enabled is True and refreshed == []


@pytest.mark.parametrize("kw", [{}, {"virtual_equity_pct": 0.0}, {"virtual_equity_pct": -5.0},
                                {"virtual_equity_pct": float("nan")}, {"virtual_equity_pct": 5000.0},
                                {"virtual_equity_pct": True}, {"enabled": "yes"}])
def test_update_expert_rejects_bad_input(env, kw):
    e = _expert()
    with pytest.raises(Invalid):
        svc.update_expert(e.id, enabled=kw.get("enabled"), virtual_equity_pct=kw.get("virtual_equity_pct"),
                          dry_run=False)


def test_update_expert_unknown_is_404(env):
    with pytest.raises(NotFound):
        svc.update_expert(999, enabled=True, virtual_equity_pct=None, dry_run=False)


def test_enabling_runs_the_market_gate_refusal_and_disabling_does_not(env, monkeypatch):
    def refuse(enter, exit_, profile):
        raise ValueError("enter-market ruleset 3 uses a market leaf the profile does not serve")
    monkeypatch.setattr(svc, "check_market_gates", refuse)
    off = _expert(enabled=False)
    with pytest.raises(Conflict, match="cannot be enabled"):
        svc.update_expert(off.id, enabled=True, virtual_equity_pct=None, dry_run=False)
    assert get_instance(ExpertInstance, off.id).enabled is False
    on = _expert(enabled=True)
    svc.update_expert(on.id, enabled=False, virtual_equity_pct=None, dry_run=False)
    assert get_instance(ExpertInstance, on.id).enabled is False


# --- update_expert_settings --------------------------------------------------------------------------------------

def test_settings_are_validated_then_saved_with_their_declared_type(env):
    refreshed, dropped = env
    e = _expert()
    out = svc.update_expert_settings(e.id, {"min_confidence": 80, "threshold": 0.7, "use_news": False, "mode": "fast",
                                            "universe": ["AAPL"], "limits": {"a": 1}}, dry_run=False)
    saved = {k: (v, t) for (_, k, v, t) in SAVED}
    assert saved["min_confidence"] == (80, "int") and saved["use_news"] == (False, "bool")
    assert saved["universe"] == (["AAPL"], "list") and saved["limits"] == ({"a": 1}, "json")
    assert sorted(out["changed"]) == sorted(saved)
    assert refreshed == [e.id] and dropped == [e.id]


@pytest.mark.parametrize("values", [
    {"nope": 1}, {}, {"min_confidence": "80"}, {"min_confidence": 80.5}, {"min_confidence": True},
    {"threshold": float("inf")}, {"use_news": "false"}, {"mode": "medium"}, {"universe": "AAPL"}, {"limits": 3},
    {"min_confidence": 80, "nope": 1},
])
def test_bad_settings_write_nothing(env, values):
    e = _expert()
    with pytest.raises(Invalid):
        svc.update_expert_settings(e.id, values, dry_run=False)
    assert SAVED == []


def test_equal_values_are_not_rewritten(env):
    e = _expert()
    out = svc.update_expert_settings(e.id, {"min_confidence": 70, "threshold": 0.9}, dry_run=False)
    assert [k for (_, k, _, _) in SAVED] == ["threshold"]
    assert out["unchanged"] == ["min_confidence"]


def test_secrets_are_masked_in_the_answer(env):
    e = _expert()
    out = svc.update_expert_settings(e.id, {"openai_api_key": SK + "NEWKEY987654", "harmless_name": "secretvalue"},
                                     dry_run=False)
    text = str(out)
    assert "NEWKEY987654" not in text and "OLDKEY123456" not in text and "secretvalue" not in text
    assert out["current"]["openai_api_key"] == {"set": True}
    assert any(k == "openai_api_key" and v == SK + "NEWKEY987654" for (_, k, v, _) in SAVED)    # but it IS saved


def test_secret_keys_listing_covers_flagged_and_named(env):
    assert set(svc.secret_setting_keys(1)) == {"openai_api_key", "harmless_name"}


def test_settings_dry_run_saves_nothing(env):
    refreshed, _ = env
    e = _expert()
    out = svc.update_expert_settings(e.id, {"threshold": 0.9}, dry_run=True)
    assert out["changed"] == ["threshold"] and SAVED == [] and refreshed == []


def test_changing_the_profile_runs_the_gate_check(env, monkeypatch):
    seen = []

    def check(enter, exit_, profile):
        seen.append(profile())
        raise ValueError("gate not served")
    monkeypatch.setattr(svc, "check_market_gates", check)
    e = _expert()
    with pytest.raises(Conflict, match="gate not served"):
        svc.update_expert_settings(e.id, {svc.MARKET_CONDITION_PROFILE_SETTING: "ohlcv-v1"}, dry_run=False)
    assert seen == ["ohlcv-v1"] and SAVED == []


def test_a_failing_save_reports_what_was_already_written(env, monkeypatch):
    def flaky(self, key, value, setting_type=None):
        if key == "threshold":
            raise RuntimeError("db locked")
        SAVED.append((self.id, key, value, setting_type))
    monkeypatch.setattr(FakeExpert, "save_setting", flaky)
    e = _expert()
    with pytest.raises(Invalid) as err:
        svc.update_expert_settings(e.id, {"min_confidence": 90, "threshold": 0.9}, dry_run=False)
    assert err.value.detail["saved"] == ["min_confidence"] and "db locked" not in err.value.message


# --- check_market_gates ------------------------------------------------------------------------------------------

def test_check_market_gates_is_lazy_about_the_profile_when_no_leaf(monkeypatch):
    import ba2_common.core.market_condition_live as live
    monkeypatch.setattr(live, "ruleset_rule_contents", lambda rid: [])
    monkeypatch.setattr(svc, "assert_market_rule_actions_live", lambda contents, where: None)
    monkeypatch.setattr(live, "market_condition_fields_in_ruleset", lambda rid: set())

    def explode():
        raise AssertionError("must not be read")
    svc.check_market_gates(1, 2, explode)


def test_check_market_gates_refuses_an_unserved_leaf(monkeypatch):
    import ba2_common.core.market_condition_live as live
    monkeypatch.setattr(live, "ruleset_rule_contents", lambda rid: [])
    monkeypatch.setattr(svc, "assert_market_rule_actions_live", lambda contents, where: None)
    monkeypatch.setattr(live, "market_condition_fields_in_ruleset", lambda rid: {"spy_trend"} if rid == 1 else set())
    monkeypatch.setattr(svc, "assert_fields_served",
                        lambda fields, profiles, where: (_ for _ in ()).throw(ValueError(f"{where}: unserved")))
    with pytest.raises(ValueError, match="enter-market ruleset 1"):
        svc.check_market_gates(1, None, lambda: svc.MARKET_CONDITION_PROFILE_OFF)
