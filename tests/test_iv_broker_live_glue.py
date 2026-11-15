"""Live glue of the iv-v1 BROKER quote source (no real broker call: fakes + recorded payloads).

* TastyTradeAccount.get_option_chain_quote_snapshot maps the REST payloads through its own session and
  wraps broker errors; AlpacaAccount asks for OPRA only and refuses (never the indicative feed);
  IBKR does not provide quotes.
* the ONE scheduler entry is parameterised by the source (AV prefetch cron / broker capture every 30 min),
  nothing is scheduled or logged without a gate, an unset source with a gate logs one ERROR.
* the broker capture window: after 20:15 ET on the session's date until 10 min before the next open.
* SCREENER COVERAGE: the capture takes the union of the bounded universes; a screener pick that was
  captured evaluates, a pick that was not refuses explicitly (counted in the pass summary).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ba2_common.core.option_chain_quotes import (
    FETCH_STRIKE_BAND, OptionChainQuotesFetchError, OptionChainQuotesUnsupported, fetch_expiry_window,
    validate_prior_session_snapshot,
)

FIX = Path(__file__).resolve().parents[1] / "packages" / "providers" / "tests" / "fixtures" / "broker_iv"
S = date(2026, 11, 13)
NY = ZoneInfo("America/New_York")


def _fx(sym):
    return json.loads((FIX / f"tt_{sym}.json").read_text())


def _data(fx, path, params):
    if path.startswith("/option-chains/"):
        return fx["chain"]
    if params and "equity" in params:
        return {"items": [fx["equity"]]}
    want = set(params["equity-option"])
    return {"items": [q for q in fx["quotes"] if q["symbol"] in want]}


# --------------------------------------------------------------------------- account implementations
class _FakeTTSession:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    async def _get(self, path, params=None):
        self.calls.append(path)
        if self.fail is not None:
            raise self.fail
        sym = path.split("/")[2] if path.startswith("/option-chains/") else None
        sym = sym or (params.get("equity") or [None])[0] or params["equity-option"][0].split()[0]
        return _data(_fx(sym), path, params)


def _tt_account(session):
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    acct = object.__new__(TastyTradeAccount)
    acct.id, acct._session, acct._account, acct._authentication_error = 9, session, object(), None
    acct._run_async = lambda coro, timeout=None: asyncio.run(coro)
    acct._sleep = lambda s: None
    return acct


def test_tastytrade_serves_a_valid_prior_session_snapshot_through_its_session():
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    assert TastyTradeAccount.supports_option_chain_quote_snapshot is True
    assert TastyTradeAccount.option_chain_quote_source_kind == "tastytrade"
    sess = _FakeTTSession()
    lo, hi = fetch_expiry_window(S)
    snap = _tt_account(sess).get_option_chain_quote_snapshot("EMN", expiry_min=lo, expiry_max=hi,
                                                            strike_band=FETCH_STRIKE_BAND)
    assert snap.account_id == 9 and snap.source_kind == "tastytrade" and len(snap.quotes) == 24
    assert validate_prior_session_snapshot(snap, S).ok
    assert all(p.startswith(("/market-data/by-type", "/option-chains/")) for p in sess.calls)


def test_tastytrade_broker_errors_become_fetch_errors():
    from tastytrade.utils import TastytradeError

    acct = _tt_account(_FakeTTSession(fail=TastytradeError("HTTP 503")))
    with pytest.raises(OptionChainQuotesFetchError, match="503"):
        acct.get_option_chain_quote_snapshot("EMN", expiry_min=S, expiry_max=S, strike_band=0.2)


def test_alpaca_asks_for_opra_only_and_refuses_when_not_entitled():
    from alpaca.data.enums import OptionsFeed

    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    seen = []

    class _E(Exception):
        status_code = 403

    class _Client:
        def get_option_chain(self, req):
            seen.append(req)
            raise _E('{"message":"OPRA agreement is not signed"}')

    acct = object.__new__(AlpacaAccount)
    acct.id = 3
    acct._option_data_client_raw = _Client()
    acct.get_instrument_current_price = lambda sym: 500.0
    with pytest.raises(OptionChainQuotesUnsupported, match="OPRA feed"):
        acct.get_option_chain_quote_snapshot("MSFT", expiry_min=S, expiry_max=S, strike_band=0.2)
    assert seen and seen[0].feed == OptionsFeed.OPRA             # never the indicative feed


def test_ibkr_does_not_provide_option_chain_quotes():
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount

    assert IBKRAccount.supports_option_chain_quote_snapshot is False


# --------------------------------------------------------------------------- the scheduler entry
def _logs(monkeypatch, module):
    logged = []
    for level in ("info", "warning", "error"):
        monkeypatch.setattr(module.logger, level, lambda msg, *a, _l=level, **k: logged.append((_l, msg)))
    return logged


def test_the_schedule_follows_the_source(monkeypatch):
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    logged = _logs(monkeypatch, ivp)
    monkeypatch.setattr(ivp, "has_active_iv_gates", lambda: False)
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "account:4")
    assert ivp.schedule_spec() is None and logged == []          # no gate: nothing, silently
    monkeypatch.setattr(ivp, "has_active_iv_gates", lambda: True)
    monkeypatch.setattr(ivp, "configured_source_value", lambda: None)
    assert ivp.schedule_spec() is None
    assert len(logged) == 1 and logged[0][0] == "error" and "iv_live_chain_source" in logged[0][1]
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "alphavantage")
    assert ivp.schedule_spec()["cron"] == {"hour": 6, "minute": 30, "day_of_week": "mon-fri"}
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "account:4")
    monkeypatch.setattr(ivp, "capture_universe", lambda instances=None: (["MSFT"], {}))
    spec = ivp.schedule_spec()
    assert spec["cron"] == {"minute": "15,45"} and spec["source"] == "account:4"
    logged.clear()
    monkeypatch.setattr(ivp, "capture_universe", lambda instances=None: (["MSFT"], {7: "set screener_symbol_universe"}))
    ivp.schedule_spec()
    assert [l for l, _m in logged] == ["error"] and "unbounded universe" in logged[0][1]


@pytest.mark.parametrize("local, want", [
    (datetime(2026, 11, 13, 20, 0), None),                        # before the first attempt
    (datetime(2026, 11, 13, 20, 20), S),
    (datetime(2026, 11, 14, 12, 0), S),                           # Saturday
    (datetime(2026, 11, 16, 9, 19), S),                           # Monday, before 09:20
    (datetime(2026, 11, 16, 9, 21), None),                        # too close to the open
    (datetime(2026, 11, 16, 12, 0), None),                        # Monday session running
    (datetime(2026, 11, 16, 21, 0), date(2026, 11, 16)),
])
def test_the_broker_capture_window(local, want):
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    got = ivp.capture_target(local.replace(tzinfo=NY))
    assert (got[0] if got else None) == want


# --------------------------------------------------------------------------- screener coverage
class _Expert:
    def __init__(self, instruments, universe=""):
        self._i, self.settings = instruments, {"screener_symbol_universe": universe}

    def get_enabled_instruments(self):
        return self._i


def _bars(root: Path, sym: str):
    bars = _fx(sym)["bars"]
    df = pd.DataFrame({"Date": pd.to_datetime([b["date"] for b in bars]),
                       **{k.capitalize(): [float(b[k]) for b in bars] for k in ("open", "high", "low", "close", "volume")}})
    df["effective_date"] = df["Date"]
    p = root / "FMPOHLCVProvider" / f"{sym}_1d.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)


def test_a_screener_pick_is_served_only_when_it_was_in_the_captured_universe(tmp_path, monkeypatch):
    from ba2_common.core.market_conditions import STATUS_NO_CONTEXT, STATUS_VALID
    from ba2_providers.market_conditions.broker_live_chain import BrokerLiveChainReader
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    root = tmp_path / "cache"
    for sym in ("MSFT", "EMN", "NVDA"):
        _bars(root, sym)
    monkeypatch.setattr(ivp, "quote_source_account", lambda aid: (object, "tastytrade", ""))
    acct = _tt_account(_FakeTTSession())
    instances = [(5, _Expert(["SCREENER"], "MSFT, EMN")), (6, _Expert(["DYNAMIC"]))]
    now = datetime(2026, 11, 13, 20, 30, tzinfo=NY)
    rep = ivp.run_capture_once(4, now, account=acct, cache_root=str(root), instances=instances)
    assert rep["captured"] == 2 and list(rep["unbounded_instances"]) == [6]
    reader = BrokerLiveChainReader("iv-v1", account_id=4, serving_kind="broker:tastytrade", cache_root=str(root))
    token = object()
    reader._current_pass = lambda: token
    picked_captured = reader.observe("EMN", S)
    assert all(o.status == STATUS_VALID for o in picked_captured.values.values())
    picked_outside = reader.observe("NVDA", S)
    assert all(o.status == STATUS_NO_CONTEXT and "not in the captured universe" in o.reason
               for o in picked_outside.values.values())
    assert list(reader._pass_state(token).reasons) == ["not_in_captured_universe"]


def test_outside_the_window_nothing_is_fetched(tmp_path, monkeypatch):
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    class _Boom:
        def get_option_chain_quote_snapshot(self, *a, **k):
            raise AssertionError("no fetch outside the window")
    rep = ivp.run_capture_once(4, datetime(2026, 11, 16, 12, 0, tzinfo=NY), account=_Boom(),
                               cache_root=str(tmp_path), instances=[(5, _Expert(["MSFT"]))])
    assert rep == {"skipped": "outside the broker capture window"}


def test_an_uncertified_broker_kind_is_never_captured(tmp_path, monkeypatch):
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    monkeypatch.setattr(ivp, "quote_source_account", lambda aid: (object, "alpaca_opra", ""))
    _logs(monkeypatch, ivp)
    rep = ivp.run_capture_once(4, datetime(2026, 11, 13, 20, 30, tzinfo=NY), account=object(),
                               cache_root=str(tmp_path), instances=[(5, _Expert(["MSFT"]))])
    assert "not certified" in rep["skipped"]


def test_the_broker_factory_refuses_an_account_that_cannot_serve_quotes(monkeypatch):
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    monkeypatch.setattr(ivp, "quote_source_account",
                        lambda aid: (object, "", "quote-source account 2 (IBKR) does not provide option chain quotes"))
    r = ivp.make_broker_chain_reader("iv-v1", account_id=2)
    assert isinstance(r, LiveUnavailableMarketConditionReader) and "IBKR" in r.reason
    monkeypatch.setattr(ivp, "quote_source_account", lambda aid: (object, "tastytrade", ""))
    r = ivp.make_broker_chain_reader("iv-v1", account_id=2, cache_root="x")
    assert r.serving_kind == "broker:tastytrade" and r.account_id == 2


def test_the_certification_tool_compares_like_the_live_path():
    """tools/certify_broker_iv_chain.compare on the recorded chains: identical quotes, rows, no flips."""
    import sys

    from ba2_providers.market_conditions.av_live_chain import chain_quotes
    from ba2_providers.market_conditions.broker_live_chain import map_snapshot
    from ba2_providers.options.alphavantage import map_chain
    from ba2_providers.options.tastytrade_quotes import fetch_chain_quote_snapshot

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import certify_broker_iv_chain as C

    fx = _fx("EMN")
    lo, hi = fetch_expiry_window(S)
    snap = fetch_chain_quote_snapshot(lambda p, q: _data(fx, p, q), "EMN", expiry_min=lo, expiry_max=hi)
    verdict = validate_prior_session_snapshot(snap, S)
    broker = chain_quotes(map_snapshot("EMN", S, verdict.kept)[0], "EMN", S)
    av_frame, _ = map_chain("EMN", S, pd.read_parquet(FIX / f"av_EMN_{S}.parquet").to_dict("records"))
    av = chain_quotes(av_frame[["option_type", "strike", "expiry", "bar_date", "bid", "ask"]], "EMN", S)
    bars = fx["bars"][-128:]
    import numpy as np
    win = tuple(np.array([float(b[k]) for b in bars]) for k in ("open", "high", "low", "close", "volume"))
    rec = C.compare("EMN", S, broker, av, win, spot=snap.spot)
    assert rec["identical_quotes_share"] == 1.0 and rec["identical_rows"] is True
    assert all(v["flipped"] == 0 for v in rec["gate_flips"].values()) and rec["gate_flips"]


def test_the_settings_dropdown_lists_only_quote_source_accounts():
    from ba2_trade_platform.ui.pages import settings as settings_page

    from tests.factories import create_account_definition

    tt = create_account_definition(name="tt-quotes", provider="TastyTrade")
    ib = create_account_definition(name="ib", provider="IBKR")
    alp = create_account_definition(name="alp", provider="Alpaca")
    tab = object.__new__(settings_page.AppSettingsTab)
    tab._stored = {}
    opts = tab._iv_chain_source_options()
    assert opts[""].startswith("Not set") and "alphavantage" in opts
    assert f"account:{tt.id}" in opts and opts[f"account:{tt.id}"].endswith("quote source only")
    assert "NOT certified" in opts[f"account:{alp.id}"]                  # OPRA only, pairing not certified
    assert f"account:{ib.id}" not in opts
    tab._stored = {"iv_live_chain_source": "account:99999"}      # a deleted account stays visible, flagged
    assert "not a quote-source account" in tab._iv_chain_source_options()["account:99999"]


def test_the_operator_tools_resolve_the_quote_source_read_only(tmp_path):
    import sqlite3
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import broker_iv_tools as T

    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE appsetting (key TEXT, value_str TEXT)")
    con.execute("CREATE TABLE accountdefinition (id INTEGER, name TEXT, provider TEXT)")
    con.execute("INSERT INTO accountdefinition VALUES (3, 'alp', 'Alpaca')")
    con.commit()
    with pytest.raises(SystemExit, match="not a quote-source account"):
        T.quote_source_account_id(str(db), None)
    con.execute("INSERT INTO appsetting VALUES ('iv_live_chain_source', 'account:3')")
    con.commit()
    con.close()
    assert T.quote_source_account_id(str(db), None) == 3
    assert T.quote_source_account_id(str(db), 8) == 8
    with pytest.raises(SystemExit, match="TastyTrade only"):          # refused before any credential read
        T.tastytrade_get_json(str(db), 3)
    assert T.last_closed_session(datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)) == S
