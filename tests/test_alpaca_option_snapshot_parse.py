"""BT/live option parity B3: the LIVE Alpaca chain and quote carry the data session's VOLUME.

Plan: ``docs/plans/2026-09-22-bt-live-option-parity.md`` step B3.

THE BUG. The options grid stamps ``option_min_volume=25`` onto every option strategy, and the
selector refuses a chain in which no contract publishes a volume
(``OptionLiquidityDataUnavailable``). ``AlpacaAccount.get_option_chain`` never set one, so a
grid option strategy deployed live never traded.

WHY THE RAW SNAPSHOT. alpaca-py's ``OptionsSnapshot`` model (0.43.4 installed, 0.44.0 too)
keeps only latest_quote/latest_trade/implied_volatility/greeks and DROPS ``dailyBar`` /
``prevDailyBar`` although the REST response carries them. So the account reads the
``raw_data=True`` client and parses the dict itself (``parse_alpaca_option_snapshot``).

THE SESSION RULE (``prior_session_v1``): a live decision at instant t reads
``decision_data_session(live_decision_label(t))`` -- the regular session BEFORE t's New York
date, even after today's close -- and the volume is ``option_session.session_volume`` of the
snapshot's daily bars for exactly that session, else 0 (the contract did not trade in it).

No network: the raw client is a fake, or the real SDK client with its HTTP ``get`` replaced.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone

import pytest

from ba2_common.core import option_session
from ba2_trade_platform.core.types import OptionRight
from ba2_trade_platform.modules.accounts.AlpacaAccount import (
    AlpacaAccount,
    parse_alpaca_option_snapshot,
)

#: The MODULE (``ba2_trade_platform.modules.accounts`` re-exports the class under the same
#: name, so ``import ...AlpacaAccount as m`` would bind the class).
alpaca_mod = sys.modules[AlpacaAccount.__module__]

SYM = "AAPL250718C00200000"
SYM2 = "AAPL250718C00210000"
EXP = date(2025, 7, 18)

#: Session S (a Tuesday), prior(S) and the session before that (a Friday).
S = date(2025, 6, 10)
PRIOR_S = date(2025, 6, 9)
PRIOR_PRIOR_S = date(2025, 6, 6)
#: June is EDT (UTC-4): 09:35 ET = 13:35Z, 16:30 ET = 20:30Z.
MORNING_OF_S = datetime(2025, 6, 10, 13, 35, tzinfo=timezone.utc)
AFTER_CLOSE_OF_S = datetime(2025, 6, 10, 20, 30, tzinfo=timezone.utc)


def _bar(day: date, volume, *, hour_utc: int = 4):
    """An Alpaca option daily bar. Alpaca stamps a daily bar at New York midnight, which is
    04:00Z in summer (EDT) and 05:00Z in winter (EST)."""
    return {"t": f"{day.isoformat()}T{hour_utc:02d}:00:00Z", "o": 5.0, "h": 5.5, "l": 4.8,
            "c": 5.2, "v": volume, "n": 12, "vw": 5.1}


def _raw(daily=None, prev=None, **overrides):
    """A raw option snapshot shaped like Alpaca's documented ``/v1beta1/options/snapshots``
    example: every documented key, including ``minuteBar`` (which the parser ignores)."""
    snap = {
        "latestQuote": {"t": "2025-06-10T13:34:59.967358976Z", "ax": "C", "ap": 5.4, "as": 12,
                        "bx": "N", "bp": 5.0, "bs": 10, "c": "A"},
        "latestTrade": {"t": "2025-06-10T13:30:01.839418112Z", "x": "N", "p": 5.2, "s": 3,
                        "c": "a"},
        "minuteBar": {"t": "2025-06-10T13:34:00Z", "o": 5.1, "h": 5.2, "l": 5.1, "c": 5.2,
                      "v": 4, "n": 2, "vw": 5.15},
        "greeks": {"delta": 0.55, "gamma": 0.02, "rho": 0.07, "theta": -0.04, "vega": 0.1},
        "impliedVolatility": 0.32,
    }
    if daily is not None:
        snap["dailyBar"] = daily
    if prev is not None:
        snap["prevDailyBar"] = prev
    snap.update(overrides)
    return snap


def _meta(symbol=SYM, strike=200.0):
    from types import SimpleNamespace
    return SimpleNamespace(symbol=symbol, underlying_symbol="AAPL", root_symbol="AAPL",
                           type=SimpleNamespace(value="call"), strike_price=strike,
                           expiration_date=EXP, open_interest="1200", size="100")


def _account(monkeypatch, raw_snapshots, *, at, metas=None):
    acct = AlpacaAccount.__new__(AlpacaAccount)        # bypass __init__/DB
    acct.id = 1
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    calls = []

    class FakeRawClient:
        def get_option_chain(self, req):
            calls.append(("chain", req))
            return raw_snapshots

        def get_option_snapshot(self, req):
            calls.append(("snapshot", req))
            return raw_snapshots

    acct._option_data_client_raw = FakeRawClient()
    metas = metas if metas is not None else [_meta(s) for s in raw_snapshots]
    monkeypatch.setattr(acct, "_get_option_contracts_meta",
                        lambda *a, **k: {m.symbol: m for m in metas}, raising=False)
    monkeypatch.setattr(option_session, "live_decision_time", lambda: at)
    acct._calls = calls
    return acct


def _chain(acct):
    return {c.symbol: c for c in acct.get_option_chain(
        "AAPL", date(2025, 6, 1), date(2025, 8, 1), OptionRight.CALL)}


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #
def test_parser_reads_every_documented_field():
    parsed = parse_alpaca_option_snapshot(_raw(daily=_bar(PRIOR_S, 40),
                                               prev=_bar(PRIOR_PRIOR_S, 90)))
    assert (parsed["bid"], parsed["ask"]) == (5.0, 5.4)
    assert (parsed["bid_size"], parsed["ask_size"]) == (10, 12)
    assert parsed["quote_time"] == datetime(2025, 6, 10, 13, 34, 59, 967358, tzinfo=timezone.utc)
    assert parsed["last"] == 5.2
    assert parsed["last_time"].tzinfo is not None
    assert parsed["iv"] == 0.32
    assert (parsed["delta"], parsed["gamma"], parsed["theta"], parsed["vega"], parsed["rho"]) \
        == (0.55, 0.02, -0.04, 0.1, 0.07)
    assert sorted(parsed["bars"]) == [(PRIOR_PRIOR_S, 90), (PRIOR_S, 40)]


@pytest.mark.parametrize("hour_utc", [4, 5])
def test_parser_dates_a_daily_bar_by_its_new_york_calendar_date(hour_utc):
    """Alpaca stamps daily bars at New York midnight: 04:00Z under EDT, 05:00Z under EST.
    Both June stamps below are the NY date 2025-06-09 (00:00 and 01:00 EDT)."""
    parsed = parse_alpaca_option_snapshot(_raw(daily=_bar(date(2025, 6, 9), 7, hour_utc=hour_utc)))
    assert parsed["bars"] == [(date(2025, 6, 9), 7)]


def test_parser_dates_a_winter_bar_stamped_at_est_midnight():
    parsed = parse_alpaca_option_snapshot(_raw(daily={**_bar(date(2025, 1, 10), 3),
                                                      "t": "2025-01-10T05:00:00Z"}))
    assert parsed["bars"] == [(date(2025, 1, 10), 3)]


def test_parser_absent_sections_are_none_not_zero():
    """A missing field stays MISSING: the liquidity checks report it, nothing invents a 0."""
    parsed = parse_alpaca_option_snapshot({})
    for key in ("bid", "ask", "bid_size", "ask_size", "quote_time", "last", "last_time", "iv",
                "delta", "gamma", "theta", "vega", "rho"):
        assert parsed[key] is None, key
    assert parsed["bars"] == []


def test_parser_ignores_unknown_extra_keys():
    parsed = parse_alpaca_option_snapshot(_raw(daily={**_bar(PRIOR_S, 5), "zz": 1}, extra={"a": 1}))
    assert parsed["bars"] == [(PRIOR_S, 5)]


@pytest.mark.parametrize("missing", ["v", "t"])
def test_parser_refuses_a_present_bar_missing_volume_or_time(missing):
    bar = _bar(PRIOR_S, 40)
    del bar[missing]
    with pytest.raises(ValueError, match=missing):
        parse_alpaca_option_snapshot(_raw(daily=bar))


def test_parser_refuses_a_bar_with_a_null_volume():
    with pytest.raises(ValueError):
        parse_alpaca_option_snapshot(_raw(prev=_bar(PRIOR_S, None)))


def test_parser_refuses_a_timestamp_without_a_zone():
    with pytest.raises(ValueError):
        parse_alpaca_option_snapshot(_raw(daily={**_bar(PRIOR_S, 5), "t": "2025-06-09T04:00:00"}))


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def test_a_documented_snapshot_becomes_a_contract_with_session_volume_and_rho(monkeypatch):
    """(a) The documented shape, end to end: volume from the data session's bar, rho kept."""
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(PRIOR_S, 40),
                                            prev=_bar(PRIOR_PRIOR_S, 90))}, at=MORNING_OF_S)
    c = _chain(acct)[SYM]
    assert c.volume == 40 and isinstance(c.volume, int)
    assert c.rho == 0.07
    assert (c.bid, c.ask, c.last) == (5.0, 5.4, 5.2)
    assert (c.delta, c.gamma, c.theta, c.vega, c.implied_volatility) == (0.55, 0.02, -0.04, 0.1, 0.32)
    assert c.open_interest == 1200 and c.strike == 200.0 and c.expiry == EXP
    assert c.option_type == OptionRight.CALL
    assert acct._calls[0][0] == "chain"


