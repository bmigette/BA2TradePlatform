"""GA-NEUTRALITY (owner 2026-11-06): the new tag on _PendingCharge and the record_submitted
bookkeeping never change an admit_option_entry verdict or any rail input."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

RAILS: Dict[str, Any] = {"max_concurrent_structures": 10, "max_deployment_pct": 40.0,
                         "max_notional_leverage": 3.0, "undefined_risk_max_pct": 20.0,
                         "circuit_breaker_pct": 20.0}
EID = 7
_REAL_ADMIT = rm.admit      # captured once: _run patches rm.admit and may run twice per test


class _Expert:
    settings = {"risk_manager_mode": "classic_options"}

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in RAILS:
            raise ValueError(key)
        return RAILS[key]


class _Account:
    id = 1

    def get_account_info(self):
        return {"equity": 100_000.0, "cash": 100_000.0, "balance": 100_000.0}

    get_account_snapshot = ReadOnlyAccountInterface.get_account_snapshot
    true_equity = ReadOnlyAccountInterface.true_equity

    def cash_available_for_delivery(self):
        return 100_000.0


def _legs(symbol: str) -> List[OptionLeg]:
    return [OptionLeg(contract_symbol=f"{symbol}260116P100", side=OrderDirection.SELL,
                      ratio_qty=1, option_type=OptionRight.PUT, strike=100.0,
                      expiry=date(2026, 1, 16), underlying=symbol)]


def _run(monkeypatch, tag: Optional[int]):
    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: dict.fromkeys({100, 101, 102}))
    seen, verdicts = [], []
    real_admit = _REAL_ADMIT

    def spy(cands, book, equity, rails, breaker, cash):
        seen.append((tuple(cands), book, equity, dict(rails), breaker, cash))
        return real_admit(cands, book, equity, rails, breaker, cash)

    monkeypatch.setattr(rm, "admit", spy)
    for i, (sym, max_loss) in enumerate([("AAPL", 4_000.0), ("MSFT", 9_000.0),
                                         ("NVDA", 30_000.0)]):   # the 3rd breaks the 40% cap
        v = rm.admit_option_entry(
            expert=_Expert(), account=_Account(), expert_instance_id=EID, underlying=sym,
            option_strategy="cash_secured_put", legs=_legs(sym), quantity=1,
            max_loss_per_contract=max_loss)
        verdicts.append((v.allowed, v.phrase, v.reason, v.detail))
        if v.allowed:
            kw = {} if tag is None else {"entry_tag": tag}
            rm.record_submitted(EID, 100 + i, v.candidate, **kw)
    return verdicts, seen


def test_a_tagged_submission_changes_no_verdict_and_no_rail_input(monkeypatch):
    plain = _run(monkeypatch, None)
    tagged = _run(monkeypatch, 14)
    assert plain[0] == tagged[0]
    assert plain[1] == tagged[1]
    assert [v[0] for v in plain[0]] == [True, True, False]       # the scenario exercises a refusal


def test_without_a_structure_share_condition_no_share_cache_is_ever_built(monkeypatch):
    _run(monkeypatch, 14)
    assert rm._SHARE_CACHE == {}


def test_untagged_submissions_touch_no_share_state(monkeypatch):
    rm.reset_state()
    monkeypatch.setattr(rm, "_SHARE_GENS", {})     # generations are never cleared; start empty
    _run(monkeypatch, None)
    assert rm._SHARE_CACHE == {} and rm._SHARE_GENS == {}
