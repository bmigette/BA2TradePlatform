"""The assigned-stock policy is a deploy-parity fact that live CARRIES (live-assigned-stock-liquidation).

``hold_assigned_stock`` / ``hold_assigned_stock_tags`` decide whether an assignment's stock is held
or liquidated. Live now makes the same decision per instance (``assignment_hold.
decide_live_assigned_stock``), so both rows have a real ``live_setting``: the export carries them in
``expert_params`` (never into the backtest's own expert -- ``applied_in_backtest=False`` keeps the
BT byte-identical) and the importer writes them.
"""
from ba2_common.core.deploy_parity import (
    BACKTEST_FORCED_SETTINGS, BacktestRunFacts, backtest_only_settings, deploy_expert_settings,
    forced_expert_settings, legacy_hold_settings, refuse_undeployable, reportable_backtest_only,
)

_ROWS = {r.key: r for r in BACKTEST_FORCED_SETTINGS}


def _facts(**over):
    base = dict(enable_short=False, hold_assigned_stock=False, entry_action=None)
    base.update(over)
    return BacktestRunFacts(**base)


def test_both_policy_rows_have_a_live_setting_of_the_same_name():
    for key in ("hold_assigned_stock", "hold_assigned_stock_tags"):
        assert _ROWS[key].live_setting == key
        assert _ROWS[key].applied_in_backtest is False


def test_the_backtests_own_expert_settings_are_unchanged():
    """The handler reads forced_expert_settings: it must NOT gain the policy keys (the BT engine
    takes the policy from its account config, and its expert settings stay byte-identical)."""
    forced = forced_expert_settings(_facts(hold_assigned_stock=True, hold_assigned_stock_tags=(7,)))
    assert "hold_assigned_stock" not in forced and "hold_assigned_stock_tags" not in forced


def test_the_export_carries_the_policy_run_level():
    out = deploy_expert_settings(_facts(hold_assigned_stock=True))
    assert out["hold_assigned_stock"] is True
    assert "hold_assigned_stock_tags" not in out          # None is not written


def test_the_export_carries_the_policy_member_scoped_sorted():
    out = deploy_expert_settings(_facts(hold_assigned_stock_tags=(7, 3)))
    assert out["hold_assigned_stock_tags"] == [3, 7]
    assert out["hold_assigned_stock"] is False


def test_a_member_scoped_run_is_no_longer_refused():
    refuse_undeployable(_facts(hold_assigned_stock_tags=(7,)))      # does not raise


def test_the_policy_is_not_a_backtest_only_record_any_more():
    assert set(backtest_only_settings(_facts())) == {"entry_action"}


def test_only_entry_action_is_silent_when_none():
    silent = {r.key for r in BACKTEST_FORCED_SETTINGS if r.silent_when_none}
    assert silent == {"entry_action"}
    assert all(r.live_setting is None for r in BACKTEST_FORCED_SETTINGS if r.silent_when_none)


def test_the_deploy_report_does_not_call_a_carried_key_not_applied():
    assert reportable_backtest_only({"hold_assigned_stock": True, "entry_action": "open_call",
                                     "hold_assigned_stock_tags": [3]}) == {
        "entry_action": "open_call"}
    assert reportable_backtest_only({"entry_action": None, "unknown_row": None}) == {
        "unknown_row": None}
    assert reportable_backtest_only(None) == {} and reportable_backtest_only({}) == {}


def test_a_legacy_payload_policy_is_recovered_from_backtest_only():
    assert legacy_hold_settings({"hold_assigned_stock": True,
                                 "hold_assigned_stock_tags": None,
                                 "entry_action": "x"}) == {"hold_assigned_stock": True}
    assert legacy_hold_settings({"hold_assigned_stock": False}) == {"hold_assigned_stock": False}
    assert legacy_hold_settings({"hold_assigned_stock_tags": [3, 7]}) == {
        "hold_assigned_stock_tags": [3, 7]}
    assert legacy_hold_settings(None) == {} and legacy_hold_settings({}) == {}