def test_morning_decision_reads_the_daily_bar_dated_the_prior_session(monkeypatch):
    """(b) 09:35 ET on S before the contract has traded today: dailyBar is prior(S)'s bar,
    which IS the data session."""
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(PRIOR_S, 40),
                                            prev=_bar(PRIOR_PRIOR_S, 90))}, at=MORNING_OF_S)
    assert _chain(acct)[SYM].volume == 40


def test_morning_decision_ignores_todays_partial_daily_bar(monkeypatch):
    """Same decision once the contract has traded this morning: dailyBar is now S's PARTIAL
    bar (lookahead for a decision that reads prior(S)), prevDailyBar is prior(S)."""
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(S, 3), prev=_bar(PRIOR_S, 40))},
                    at=MORNING_OF_S)
    assert _chain(acct)[SYM].volume == 40


def test_prior_session_v1_after_the_close_still_reads_the_prior_session(monkeypatch):
    """(c) 16:30 ET on S: dailyBar is S's COMPLETE bar, but ``prior_session_v1`` says a
    decision labelled S reads prior(S) -- so prevDailyBar's volume, not dailyBar's."""
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(S, 500), prev=_bar(PRIOR_S, 40))},
                    at=AFTER_CLOSE_OF_S)
    assert _chain(acct)[SYM].volume == 40


