"""Stage-2 E2 review I1: the member-scoped hold is a deploy-parity fact, and it cannot deploy.

``hold_assigned_stock_tags`` decides, per style member, whether an assignment's stock is held or
liquidated. Live has no such decision at all (it never sells assigned stock), so a run that used
it must not export as ``hold_assigned_stock: False`` -- that would describe a different run. The
table records it, and ``refuse_undeployable`` refuses the export by name.
"""
import pytest

from ba2_common.core.deploy_parity import (
    BACKTEST_FORCED_SETTINGS, BacktestRunFacts, UndeployableRunRefused, backtest_only_settings,
    refuse_undeployable,
)


def _facts(**over):
    base = dict(enable_short=False, hold_assigned_stock=False, entry_action=None)
    base.update(over)
    return BacktestRunFacts(**base)


def test_the_table_records_the_member_scoped_hold_as_a_backtest_only_fact():
    (row,) = [r for r in BACKTEST_FORCED_SETTINGS if r.key == "hold_assigned_stock_tags"]
    assert row.live_setting is None and row.why_no_live_analogue
    assert backtest_only_settings(_facts(hold_assigned_stock_tags=(7, 3)))[
        "hold_assigned_stock_tags"] == [3, 7]


def test_a_run_without_it_records_None_and_deploys():
    facts = _facts()
    assert backtest_only_settings(facts)["hold_assigned_stock_tags"] is None
    refuse_undeployable(facts)          # does not raise


def test_a_member_scoped_run_is_refused_for_deploy_by_name():
    with pytest.raises(UndeployableRunRefused, match="hold_assigned_stock_tags"):
        refuse_undeployable(_facts(hold_assigned_stock_tags=(7,)))


def test_the_deploy_report_skips_a_backtest_only_row_that_states_nothing():
    """Every deployable payload carries ``hold_assigned_stock_tags: None`` (a member-scoped run is
    refused above), so printing it on every deploy is noise. A None row says nothing about the
    run; every other row -- False included -- is still reported."""
    from ba2_common.core.deploy_parity import backtest_only_notices
    notices = backtest_only_notices(backtest_only_settings(_facts(hold_assigned_stock=True)))
    assert notices == ["BACKTEST-ONLY, not applied: hold_assigned_stock=True"]
    assert backtest_only_notices({"a": False, "b": None, "c": 0}) == [
        "BACKTEST-ONLY, not applied: a=False", "BACKTEST-ONLY, not applied: c=0"]
    assert backtest_only_notices({}) == []
