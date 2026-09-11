"""The Classic Risk Manager run dialog must show what the record now knows.

The record was extended (2026-09-11) with the rank each symbol was funded in, the capital
each order was measured against, and the CONSTRAINT that produced its size. A record nobody
can read is not observability, so these pin the screen, not the row:

* the columns exist and carry the recorded values;
* a run written before any of it existed still renders, as dashes, not as a crash;
* the capital line reads as a chain -- equity to what was left -- and omits what was
  never recorded rather than printing a zero.
"""
import pytest

from ba2_common.core.risk_manager_run import (MODE_CLASSIC, OUTCOME_FUNDED,
                                              OUTCOME_PERMISSION, OUTCOME_UNFUNDED,
                                              decision)
from ba2_trade_platform.ui.pages import marketanalysis as ma


def _funded(symbol, *, rank, score, quantity=10.0, **extra):
    return decision(symbol, OUTCOME_FUNDED, f"funded at {quantity:g}", quantity=quantity,
                    side="BUY", price=100.0, cost=quantity * 100.0, rank=rank, score=score,
                    **extra)


@pytest.fixture
def run_rows():
    """A run with all three shapes: funded, refused-after-ranking, refused-before."""
    return [
        _funded("BBB", rank=2, score=4.0, cap_available=1_000.0, balance_before=9_000.0,
                balance_after=8_000.0, binding="instrument_cap", weight=50.0,
                profit_pct=8.0, confidence=50.0),
        _funded("AAA", rank=1, score=12.0, cap_available=1_000.0, balance_before=10_000.0,
                balance_after=9_000.0, binding="balance", profit_pct=30.0, confidence=40.0),
        decision("CCC", OUTCOME_UNFUNDED, "sized to zero", side="BUY", price=100.0,
                 rank=3, score=1.0, binding="early_skip_balance",
                 balance_before=8_000.0, balance_after=8_000.0),
        decision("DDD", OUTCOME_PERMISSION, "SELL entries are disabled", side="SELL"),
    ]


def test_the_rows_read_in_funding_order(run_rows):
    """The manager funds down the ranking until the money runs out, so rank order IS the
    explanation. A symbol the permission filter dropped was never ranked and goes last."""
    rows = ma.classic_run_detail_rows(run_rows)

    assert [r['symbol'] for r in rows] == ['AAA', 'BBB', 'CCC', 'DDD']
    assert [r['rank'] for r in rows] == ['1', '2', '3', '-']


def test_a_row_carries_its_binding_constraint_and_its_capital(run_rows):
    rows = {r['symbol']: r for r in ma.classic_run_detail_rows(run_rows)}

    assert rows['BBB']['binding'] == 'instrument cap'
    assert rows['AAA']['binding'] == 'balance'
    assert rows['CCC']['binding'] == 'balance: < 1 share'
    assert rows['AAA']['balance'] == '10,000 → 9,000'
    assert rows['AAA']['cap_available'] == '1,000.00'
    assert rows['BBB']['weight'] == '50%'


def test_the_score_inputs_ride_with_the_score(run_rows):
    """The sort key, carried with the two numbers it is computed from, so it can be
    re-derived instead of trusted."""
    rows = {r['symbol']: r for r in ma.classic_run_detail_rows(run_rows)}

    assert rows['AAA']['score'] == '12.000'
    assert rows['AAA']['score_inputs'] == 'profit 30% · conf 40%'


def test_a_binding_the_screen_has_never_heard_of_is_shown_not_hidden():
    """A new constraint in the sizing core must appear as itself. A dash would be the same
    thing the screen shows for "nothing decided this", which is the opposite claim."""
    rows = ma.classic_run_detail_rows(
        [decision("AAA", OUTCOME_UNFUNDED, "sized to zero", binding="some_new_gate")])

    assert rows[0]['binding'] == 'some_new_gate'


def test_a_run_recorded_before_the_trace_renders_as_dashes():
    """Every row in production today. It must render -- and must not claim a rank, a
    binding or a balance it never had."""
    rows = ma.classic_run_detail_rows([
        decision("AAPL", OUTCOME_FUNDED, "funded at 12", quantity=12.0, side="BUY",
                 price=100.0, cost=1_200.0),
        decision("MSFT", OUTCOME_UNFUNDED, "budget exhausted", side="BUY"),
    ])

    assert [r['symbol'] for r in rows] == ['AAPL', 'MSFT']
    for row in rows:
        assert row['rank'] == '-' and row['binding'] == '-' and row['balance'] == '-'
        assert row['cap_available'] == '-' and row['score'] == '-'
    assert rows[0]['quantity'] == '12' and rows[0]['size'] == '1,200.00'
    assert rows[1]['quantity'] == '-', "a refused symbol was never sized"