def test_a_contract_with_no_bar_in_the_data_session_traded_zero(monkeypatch):
    """(d) Both bars older than the data session: it did not trade then. 0, never the older
    bar's volume (yesterday's volume is not today's liquidity)."""
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(PRIOR_PRIOR_S, 90),
                                            prev=_bar(date(2025, 6, 5), 70)),
                                  SYM2: _raw()}, at=MORNING_OF_S,
                    metas=[_meta(SYM), _meta(SYM2, 210.0)])
    chain = _chain(acct)
    assert chain[SYM].volume == 0
    assert chain[SYM2].volume == 0


def _capture_errors(monkeypatch):
    """ba2 loggers do not propagate to caplog: tee the module logger's ``error``."""
    real = alpaca_mod.logger
    errors = []

    class _Tee:
        def __getattr__(self, name):
            return getattr(real, name)

        def error(self, msg, *a, **k):
            errors.append(str(msg))

    monkeypatch.setattr(alpaca_mod, "logger", _Tee())
    return errors


def test_a_present_bar_missing_its_volume_excludes_that_contract_loudly(monkeypatch):
    """(e) A parse bug is loud but LOCAL: Alpaca's option_bar requires ``v``, so the contract
    carrying the defect is excluded with an ERROR naming it, and the rest of the underlying's
    chain is still returned (one bad row must not stop the whole underlying trading)."""
    bar = _bar(PRIOR_S, 40)
    del bar["v"]
    errors = _capture_errors(monkeypatch)
    acct = _account(monkeypatch, {SYM: _raw(daily=bar), SYM2: _raw(daily=_bar(PRIOR_S, 41))},
                    at=MORNING_OF_S, metas=[_meta(SYM), _meta(SYM2, 210.0)])
    chain = _chain(acct)
    assert list(chain) == [SYM2] and chain[SYM2].volume == 41
    assert any(SYM in m and "'v'" in m for m in errors), errors
    assert any("excluded 1 of 2" in m for m in errors), errors


