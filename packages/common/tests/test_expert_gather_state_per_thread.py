"""``self._gather_*`` is per THREAD: concurrent analyses on one cached expert instance never see each other's symbol.

THE DEFECT (found 2026-11-07 by the live replay test). ``run_analysis`` sets ``self._gather_symbol = symbol`` and
``_gather`` reads it back; live, WorkerQueue threads share ONE cached expert instance, so analysis B could overwrite
A's symbol in between and A stored B's recommendation (prod 2026-11-06: CORT got RSI's BUY at RSI's price)."""
import gc
import threading

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface, _GATHER_TLS


class _Expert(MarketExpertInterface):
    """Just enough of an expert: the race lives in set-then-gather, nothing else."""

    def _gather(self, providers, as_of):
        return {"symbol": self._gather_symbol}

    # abstract members of the interface, unused here
    def run_analysis(self, symbol, market_analysis):  # pragma: no cover
        raise NotImplementedError

    def render_market_analysis(self, market_analysis):  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def description(cls):  # pragma: no cover
        return ""

    @classmethod
    def get_settings_definitions(cls):  # pragma: no cover
        return {}


def _expert():
    return _Expert.__new__(_Expert)     # no DB: the instance attributes are not what is under test


def test_concurrent_set_then_gather_reads_its_own_symbol():
    e = _expert()
    both_set = threading.Barrier(2)
    seen = {}

    def analysis(symbol):
        e._gather_symbol = symbol
        both_set.wait(timeout=5)               # the other analysis has now set ITS symbol too
        seen[symbol] = e._gather(None, None)["symbol"]

    threads = [threading.Thread(target=analysis, args=(s,)) for s in ("CORT", "RSI")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert seen == {"CORT": "CORT", "RSI": "RSI"}


def test_getattr_default_hasattr_and_delattr_keep_their_meaning():
    e = _expert()
    assert getattr(e, "_gather_model_target_method", "forward") == "forward"
    assert not hasattr(e, "_gather_symbol")
    e._gather_symbol = "AAPL"
    assert hasattr(e, "_gather_symbol") and e._gather_symbol == "AAPL"
    del e._gather_symbol
    with pytest.raises(AttributeError):
        e._gather_symbol
    with pytest.raises(AttributeError):
        e.no_such_attribute


def test_a_value_set_on_another_thread_is_not_visible():
    e = _expert()
    t = threading.Thread(target=lambda: setattr(e, "_gather_symbol", "OTHER"))
    t.start()
    t.join()
    assert getattr(e, "_gather_symbol", None) is None


def test_methods_and_ordinary_attributes_are_untouched():
    e = _expert()
    e.id = 7
    assert e.__dict__["id"] == 7 and "_gather_symbol" not in e.__dict__
    e._gather_symbol = "MSFT"
    assert "_gather_symbol" not in e.__dict__      # never a shared instance attribute
    assert e._gather(None, None) == {"symbol": "MSFT"}


def test_two_instances_on_one_thread_are_separate_and_freed():
    a, b = _expert(), _expert()
    a._gather_symbol, b._gather_symbol = "A", "B"
    assert (a._gather_symbol, b._gather_symbol) == ("A", "B")
    n = len(_GATHER_TLS.by_expert)
    del a
    gc.collect()
    assert len(_GATHER_TLS.by_expert) == n - 1


def test_assigning_a_gather_METHOD_on_an_instance_stays_an_instance_override():
    """Test doubles replace methods like ``_gather_holdings`` on the instance: that must keep working."""
    class _WithMethod(_Expert):
        def _gather_holdings(self):
            return ["REAL"]

    e = _WithMethod.__new__(_WithMethod)
    e._gather_holdings = lambda: ["FAKE"]
    assert e._gather_holdings() == ["FAKE"] and "_gather_holdings" in e.__dict__