# -----------------------------------------------------------------------------------------
# The capital line
# -----------------------------------------------------------------------------------------

def test_the_capital_line_reads_as_a_chain():
    lines = ma.classic_run_context_lines({
        'equity': 10_000.0, 'tradable_balance': 20_000.0, 'margin_factor': 2.0,
        'allocation_pct': 100.0, 'virtual_balance': 20_000.0, 'used_balance': 5_000.0,
        'available_balance': 15_000.0, 'max_per_instrument': 1_500.0,
        'max_per_instrument_ratio': 0.1, 'regime_risk_scale': 1.0,
        'sizing_mode': 'notional', 'diversification_factor': 1.0,
        'commission_per_trade': 0.0, 'enable_buy': True, 'enable_sell': False})

    assert lines[0] == ('Capital: equity $10,000.00 → tradable $20,000.00 (x2) → '
                        'allocation 100% → virtual $20,000.00 → used $5,000.00 → '
                        'available $15,000.00')
    assert lines[1] == 'Max per instrument: $1,500.00 (10% of available)'
    assert lines[2] == ('Regime scale 1 · Sizing notional · Diversification 1 · '
                        'Commission $0.00')
    assert lines[3] == 'enable buy: True · enable sell: False'


def test_a_capital_figure_that_was_never_read_is_simply_not_there():
    lines = ma.classic_run_context_lines({'available_balance': 5_000.0,
                                          'max_per_instrument': 2_000.0,
                                          'enable_buy': True})

    assert lines[0] == 'Capital: available $5,000.00'
    assert lines[1] == 'Max per instrument: $2,000.00'
    assert all('equity' not in line for line in lines)
    assert all('$0.00' not in line for line in lines), "an unread figure is never a zero"


def test_a_context_key_nobody_planned_for_is_still_shown():
    """A context key added later must be visible the day it is written, not the day
    someone remembers to add a line for it."""
    lines = ma.classic_run_context_lines({'some_new_knob': 7})

    assert lines == ['some new knob: 7']


def test_an_empty_context_renders_nothing_rather_than_an_empty_line():
    assert ma.classic_run_context_lines(None) == []
    assert ma.classic_run_context_lines({}) == []


# -----------------------------------------------------------------------------------------
# The dialog itself
# -----------------------------------------------------------------------------------------

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw. No browser."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-classic-rm-run-dialog'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _stored_run(run_rows):
    from ba2_common.core.db import get_db
    from ba2_common.core.models import AccountDefinition, ExpertInstance, RiskManagerRun
    with get_db() as session:
        account = AccountDefinition(name="rm-dialog-test", provider="StubProvider")
        session.add(account)
        session.commit()
        session.refresh(account)
        expert = ExpertInstance(account_id=account.id, expert="StubExpert")
        session.add(expert)
        session.commit()
        session.refresh(expert)
        run = RiskManagerRun(
            expert_instance_id=expert.id, account_id=account.id, mode=MODE_CLASSIC,
            symbols_received=len(run_rows), symbols_funded=2, decisions=run_rows,
            context={'equity': 10_000.0, 'available_balance': 10_000.0,
                     'max_per_instrument': 1_000.0, 'sizing_mode': 'notional'})
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def test_the_dialog_renders_the_new_columns_and_the_capital_line(nicegui_client, run_rows):
    """The real dialog, against a real stored run -- the pure builders above cannot catch a
    column that was added to the rows and never to the table."""
    import types

    run_id = _stored_run(run_rows)
    view = types.SimpleNamespace()
    view._format_run_date = ma.JobMonitoringTab._format_run_date
    view._format_duration = ma.JobMonitoringTab._format_duration

    with nicegui_client:
        ma.JobMonitoringTab._show_risk_manager_run_detail(view, run_id)

    tables = [e for e in nicegui_client.elements.values() if isinstance(e, ma.ui.table)]
    assert tables, "the dialog rendered no decision table"
    table = tables[-1]
    assert [c['name'] for c in table.columns][:4] == ['rank', 'symbol', 'outcome', 'score']
    assert {'cap_available', 'balance', 'binding'} <= {c['name'] for c in table.columns}
    assert [r['symbol'] for r in table.rows] == ['AAA', 'BBB', 'CCC', 'DDD']

    texts = [e._text for e in nicegui_client.elements.values() if e._text]
    assert any(t.startswith('Capital: equity $10,000.00') for t in texts), texts
    assert any('Sizing notional' in t for t in texts), texts
