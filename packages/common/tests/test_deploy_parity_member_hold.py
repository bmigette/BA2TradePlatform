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



def test_only_the_rows_whose_None_means_nothing_are_marked_silent_when_none():
    silent = {r.key for r in BACKTEST_FORCED_SETTINGS if r.silent_when_none}
    assert silent == {"hold_assigned_stock_tags", "entry_action"}
    # the flag is a statement about a backtest-only record; a live-carried row never has it
    assert all(r.live_setting is None for r in BACKTEST_FORCED_SETTINGS if r.silent_when_none)


def test_the_deploy_report_skips_a_None_row_only_where_the_table_says_None_means_nothing():
    """Every deployable payload carries ``hold_assigned_stock_tags: None`` (a member-scoped run is
    refused above) and an equity run ``entry_action: None``; printing them on every deploy is
    noise. The decision is the ROW's, not a blanket "skip None": a None on any other row, or on
    a key the table does not know, is still reported."""
    from ba2_common.core.deploy_parity import reportable_backtest_only
    assert reportable_backtest_only(backtest_only_settings(_facts(hold_assigned_stock=True))) == {
        "hold_assigned_stock": True}
    assert reportable_backtest_only({"hold_assigned_stock": None, "unknown_row": None,
                                     "hold_assigned_stock_tags": None, "entry_action": None}) == {
        "hold_assigned_stock": None, "unknown_row": None}
    assert reportable_backtest_only({"hold_assigned_stock": False, "entry_action": "open_call"})         == {"hold_assigned_stock": False, "entry_action": "open_call"}
    assert reportable_backtest_only(None) == {} and reportable_backtest_only({}) == {}
