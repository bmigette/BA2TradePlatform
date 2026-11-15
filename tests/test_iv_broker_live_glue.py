"""Live glue of the iv-v1 TastyTrade source (no real broker call: fakes + recorded payloads).

* the source is chosen by the app setting ``iv_live_chain_source`` (unset | alphavantage | tastytrade) and
  ``tastytrade`` uses the APP-LEVEL market-data credentials (no account definition); blank credentials
  make the gates refuse naming the settings;
* the ONE scheduler entry is parameterised by the source (AV prefetch cron / capture every 30 min); no
  gate -> nothing scheduled, nothing logged; an unset source with a gate logs one ERROR;
* the capture window: after 20:15 ET on the session's date until 10 min before the next open;
* SCREENER COVERAGE: the capture takes the union of the bounded universes; a screener pick that was
  captured evaluates, a pick that was not refuses explicitly (counted in the pass summary);
* the operator tools read credentials read-only from the DB they are pointed at (app settings or
  ``--use-account-id``) and refuse a non-TastyTrade or sandbox account.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ba2_common.core.option_chain_quotes import (
    FETCH_STRIKE_BAND, OptionChainSourceUnavailable, fetch_expiry_window, validate_prior_session_snapshot,
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


class _FakeClient:
    """The market-data client's interface over the recorded payloads."""

    def __init__(self, configured=True):
        self.configured, self.requests, self.paths = configured, 0, []

    def get_json(self, path, params=None):
        if not self.configured:
            raise OptionChainSourceUnavailable(
                "TastyTrade market-data credentials are not set (app settings tastytrade_market_client_secret "
                "and tastytrade_market_refresh_token)")
        self.requests += 1
        self.paths.append(path)
        sym = path.split("/")[2] if path.startswith("/option-chains/") else None
        sym = sym or (params.get("equity") or [None])[0] or params["equity-option"][0].split()[0]
        return _data(_fx(sym), path, params)


# --------------------------------------------------------------------------- settings
def test_the_settings_are_declared_secret_and_optional():
    from ba2_trade_platform.core.app_settings import APP_SETTINGS_DEFINITIONS
    from ba2_trade_platform.ui.utils.secret_mask import is_secret_setting

    for key in ("tastytrade_market_client_secret", "tastytrade_market_refresh_token"):
        assert APP_SETTINGS_DEFINITIONS[key]["required"] is False and "default" not in APP_SETTINGS_DEFINITIONS[key]
        assert is_secret_setting(key)
    src = APP_SETTINGS_DEFINITIONS["iv_live_chain_source"]
    assert src["choices"] == ["", "alphavantage", "tastytrade"] and "default" not in src


def test_blank_credentials_make_the_gates_refuse_naming_the_settings(monkeypatch):
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    monkeypatch.setattr(ivp, "tastytrade_market_data_client", lambda: _FakeClient(configured=False))
    r = ivp.make_tastytrade_chain_reader("iv-v1", cache_root="x")
    assert isinstance(r, LiveUnavailableMarketConditionReader)
    assert "tastytrade_market_client_secret" in r.reason and "tastytrade_market_refresh_token" in r.reason
    monkeypatch.setattr(ivp, "tastytrade_market_data_client", lambda: _FakeClient())
    r = ivp.make_tastytrade_chain_reader("iv-v1", cache_root="x")
    assert r.serving_kind == "tastytrade" and r.option_source == "tastytrade"


