"""The analysis-task seam: an option expert's task fills its symbol's series BEFORE the rule is
evaluated; every other expert pays nothing."""
import inspect
from datetime import date

import pytest

from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from ba2_trade_platform.modules.dataproviders.options import atm_iv_task_hook as K
from tests.atm_iv_fakes import make_provider, reset_module_state
from tests.factories import (create_event_action, create_expert_instance, create_ruleset,
                             link_rule_to_ruleset)

END = date(2026, 3, 6)
IV = {"trigger_0": {"event_type": "iv_rank", "operator": "<=", "value": 40.0}}
PLAIN = {"trigger_0": {"event_type": "confidence", "operator": ">=", "value": 80.0}}


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    K.invalidate_gate_cache()
    yield
    reset_module_state()
    K.invalidate_gate_cache()


def _expert(account_id, triggers, *, enabled=True, enter=True):
    rs = create_ruleset(name="rs")
    ea = create_event_action(name="r", triggers=triggers)
    link_rule_to_ruleset(rs.id, ea.id, order_index=0)
    kw = {"enter_market_ruleset_id": rs.id} if enter else {"open_positions_ruleset_id": rs.id}
    return create_expert_instance(account_id=account_id, expert="MockExpert", enabled=enabled, **kw)


class _Acct:
    def __init__(self, prov):
        self.p = prov

    def _atm_iv_history(self):
        return self.p


def test_gate_detection_is_per_use_case_enabled_and_iv_rank_only(mock_account_def):
    gated = _expert(mock_account_def.id, IV)
    plain = _expert(mock_account_def.id, PLAIN)
    off = _expert(mock_account_def.id, IV, enabled=False)
    pos = _expert(mock_account_def.id, IV, enter=False)
    assert K._account_id_if_iv_rank_gated(gated.id, "enter_market") == mock_account_def.id
    assert K._account_id_if_iv_rank_gated(gated.id, "open_positions") is None
    assert K._account_id_if_iv_rank_gated(pos.id, "open_positions") == mock_account_def.id
    assert K._account_id_if_iv_rank_gated(plain.id, "enter_market") is None
    assert K._account_id_if_iv_rank_gated(off.id, "enter_market") is None


def test_gate_answer_is_cached_and_invalidated(mock_account_def):
    inst = _expert(mock_account_def.id, PLAIN)
    assert K._account_id_if_iv_rank_gated(inst.id, "enter_market") is None
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import EventAction
    from sqlmodel import select
    with get_db() as session:
        ea = session.exec(select(EventAction).where(EventAction.name == "r")).first()
        ea.triggers = IV
        session.add(ea)
        session.commit()
    assert K._account_id_if_iv_rank_gated(inst.id, "enter_market") is None      # cached
    K.invalidate_gate_cache()
    assert K._account_id_if_iv_rank_gated(inst.id, "enter_market") == mock_account_def.id


def test_option_expert_task_fills_its_symbol_blocking_and_a_plain_expert_makes_zero_calls(tmp_path, mock_account_def):
    prov, world, bars, lister = make_provider(tmp_path)
    prov.last_completed_session = lambda: END
    opt = _expert(mock_account_def.id, IV)
    plain = _expert(mock_account_def.id, PLAIN)

    def resolver(aid):
        assert aid == mock_account_def.id
        return _Acct(prov)
    assert K.ensure_for_analysis_task(plain.id, "TEST", "enter_market", account_resolver=resolver) is None
    assert prov.api_calls == 0                                                   # nothing for a plain expert
    res = K.ensure_for_analysis_task(opt.id, "TEST", "enter_market", account_resolver=resolver)
    assert res is not None and res.status == H.STATUS_COMPLETE and prov.api_calls > 0
    n = prov.api_calls
    K.ensure_for_analysis_task(opt.id, "TEST", "enter_market", account_resolver=resolver)
    assert prov.api_calls == n                                                   # next pass: incremental, none missing


def test_hook_never_raises_and_skips_non_alpaca_accounts(mock_account_def):
    opt = _expert(mock_account_def.id, IV)

    def boom(aid):
        raise RuntimeError("db down")
    assert K.ensure_for_analysis_task(opt.id, "TEST", "enter_market", account_resolver=boom) is None
    assert K.ensure_for_analysis_task(opt.id, "TEST", "enter_market", account_resolver=lambda a: object()) is None


def test_worker_queue_runs_the_fill_before_the_expert_analysis():
    """The ordering the rule depends on: hook first, run_analysis second, in the same task."""
    from ba2_trade_platform.core.WorkerQueue import WorkerQueue
    src = inspect.getsource(WorkerQueue._execute_task)
    hook = src.index("ensure_for_analysis_task(task.expert_instance_id, task.symbol, task.subtype)")
    run = src.index("expert.run_analysis(task.symbol, market_analysis)")
    assert hook < run
