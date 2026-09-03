"""The risk-manager runs table unions two source tables, and must not confuse them.

The screen used to show only ``smartriskmanagerjob``. It now also shows
``risk_manager_run`` (the classic and option managers), because "why did nothing trade
today?" is one question and the answer lives in whichever manager the expert happened to
be configured with.

Two things break quietly when you union two tables in a UI, and both are pinned here:

* the ROW KEY. Both tables' ids start at 1, so smart job 7 and classic run 7 collide on
  a plain ``id`` key and Quasar renders one of them.
* the DETAIL ROUTE. The emit has to say which table to open, or the classic run's id is
  looked up in the smart table and either 404s or -- worse -- opens the wrong run.
"""
import pytest
from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import (AccountDefinition, ExpertInstance, RiskManagerRun,
                                    SmartRiskManagerJob)
from ba2_common.core.risk_manager_run import (MODE_CLASSIC, MODE_OPTIONS, OUTCOME_FUNDED,
                                              OUTCOME_UNFUNDED, decision)


@pytest.fixture
def expert_id():
    with get_db() as session:
        account = AccountDefinition(name="rm-table-test", provider="StubProvider")
        session.add(account)
        session.commit()
        session.refresh(account)
        expert = ExpertInstance(account_id=account.id, expert="StubExpert", alias="Stubby")
        session.add(expert)
        session.commit()
        session.refresh(expert)
        return expert.id


def _classic_run(expert_id, *, mode=MODE_CLASSIC, funded=1, received=3):
    with get_db() as session:
        run = RiskManagerRun(
            expert_instance_id=expert_id, account_id=None, mode=mode,
            symbols_received=received, symbols_funded=funded,
            decisions=[
                decision("AAPL", OUTCOME_FUNDED, "funded at 12", quantity=12),
                decision("MSFT", OUTCOME_UNFUNDED, "budget exhausted"),
                decision("TSLA", OUTCOME_UNFUNDED, "one share exceeds the cap"),
            ],
            context={"available_balance": 5000.0})
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def _smart_job(expert_id):
    with get_db() as session:
        job = SmartRiskManagerJob(expert_instance_id=expert_id, account_id=1,
                                  model_used="stub", user_instructions="")
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


# ---------------------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------------------

def test_a_run_keeps_every_symbol_it_received(expert_id):
    """Including -- especially -- the ones it refused. That is the whole record."""
    run_id = _classic_run(expert_id)

    with get_db() as session:
        run = session.get(RiskManagerRun, run_id)
        assert [d["symbol"] for d in run.decisions] == ["AAPL", "MSFT", "TSLA"]
        assert all(d["reason"] for d in run.decisions)


def test_a_refused_symbol_stores_no_quantity(expert_id):
    """Not 0 — a refused symbol was never sized."""
    run_id = _classic_run(expert_id)

    with get_db() as session:
        run = session.get(RiskManagerRun, run_id)
        refused = [d for d in run.decisions if d["outcome"] != OUTCOME_FUNDED]
        assert refused
        assert all("quantity" not in d for d in refused)


def test_the_mode_is_a_column_so_the_filter_can_use_an_index(expert_id):
    """Filtering on a key inside the JSON would need a scan; the UI filters on this."""
    _classic_run(expert_id, mode=MODE_CLASSIC)
    _classic_run(expert_id, mode=MODE_OPTIONS)

    with get_db() as session:
        classic = session.exec(
            select(RiskManagerRun).where(RiskManagerRun.mode == MODE_CLASSIC)).all()
        options = session.exec(
            select(RiskManagerRun).where(RiskManagerRun.mode == MODE_OPTIONS)).all()

    assert len(classic) == 1
    assert len(options) == 1


# ---------------------------------------------------------------------------------------
# The union
# ---------------------------------------------------------------------------------------

def _rows(expert_id, type_filter='all'):
    """Drive the page's fetch with the filters set, without rendering the page.

    A stand-in object rather than a real ``JobMonitoringTab``: constructing one renders
    the whole tab. The fetch methods only ever read the three filter attributes
    and each other, so borrowing them onto a bare object exercises the real code without
    a UI. If a fetch method ever starts reading page state, this raises AttributeError
    rather than passing on a stale default -- which is the failure we would want.
    """
    import types
    from ba2_trade_platform.ui.pages import marketanalysis as ma

    class _Page:
        pass

    view = _Page()
    view.smart_risk_status_filter = 'all'
    view.smart_risk_expert_filter = 'all'
    view.smart_risk_type_filter = type_filter
    for name in ('_fetch_all_risk_manager_rows', '_fetch_all_risk_manager_runs',
                 '_fetch_all_smart_risk_jobs'):
        setattr(view, name, types.MethodType(getattr(ma.JobMonitoringTab, name), view))
    # staticmethods: already plain functions off the class, so no binding.
    view._format_run_date = ma.JobMonitoringTab._format_run_date
    view._format_duration = ma.JobMonitoringTab._format_duration
    return view._fetch_all_risk_manager_rows()


def test_smart_and_classic_runs_appear_in_one_list(expert_id):
    _smart_job(expert_id)
    _classic_run(expert_id)

    types = {r['run_type'] for r in _rows(expert_id)}

    assert types == {'smart', 'classic'}


