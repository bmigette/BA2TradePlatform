"""AlpacaAccount.get_iv_rank records WHY it returned None (display only)."""
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _acct(state):
    a = object.__new__(AlpacaAccount)
    a.__dict__["id"] = 1
    a._iv_rank_state = lambda u, lb: state
    return a


def _state(**kw):
    s = {"ok": True, "series": [0.2] * 30, "current": 0.25, "n": 30, "m": 30, "status": "complete",
         "provenance": {}, "reason": None, "L": "2026-10-09", "resolved": 30, "need": 24}
    s.update(kw)
    return s


def test_filling_reason_recorded_and_cleared():
    AlpacaAccount._iv_rank_reasons.clear()
    a = _acct(_state(ok=False, status="filling", n=120, m=252))
    assert a.get_iv_rank("AAPL") is None
    assert a.iv_rank_unavailable_reason("AAPL").startswith("ATM-IV history filling 120/252")
    b = _acct(_state())
    assert b.get_iv_rank("AAPL") is not None
    assert b.iv_rank_unavailable_reason("AAPL") is None


def test_no_current_reason():
    AlpacaAccount._iv_rank_reasons.clear()
    a = _acct(_state(current=None))
    assert a.get_iv_rank("MSFT") is None
    assert a.iv_rank_unavailable_reason("MSFT") == \
        "no derived ATM IV for the last completed session 2026-10-09"