def test_the_real_client_reads_blank_app_settings_without_breaking():
    """The test DB has no TastyTrade setting: the client exists, is not configured, and no call is made."""
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    c = ivp.tastytrade_market_data_client()
    assert c.configured is False and "credentials hidden" in repr(c)
    assert "tastytrade_market_client_secret" in ivp.tastytrade_credentials_reason()


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
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "tastytrade")
    assert ivp.schedule_spec() is None and logged == []          # no gate: nothing, silently
    monkeypatch.setattr(ivp, "has_active_iv_gates", lambda: True)
    monkeypatch.setattr(ivp, "configured_source_value", lambda: None)
    assert ivp.schedule_spec() is None
    assert len(logged) == 1 and logged[0][0] == "error" and "iv_live_chain_source" in logged[0][1]
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "alphavantage")
    assert ivp.schedule_spec()["cron"] == {"hour": 6, "minute": 30, "day_of_week": "mon-fri"}
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "tastytrade")
    monkeypatch.setattr(ivp, "tastytrade_market_data_client", lambda: _FakeClient())
    monkeypatch.setattr(ivp, "capture_universe", lambda instances=None: (["MSFT"], {}))
    logged.clear()
    spec = ivp.schedule_spec()
    assert spec["cron"] == {"minute": "15,45"} and spec["source"] == "tastytrade" and logged == []
    monkeypatch.setattr(ivp, "capture_universe", lambda instances=None: (["MSFT"], {7: "set screener_symbol_universe"}))
    monkeypatch.setattr(ivp, "tastytrade_market_data_client", lambda: _FakeClient(configured=False))
    ivp.schedule_spec()
    assert [l for l, _m in logged] == ["error", "error"]
    assert "tastytrade_market_client_secret" in logged[0][1] and "unbounded universe" in logged[1][1]


@pytest.mark.parametrize("local, want", [
    (datetime(2026, 11, 13, 20, 0), None),                        # before the first attempt
    (datetime(2026, 11, 13, 20, 20), S),
    (datetime(2026, 11, 14, 12, 0), S),                           # Saturday
    (datetime(2026, 11, 16, 9, 19), S),                           # Monday, before 09:20
    (datetime(2026, 11, 16, 9, 21), None),                        # too close to the open
    (datetime(2026, 11, 16, 12, 0), None),                        # Monday session running
    (datetime(2026, 11, 16, 21, 0), date(2026, 11, 16)),
])
def test_the_capture_window(local, want):
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


def test_a_screener_pick_is_served_only_when_it_was_in_the_captured_universe(tmp_path):
    from ba2_common.core.market_conditions import STATUS_NO_CONTEXT, STATUS_VALID
    from ba2_providers.market_conditions.broker_live_chain import BrokerLiveChainReader
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    root = tmp_path / "cache"
    for sym in ("MSFT", "EMN", "NVDA"):
        _bars(root, sym)
    client = _FakeClient()
    instances = [(5, _Expert(["SCREENER"], "MSFT, EMN")), (6, _Expert(["DYNAMIC"]))]
    rep = ivp.run_capture_once(datetime(2026, 11, 13, 20, 30, tzinfo=NY), client=client, cache_root=str(root),
                               instances=instances)
    assert rep["captured"] == 2 and list(rep["unbounded_instances"]) == [6]
    assert all(p.startswith(("/option-chains/", "/market-data/by-type")) for p in client.paths)
    reader = BrokerLiveChainReader("iv-v1", source="tastytrade", cache_root=str(root))
    token = object()
    reader._current_pass = lambda: token
    picked_captured = reader.observe("EMN", S)
    assert all(o.status == STATUS_VALID for o in picked_captured.values.values())
    picked_outside = reader.observe("NVDA", S)
    assert all(o.status == STATUS_NO_CONTEXT and "not in the captured universe" in o.reason
               for o in picked_outside.values.values())
    assert list(reader._pass_state(token).reasons) == ["not_in_captured_universe"]


def test_a_capture_with_blank_credentials_records_why_and_the_gate_says_so(tmp_path):
    from ba2_providers.market_conditions.broker_live_chain import BrokerLiveChainReader
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    rep = ivp.run_capture_once(datetime(2026, 11, 13, 20, 30, tzinfo=NY), client=_FakeClient(configured=False),
                               cache_root=str(tmp_path), instances=[(5, _Expert(["EMN"]))])
    assert rep["captured"] == 0 and "tastytrade_market_client_secret" in rep["unsupported"]
    reader = BrokerLiveChainReader("iv-v1", source="tastytrade", cache_root=str(tmp_path))
    assert "tastytrade_market_client_secret" in reader.observe("EMN", S).values["underlying_iv_rv_ratio_30_20"].reason


