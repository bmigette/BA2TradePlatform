"""Fractional-share eligibility in a BACKTEST: from disk, never a broker.

A backtest cannot ask a broker. It answers from the file ``ba2-test prewarm`` writes, and a
symbol the file does not list is unknown -- whole shares, exactly the pre-feature behaviour.
"""
import socket

import pytest

from ba2_common.core import fractionable_store as fs


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No network, and a private cache folder."""
    from ba2_common import config

    def _refuse(*a, **k):
        raise AssertionError("a backtest-side fractionability read reached the network")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path), raising=False)
    fs.clear_memo()
    yield
    fs.clear_memo()


def _account():
    from app.services.backtest.backtest_account import BacktestAccount
    return object.__new__(BacktestAccount)


def test_the_backtest_account_answers_from_the_file():
    fs.save_fractionable_map({"AAPL": True, "BRK.A": False}, source="test")

    assert _account().get_fractionable(["AAPL", "BRK.A"]) == {"AAPL": True, "BRK.A": False}


def test_a_symbol_the_file_does_not_list_is_unknown():
    fs.save_fractionable_map({"AAPL": True}, source="test")

    assert _account().get_fractionable(["ZZZZ"]) == {"ZZZZ": None}


def test_no_file_means_every_symbol_is_unknown_and_nothing_raises():
    """The cold state: sizes exactly as before fractional support existed."""
    assert _account().is_fractionable("AAPL") is None


# ---------------------------------------------------------------------------
# The prewarm that writes the file
# ---------------------------------------------------------------------------

def test_a_fresh_file_is_not_refetched(monkeypatch):
    import app.services.prewarm_fetchers as pf

    fs.save_fractionable_map({"AAPL": True}, source="test")
    monkeypatch.setattr(pf, "fetch_alpaca_fractionable",
                        lambda *a, **k: pytest.fail("refetched a fresh file"))

    assert pf.prewarm_fractionable(24.0)["fresh"] is True


def test_missing_keys_are_reported_not_fatal(monkeypatch):
    """Only an opted-in expert reads this; a 500-symbol FMP prewarm must not fail over it."""
    import app.services.prewarm_fetchers as pf

    monkeypatch.setattr(pf, "resolve_alpaca_keys", lambda: (None, None))
    warnings = []

    out = pf.prewarm_fractionable(24.0, warn=warnings.append)

    assert "error" in out
    assert warnings and "whole shares" in warnings[0]


def test_a_successful_fetch_writes_the_file(monkeypatch):
    import app.services.prewarm_fetchers as pf

    monkeypatch.setattr(pf, "resolve_alpaca_keys", lambda: ("K", "S"))
    monkeypatch.setattr(pf, "fetch_alpaca_fractionable",
                        lambda key, secret: {"AAPL": True, "BRK.A": False})

    out = pf.prewarm_fractionable(24.0, log=lambda m: None)

    assert out == {"written": 2, "fractionable": 1}
    assert fs.load_fractionable_map() == {"AAPL": True, "BRK.A": False}


def test_a_failed_fetch_leaves_the_existing_file_alone(monkeypatch):
    import app.services.prewarm_fetchers as pf

    fs.save_fractionable_map({"AAPL": True}, source="test")
    monkeypatch.setattr(pf, "resolve_alpaca_keys", lambda: ("K", "S"))

    def _boom(*a, **k):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(pf, "fetch_alpaca_fractionable", _boom)

    out = pf.prewarm_fractionable(0.0, warn=lambda m: None)

    assert "error" in out
    assert fs.load_fractionable_map() == {"AAPL": True}


def test_the_alpaca_parser_keeps_only_real_booleans(monkeypatch):
    import app.services.prewarm_fetchers as pf

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{"symbol": "aapl", "fractionable": True},
                    {"symbol": "BRK.A", "fractionable": False},
                    {"symbol": "ODD", "fractionable": "yes"},
                    {"symbol": "", "fractionable": True}]

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    assert pf.fetch_alpaca_fractionable("K", "S") == {"AAPL": True, "BRK.A": False}


def test_a_live_key_refused_on_the_live_host_falls_back_to_paper(monkeypatch):
    import app.services.prewarm_fetchers as pf
    import requests

    hosts = []

    class _Resp:
        def __init__(self, url):
            self.status_code = 401 if "paper" not in url else 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{"symbol": "AAPL", "fractionable": True}]

    def _get(url, **k):
        hosts.append(url)
        return _Resp(url)

    monkeypatch.setattr(requests, "get", _get)

    assert pf.fetch_alpaca_fractionable("K", "S") == {"AAPL": True}
    assert len(hosts) == 2 and "paper" in hosts[1]