def test_a_non_count_volume_on_the_session_bar_excludes_that_contract(monkeypatch):
    errors = _capture_errors(monkeypatch)
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(PRIOR_S, 2.5)),
                                  SYM2: _raw(daily=_bar(PRIOR_S, 41))},
                    at=MORNING_OF_S, metas=[_meta(SYM), _meta(SYM2, 210.0)])
    assert list(_chain(acct)) == [SYM2]
    assert any(SYM in m for m in errors), errors


@pytest.mark.parametrize("bad_snapshot", [_raw(daily={}), None], ids=["empty_daily_bar", "none"])
def test_an_empty_bar_or_a_null_snapshot_excludes_only_that_contract(monkeypatch, bad_snapshot):
    """An empty ``dailyBar`` object is a present bar with no ``t``/``v``; a ``None`` snapshot
    is not an object at all. Either drops THAT contract loudly; the rest of the chain stays."""
    errors = _capture_errors(monkeypatch)
    acct = _account(monkeypatch, {SYM: bad_snapshot, SYM2: _raw(daily=_bar(PRIOR_S, 41))},
                    at=MORNING_OF_S, metas=[_meta(SYM), _meta(SYM2, 210.0)])
    assert list(_chain(acct)) == [SYM2]
    assert any(SYM in m and "malformed" in m for m in errors), errors


def test_parser_refuses_a_snapshot_that_is_not_an_object():
    with pytest.raises(ValueError, match="not an object"):
        parse_alpaca_option_snapshot(None)


def _bad_bar():
    bar = _bar(PRIOR_S, 40)
    del bar["v"]
    return bar


def test_a_malformed_daily_bar_degrades_only_the_quotes_volume(monkeypatch):
    """Closes price off this quote: a bad daily bar must not take the bid/ask with it. The
    volume is MISSING (None, never 0) and the defect is logged naming the contract."""
    errors = _capture_errors(monkeypatch)
    acct = _account(monkeypatch, {SYM: _raw(daily=_bad_bar())}, at=MORNING_OF_S)
    q = acct.get_option_quote(SYM)
    assert (q.bid, q.ask, q.delta, q.rho) == (5.0, 5.4, 0.55, 0.07)
    assert q.volume is None
    assert any(SYM in m and "'v'" in m for m in errors), errors


def test_an_unanswerable_calendar_degrades_only_the_quotes_volume(monkeypatch):
    from ba2_common.core.market_calendar import MarketCalendarUnavailable

    errors = _capture_errors(monkeypatch)
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(PRIOR_S, 40))}, at=MORNING_OF_S)

    def no_calendar():
        raise MarketCalendarUnavailable("no calendar")

    monkeypatch.setattr(acct, "_option_data_session", no_calendar)
    q = acct.get_option_quote(SYM)
    assert (q.bid, q.ask) == (5.0, 5.4) and q.volume is None
    assert any(SYM in m and "MarketCalendarUnavailable" in m for m in errors), errors


def test_a_malformed_QUOTE_is_still_refused(monkeypatch):
    """The price itself is what a close would be sent at: a zoneless quote time raises."""
    snap = _raw(daily=_bar(PRIOR_S, 40))
    snap["latestQuote"]["t"] = "2025-06-10T13:34:59"
    acct = _account(monkeypatch, {SYM: snap}, at=MORNING_OF_S)
    with pytest.raises(ValueError):
        acct.get_option_quote(SYM)


def test_a_live_close_prices_through_a_malformed_daily_bar_in_enforce_mode(monkeypatch):
    """THE SAFETY CASE: ``CloseOptionAction._close_limit_price`` catches quote failures only
    via ``absorb_if_benign(e, InstanceNotFound)``, so under ``BA2_ERROR_MODE=enforce`` a
    ValueError out of the quote would FAIL the close. It must price at the bid instead."""
    from types import SimpleNamespace

    from ba2_common.core.TradeActions import CloseOptionAction
    from ba2_trade_platform.core.types import OrderDirection

    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    _capture_errors(monkeypatch)
    acct = _account(monkeypatch, {SYM: _raw(daily=_bad_bar())}, at=MORNING_OF_S)
    action = CloseOptionAction.__new__(CloseOptionAction)
    action.account = acct
    action.instrument_name = "AAPL"
    position = SimpleNamespace(contract_symbol=SYM, side=OrderDirection.BUY)
    order = SimpleNamespace(open_price=4.0, limit_price=None)
    monkeypatch.setattr(action, "_close_cross_fraction", lambda o: 0.0, raising=False)
    monkeypatch.setattr(action, "_closing_leg", lambda p: None, raising=False)
    assert action._close_limit_price(position, order) == 5.0     # the live BID, not 4.0