def test_outside_the_window_nothing_is_fetched(tmp_path):
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    class _Boom:
        def get_json(self, *a, **k):
            raise AssertionError("no fetch outside the window")
    rep = ivp.run_capture_once(datetime(2026, 11, 16, 12, 0, tzinfo=NY), client=_Boom(), cache_root=str(tmp_path),
                               instances=[(5, _Expert(["MSFT"]))])
    assert rep == {"skipped": "outside the broker capture window"}


# --------------------------------------------------------------------------- operator tools
def _tools():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import broker_iv_tools as T
    return T


def test_the_operator_tools_read_credentials_read_only(tmp_path):
    import sqlite3

    T = _tools()
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE appsetting (key TEXT, value_str TEXT)")
    con.execute("CREATE TABLE accountdefinition (id INTEGER, name TEXT, provider TEXT)")
    con.execute("CREATE TABLE accountsetting (account_id INTEGER, key TEXT, value_str TEXT, value_json TEXT, "
                "value_float REAL)")
    con.execute("INSERT INTO accountdefinition VALUES (3, 'alp', 'Alpaca'), (4, 'tt', 'TastyTrade'), (5, 'tt2', 'TastyTrade')")
    for acct, test in ((4, '"false"'), (5, '"true"')):
        con.execute("INSERT INTO accountsetting VALUES (?, 'client_secret', 'S', NULL, NULL)", (acct,))
        con.execute("INSERT INTO accountsetting VALUES (?, 'refresh_token', 'R', NULL, NULL)", (acct,))
        con.execute("INSERT INTO accountsetting VALUES (?, 'is_test', NULL, ?, NULL)", (acct, test))
    con.commit()
    with pytest.raises(SystemExit, match="not set"):                     # blank app credentials
        T.market_data_client(str(db))
    con.execute("INSERT INTO appsetting VALUES ('tastytrade_market_client_secret', 'APP-S'), "
                "('tastytrade_market_refresh_token', 'APP-R')")
    con.commit()
    con.close()
    assert T.market_data_client(str(db)).configured
    assert T.market_data_client(str(db), 4).configured                    # --use-account-id
    with pytest.raises(SystemExit, match="needs a TastyTrade account"):
        T.market_data_client(str(db), 3)
    with pytest.raises(SystemExit, match="sandbox"):
        T.market_data_client(str(db), 5)
    assert T.last_closed_session(datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)) == S


def test_the_certification_tool_compares_like_the_live_path():
    """tools/certify_broker_iv_chain.compare on the recorded chains: identical quotes, rows, no flips."""
    import numpy as np

    from ba2_providers.market_conditions.av_live_chain import chain_quotes
    from ba2_providers.market_conditions.broker_live_chain import map_snapshot
    from ba2_providers.options.alphavantage import map_chain
    from ba2_providers.options.tastytrade_quotes import fetch_chain_quote_snapshot

    _tools()
    import certify_broker_iv_chain as C

    fx = _fx("EMN")
    lo, hi = fetch_expiry_window(S)
    snap = fetch_chain_quote_snapshot(lambda p, q: _data(fx, p, q), "EMN", expiry_min=lo, expiry_max=hi,
                                      strike_band=FETCH_STRIKE_BAND)
    verdict = validate_prior_session_snapshot(snap, S)
    broker = chain_quotes(map_snapshot("EMN", S, verdict.kept)[0], "EMN", S)
    av_frame, _ = map_chain("EMN", S, pd.read_parquet(FIX / f"av_EMN_{S}.parquet").to_dict("records"))
    av = chain_quotes(av_frame[["option_type", "strike", "expiry", "bar_date", "bid", "ask"]], "EMN", S)
    bars = fx["bars"][-128:]
    win = tuple(np.array([float(b[k]) for b in bars]) for k in ("open", "high", "low", "close", "volume"))
    rec = C.compare("EMN", S, broker, av, win, spot=snap.spot)
    assert rec["identical_quotes_share"] == 1.0 and rec["identical_rows"] is True
    assert all(v["flipped"] == 0 for v in rec["gate_flips"].values()) and rec["gate_flips"]