def test_colliding_ids_do_not_collapse_into_one_row(expert_id):
    """Both tables start their ids at 1. On a plain `id` row key Quasar treats smart 1
    and classic 1 as the same row and renders one of them."""
    _smart_job(expert_id)
    _classic_run(expert_id)

    rows = _rows(expert_id)
    keys = [r['row_key'] for r in rows]

    assert len(keys) == len(set(keys)), f"row keys collide: {keys}"
    assert all(':' in k for k in keys)


def test_the_type_filter_narrows_to_one_manager(expert_id):
    _smart_job(expert_id)
    _classic_run(expert_id, mode=MODE_CLASSIC)
    _classic_run(expert_id, mode=MODE_OPTIONS)

    assert {r['run_type'] for r in _rows(expert_id, 'smart')} == {'smart'}
    assert {r['run_type'] for r in _rows(expert_id, 'classic')} == {'classic'}
    assert {r['run_type'] for r in _rows(expert_id, 'options')} == {'options'}


def test_the_union_is_ordered_newest_first(expert_id):
    """Across BOTH tables — a classic run from today must outrank a smart job from
    last week, which a naive 'smart rows then classic rows' concat gets wrong."""
    _smart_job(expert_id)
    _classic_run(expert_id)

    rows = _rows(expert_id)
    dates = [r['run_date'] for r in rows]
    normalised = [d.replace(tzinfo=None) if d and d.tzinfo else d for d in dates]

    assert normalised == sorted(normalised, reverse=True)


def test_a_sizing_manager_reports_funded_over_received_not_an_action_count(expert_id):
    _classic_run(expert_id, funded=1, received=3)

    row = next(r for r in _rows(expert_id) if r['run_type'] == 'classic')

    assert row['actions_taken_count'] == '1 / 3'


def test_a_sizing_manager_shows_no_iteration_count(expert_id):
    """These managers have no iteration loop. A 0 would invite comparison against a
    smart run's 7, as though they had iterated and stopped immediately."""
    _classic_run(expert_id)

    row = next(r for r in _rows(expert_id) if r['run_type'] == 'classic')

    assert row['iteration_count'] == '-'


# ---------------------------------------------------------------------------------------
# The producer meets the table (review 2026-08-30 dev-merge, FIX 2)
# ---------------------------------------------------------------------------------------
"""The Options filter had a reader and a model but NO producer.

``RiskManagerRun.mode`` and the UI's ``mode == 'options'`` filter shipped from dev; the
option risk manager shipped from the options branch writing a ``SmartRiskManagerJob`` with
``model_used="classic_options"`` — a column no filter looks at. The two halves never met:
the Options filter returned nothing, and every option run surfaced mislabelled under
**Smart**. These tests drive the REAL producer into the REAL fetch, so the handshake
cannot come apart again silently.
"""


def _option_run(expert_id, *, admitted=("AAPL",), refused=("MSFT",)):
    """Produce a run through the real writer, from the real journal.

    The journal is the producer's input, and the journal-entry -> decision-row mapping is
    pinned in ``packages/common/tests/test_option_risk_manager_wiring.py``. What is pinned
    HERE is the other end: that what ``flush_option_rm_run`` writes is what the table's
    Options filter reads.
    """
    import ba2_common.core.OptionRiskManagement as rm

    rm.reset_journal()
    for symbol in admitted:
        rm._journal_entry(expert_id, rm.OptionEntryVerdict(
            True, "", "ok", "within every sleeve rail"),
            symbol, "cash_secured_put", 1)
    for symbol in refused:
        rm._journal_entry(expert_id, rm.OptionEntryVerdict(
            False, "OPTION RAIL REFUSAL", "max_deployment",
            "cash_secured_put on %s: max_deployment — 42.0%% of a 40.0%% cap" % symbol),
            symbol, "cash_secured_put", 1)
    return rm.flush_option_rm_run(expert_id, None)


def test_an_option_run_appears_under_the_options_filter(expert_id):
    """The whole handshake: the option risk manager's own run, fetched by the filter that
    existed for it. Before FIX 2 this returned an empty list."""
    run_id = _option_run(expert_id)
    assert run_id is not None

    rows = _rows(expert_id, 'options')

    assert [r['id'] for r in rows] == [run_id]
    assert rows[0]['run_type'] == 'options'
    assert rows[0]['type_label'] == 'Options'
    # One admitted of two weighed — the sizing manager's count, not an action count.
    assert rows[0]['actions_taken_count'] == '1 / 2'


def test_the_smart_filter_no_longer_shows_the_option_run(expert_id):
    """It used to be the ONLY place it showed, labelled as a Smart job. An option run is
    not an LLM job and must not sit in that list."""
    _option_run(expert_id)

    assert _rows(expert_id, 'smart') == []
    # And it is not double-reported: the union shows it exactly once, as an options run.
    assert [r['run_type'] for r in _rows(expert_id)] == ['options']


def test_a_backtest_writes_no_run_row_at_all(expert_id, monkeypatch):
    """The pin holds through the rewrite: a grid evaluates tens of thousands of entries
    and has no runs table to read them."""
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)

    assert _option_run(expert_id) is None
    assert _rows(expert_id) == []