def test_the_data_session_is_computed_once_per_chain(monkeypatch):
    reads = []
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(PRIOR_S, 40)),
                                  SYM2: _raw(daily=_bar(PRIOR_S, 41))}, at=MORNING_OF_S,
                    metas=[_meta(SYM), _meta(SYM2, 210.0)])
    monkeypatch.setattr(option_session, "live_decision_time",
                        lambda: reads.append(1) or MORNING_OF_S)
    assert {s: c.volume for s, c in _chain(acct).items()} == {SYM: 40, SYM2: 41}
    assert len(reads) == 1


def test_two_page_chain_is_paginated_by_the_real_sdk(monkeypatch):
    """(f) The REAL raw ``OptionHistoricalDataClient`` with its HTTP ``get`` replaced: the SDK
    follows ``next_page_token`` and merges both pages' ``snapshots`` into one symbol-keyed dict."""
    from alpaca.data.historical.option import OptionHistoricalDataClient

    client = OptionHistoricalDataClient(api_key="k", secret_key="s", raw_data=True)
    pages = [
        {"snapshots": {SYM: _raw(daily=_bar(PRIOR_S, 40))}, "next_page_token": "tok-2"},
        {"snapshots": {SYM2: _raw(daily=_bar(PRIOR_S, 55))}, "next_page_token": None},
    ]
    seen_tokens = []

    def fake_get(path, data=None, **kwargs):
        seen_tokens.append(data.get("page_token"))
        assert path == "/options/snapshots/AAPL"
        return pages[len(seen_tokens) - 1]

    monkeypatch.setattr(client, "get", fake_get)
    acct = _account(monkeypatch, {}, at=MORNING_OF_S, metas=[_meta(SYM), _meta(SYM2, 210.0)])
    acct._option_data_client_raw = client
    chain = _chain(acct)
    assert seen_tokens == [None, "tok-2"]
    assert {s: c.volume for s, c in chain.items()} == {SYM: 40, SYM2: 55}


def test_the_raw_client_is_a_raw_data_sdk_client_and_is_cached():
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = 1
    acct._settings_cache = {"api_key": "k", "api_secret": "s"}
    client = acct._get_option_data_client_raw()
    assert client._use_raw_data is True
    assert acct._get_option_data_client_raw() is client


# --------------------------------------------------------------------------- #
# The quote
# --------------------------------------------------------------------------- #
def test_quote_carries_session_volume_rho_and_an_aware_timestamp(monkeypatch):
    acct = _account(monkeypatch, {SYM: _raw(daily=_bar(S, 3), prev=_bar(PRIOR_S, 40))},
                    at=MORNING_OF_S)
    q = acct.get_option_quote(SYM)
    assert q.volume == 40 and q.rho == 0.07
    assert (q.bid, q.ask, q.last, q.delta, q.implied_volatility) == (5.0, 5.4, 5.2, 0.55, 0.32)
    assert q.timestamp == datetime(2025, 6, 10, 13, 34, 59, 967358, tzinfo=timezone.utc)
    assert acct._calls[0][0] == "snapshot"


def test_quote_for_an_unknown_symbol_is_none(monkeypatch):
    acct = _account(monkeypatch, {}, at=MORNING_OF_S)
    assert acct.get_option_quote(SYM) is None


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #
def test_decision_time_is_the_market_condition_decisions_when_one_is_open(monkeypatch):
    """Inside an enter-market decision pass the chain reads the SAME instant the market gates
    froze, rather than taking (and, under capture, recording) a second clock read."""
    from ba2_common.core import market_condition_live as mcl

    state = mcl.DecisionState(resolver=None, decision_time=AFTER_CLOSE_OF_S, reader=None)
    token = mcl._DECISION.set(state)
    try:
        assert option_session.live_decision_time() == AFTER_CLOSE_OF_S
    finally:
        mcl._DECISION.reset(token)


def test_decision_time_reads_the_replay_clock_outside_a_decision(monkeypatch):
    from ba2_common.core.replay import clock

    monkeypatch.setattr(clock, "replay_now", lambda as_of=None: MORNING_OF_S)
    assert option_session.live_decision_time() == MORNING_OF_S
