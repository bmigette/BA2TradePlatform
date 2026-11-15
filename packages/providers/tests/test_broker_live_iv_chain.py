"""Broker quote source for LIVE iv-v1 (TastyTrade), on the payloads recorded 2026-11-15.

Fixtures (``fixtures/broker_iv``): the TastyTrade REST chain + market-data items of MSFT, NVDA and EMN for
session 2026-11-13 (read-only probe; trimmed, no secrets), the Alpha Vantage HISTORICAL_OPTIONS rows of
the same session (7..130-day expiries, strikes within 30% of the close) and the FMP daily bars.

Pinned:
* the broker path (REST mapping -> data validity rule -> the store's acceptance rule -> the store
  reader's filter -> ``compute_iv_v1``) gives the IDENTICAL row the Alpha Vantage chain gives;
* the fetched strike band reproduces the wide-chain row (15% and the shipped 20%);
* every invalid snapshot (all-null quotes, quotes of S-1, quotes of S+1 mixed in, an update after the
  next open, no spot, empty) is REFUSED, never cached, recorded in the capture manifest, and the reader
  refuses with that reason; a missing capture / a symbol outside the captured universe refuse explicitly
  and are counted per kind in the pass summary;
* one failing symbol never stops a capture; an unsupported account does; the stop flag stops it;
* capture evidence records the serving kind and the snapshot facts, and replay recomputes the row.
"""
from __future__ import annotations

import dataclasses
import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from ba2_common.core.market_conditions import CHAIN_COMPUTE_BY_PROFILE, IV_PROFILE, STATUS_NO_CONTEXT, STATUS_VALID
from ba2_common.core.option_chain_quotes import (
    FETCH_STRIKE_BAND, OptionChainQuotesFetchError, OptionChainSourceUnavailable, fetch_expiry_window,
    validate_prior_session_snapshot,
)
from ba2_providers.market_conditions.av_live_chain import AlphaVantageLiveChainReader, chain_quotes, read_cached
from ba2_providers.market_conditions.broker_live_chain import (
    BrokerLiveChainReader, broker_chain_dir, capture_chains, capture_manifest, map_snapshot, read_snapshot_facts,
)
from ba2_providers.options.alphavantage import map_chain
from ba2_providers.options.tastytrade_quotes import fetch_chain_quote_snapshot

FIX = Path(__file__).parent / "fixtures" / "broker_iv"
S = date(2026, 11, 13)
SYMS = ("MSFT", "NVDA", "EMN")
FETCHED = datetime(2026, 11, 15, 11, 51, tzinfo=timezone.utc)
KIND = "tastytrade"


def _fx(sym):
    return json.loads((FIX / f"tt_{sym}.json").read_text())


def _write_bars(root: Path, sym: str):
    bars = _fx(sym)["bars"]
    df = pd.DataFrame({"Date": pd.to_datetime([b["date"] for b in bars]),
                       "Open": [float(b["open"]) for b in bars], "High": [float(b["high"]) for b in bars],
                       "Low": [float(b["low"]) for b in bars], "Close": [float(b["close"]) for b in bars],
                       "Volume": [float(b["volume"]) for b in bars]})
    df["effective_date"] = df["Date"]
    p = root / "FMPOHLCVProvider" / f"{sym}_1d.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "cache"
    for sym in SYMS:
        _write_bars(r, sym)
    return r


def _get_json(fx, calls=None):
    def get(path, params):
        if calls is not None:
            calls.append((path, params))
        if path.startswith("/option-chains/"):
            return fx["chain"]
        if params and "equity" in params:
            return {"items": [fx["equity"]]}
        want = set(params["equity-option"])
        assert len(want) <= 100
        return {"items": [q for q in fx["quotes"] if q["symbol"] in want]}
    return get


def _snapshot(sym, band=FETCH_STRIKE_BAND, calls=None):
    lo, hi = fetch_expiry_window(S)
    return fetch_chain_quote_snapshot(_get_json(_fx(sym), calls), sym, expiry_min=lo, expiry_max=hi,
                                      strike_band=band, now=lambda: FETCHED)


def _av_row(root, sym, band=None):
    rows = pd.read_parquet(FIX / f"av_{sym}_{S}.parquet").to_dict("records")
    frame, _ = map_chain(sym, S, rows)
    if band is not None:
        spot = float(_fx(sym)["equity"]["close"])
        frame = frame[(frame["strike"] / spot - 1).abs() <= band]
    win = AlphaVantageLiveChainReader(IV_PROFILE, cache_root=str(root))._window(sym, S)
    return CHAIN_COMPUTE_BY_PROFILE[IV_PROFILE](S, chain_quotes(frame, sym, S), *win)


def _reader(root):
    return BrokerLiveChainReader(IV_PROFILE, source=KIND, cache_root=str(root))


def _capture(root, symbols, snapshot_fn, **kw):
    return capture_chains(symbols, S, snapshot_fn=snapshot_fn, source=KIND,
                          cache_root=str(root), pause_s=0, stop=threading.Event(), **kw)


# --------------------------------------------------------------------------- identity with the AV chain
@pytest.mark.parametrize("sym", SYMS)
def test_the_broker_row_is_identical_to_the_alpha_vantage_row(root, sym):
    calls = []
    snap = _snapshot(sym, calls=calls)
    assert snap.spot_summary_date == S and snap.quotes and snap.requests == len(calls)
    assert all(len(p["equity-option"]) <= 100 for _path, p in calls if p and "equity-option" in p)
    verdict = validate_prior_session_snapshot(snap, S)
    assert verdict.ok, verdict.reason
    rep = _capture(root, [sym], lambda s: _snapshot(s))
    assert rep["captured"] == 1 and not rep["refused"] and not rep["failed"]
    row = _reader(root).observe(sym, S)
    want = _av_row(root, sym)
    assert all(o.status == STATUS_VALID for o in want.values.values())
    assert dict(row.by_field()) == dict(want.by_field())           # NO tolerance


@pytest.mark.parametrize("sym", SYMS)
@pytest.mark.parametrize("band", [0.15, FETCH_STRIKE_BAND])
def test_the_fetched_strike_band_reproduces_the_wide_chain_row(root, sym, band):
    assert dict(_av_row(root, sym, band).by_field()) == dict(_av_row(root, sym).by_field())


def test_the_cached_chain_carries_the_snapshot_facts(root):
    _capture(root, ["MSFT"], lambda s: _snapshot(s))
    from ba2_providers.market_conditions.av_live_chain import chain_path

    facts = read_snapshot_facts(chain_path(str(root), "MSFT", S, broker_chain_dir(KIND)))
    assert facts["source_kind"] == "tastytrade" and facts["serving_kind"] == KIND
    assert facts["summary_dates"] == {S.isoformat(): 894}
    assert facts["updated_at_min"].startswith("2026-11-14T00:59") and facts["updated_at_max"].startswith("2026-11-14T00:59")
    assert facts["near_money_of_session"] == facts["near_money"] > 0
    man = capture_manifest(str(root), KIND, S)
    assert man["symbols"]["MSFT"]["status"] == "captured" and man["source"] == KIND


# --------------------------------------------------------------------------- the data validity rule
def _alter(snap, changes_for):
    return dataclasses.replace(snap, quotes=tuple(dataclasses.replace(q, **changes_for(q)) for q in snap.quotes))


def _near(snap, q):
    return abs(q.strike / snap.spot - 1) <= 0.05


BAD = {
    "all_null_quotes": (lambda s: _alter(s, lambda q: {"bid": None, "ask": None}), "not_session_quotes"),
    "quotes_of_s_minus_1": (lambda s: _alter(s, lambda q: {"summary_date": date(2026, 11, 12)} if _near(s, q) else {}),
                            "not_session_quotes"),
    "mixed_s_and_s_plus_1": (lambda s: _alter(s, lambda q: {"summary_date": date(2026, 11, 16)}
                                              if q.strike == s.quotes[0].strike else {}), "snapshot_after_session"),
    "updated_after_next_open": (lambda s: _alter(s, lambda q: {"updated_at": datetime(2026, 11, 16, 14, 31,
                                                                                      tzinfo=timezone.utc)}
                                                 if q is s.quotes[3] else {}), "snapshot_after_session"),
    "no_spot": (lambda s: dataclasses.replace(s, spot=None), "no_spot"),
    "empty": (lambda s: dataclasses.replace(s, quotes=()), "snapshot_empty"),
    "updated_before_the_close": (lambda s: _alter(s, lambda q: {"updated_at": datetime(2026, 11, 13, 18, 0,
                                                                                       tzinfo=timezone.utc)}),
                                 "not_session_quotes"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_an_invalid_snapshot_is_refused_never_cached_and_the_gate_says_why(root, case):
    mutate, kind = BAD[case]
    snap = mutate(_snapshot("MSFT"))
    verdict = validate_prior_session_snapshot(snap, S)
    assert not verdict.ok and verdict.kind == kind
    assert "snapshot not the prior-session close" in verdict.reason
    rep = _capture(root, ["MSFT"], lambda s: snap)
    assert rep["captured"] == 0 and "MSFT" in rep["refused"]
    assert read_cached(str(root), "MSFT", S, broker_chain_dir(KIND)) is None
    entry = capture_manifest(str(root), KIND, S)["symbols"]["MSFT"]
    assert entry["status"] == "refused" and entry["kind"] == kind and "summary_dates" in entry["facts"]
    row = _reader(root).observe("MSFT", S)
    assert all(o.status == STATUS_NO_CONTEXT and "snapshot not the prior-session close" in o.reason
               for o in row.values.values())
    # retried later in the window: a valid snapshot is then captured and served
    _capture(root, ["MSFT"], lambda s: _snapshot(s))
    assert read_cached(str(root), "MSFT", S, broker_chain_dir(KIND)) is not None


def test_a_few_stale_far_contracts_are_dropped_like_missing_quotes_not_refused(root):
    snap = _snapshot("MSFT")
    far = [q for q in snap.quotes if not _near(snap, q)][:20]
    snap2 = _alter(snap, lambda q: {"summary_date": date(2026, 11, 12)} if q in far else {})
    verdict = validate_prior_session_snapshot(snap2, S)
    assert verdict.ok and len(verdict.kept) == len(snap.quotes) - len(far)


# --------------------------------------------------------------------------- decision-path refusals
def _with_pass(reader):
    token = object()
    reader._current_pass = lambda: token
    return token


def test_missing_capture_and_outside_universe_refuse_explicitly_and_are_counted(root):
    r = _reader(root)
    token = _with_pass(r)
    row = r.observe("MSFT", S)
    assert "no broker chain capture ran for session 2026-11-13" in row.values["underlying_iv_rv_ratio_30_20"].reason
    _capture(root, ["MSFT"], lambda s: _snapshot(s))                # the universe = {MSFT}
    r2 = _reader(root)
    token2 = _with_pass(r2)
    assert r2.observe("MSFT", S).values["underlying_iv_rv_ratio_30_20"].status == STATUS_VALID
    picked = r2.observe("NVDA", S)                                  # a screener pick outside the universe
    assert "not in the captured universe" in picked.values["underlying_iv_rv_ratio_30_20"].reason
    assert list(r2._pass_state(token2).reasons) == ["not_in_captured_universe"]
    assert list(r._pass_state(token).reasons) == ["not_captured_session"]
    assert r2.refused == 1 and r2.computed == 1


# --------------------------------------------------------------------------- capture run behaviour
def test_one_failing_symbol_never_stops_the_run(root):
    def snap(sym):
        if sym == "NVDA":
            raise OptionChainQuotesFetchError("HTTP 502")
        return _snapshot(sym)
    rep = _capture(root, ["MSFT", "NVDA", "EMN"], snap)
    assert rep["captured"] == 2 and set(rep["failed"]) == {"NVDA"}
    assert capture_manifest(str(root), KIND, S)["symbols"]["NVDA"]["status"] == "failed"


def test_rejected_credentials_stop_the_run_with_their_reason(root):
    def snap(sym):
        raise OptionChainSourceUnavailable("TastyTrade rejected the market-data credentials (HTTP 401)")
    rep = _capture(root, ["MSFT", "NVDA"], snap)
    assert rep["unsupported"] and rep["captured"] == 0
    man = capture_manifest(str(root), KIND, S)["symbols"]
    assert {v["status"] for v in man.values()} == {"unsupported"}
    reason = _reader(root).observe("NVDA", S).values["underlying_iv_rv_ratio_30_20"].reason
    assert "rejected the market-data credentials" in reason


def test_the_stop_flag_stops_the_run(root):
    stop = threading.Event()

    def snap(sym):
        stop.set()
        return _snapshot(sym)
    rep = capture_chains(["EMN", "MSFT", "NVDA"], S, snapshot_fn=snap, source=KIND,
                         cache_root=str(root), pause_s=0, stop=stop)
    assert rep["stopped"] and rep["captured"] == 1 and rep["remaining"] == ["MSFT", "NVDA"]


def test_a_second_run_resumes_from_the_cache(root):
    n = {"calls": 0}

    def snap(sym):
        n["calls"] += 1
        return _snapshot(sym)
    _capture(root, ["MSFT", "EMN"], snap)
    rep = _capture(root, ["MSFT", "EMN"], snap)
    assert n["calls"] == 2 and rep["cached"] == 2


# --------------------------------------------------------------------------- capture / replay evidence
class _Capture:
    def __init__(self):
        self.observations = []

    def record_observation(self, **kw):
        self.observations.append(kw)

    def note_failure(self, what, exc):
        raise AssertionError(f"{what}: {exc}")


def test_capture_records_the_serving_source_and_facts_and_replay_recomputes(root):
    from ba2_common.core.market_condition_iv_live import ReplayChainReader
    from ba2_common.core.market_condition_readers import capturing_reader_for

    _capture(root, ["EMN"], lambda s: _snapshot(s))
    cap = _Capture()
    reader, _record = capturing_reader_for(_reader(root), cap, source_profile="fmp-daily-split-adjusted-v1",
                                           timing_policy="prior_session_v1")
    row = reader.observe("EMN", S)
    (p,) = [o["payload"] for o in cap.observations]
    assert p["serving_kind"] == KIND and p["option_source"] == KIND and p["chain_served_digest"]
    assert p["source_facts"]["source_kind"] == KIND and p["source_facts"]["summary_dates"] == {S.isoformat(): 24}
    assert p["source_facts"]["updated_at_min"] and p["source_facts"]["updated_at_max"]
    replay = ReplayChainReader.from_payloads(json.loads(json.dumps([p])), profile=IV_PROFILE, option_source=KIND)
    assert dict(replay.observe("EMN", S).by_field()) == dict(row.by_field())


# --------------------------------------------------------------------------- the market-data client
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class _Http:
    def __init__(self, token_status=200):
        self.token_status, self.calls = token_status, []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url))
        return _Resp(self.token_status, {"access_token": "ACCESS-XYZ", "expires_in": 900})

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url))
        assert headers["Authorization"] == "Bearer ACCESS-XYZ"
        return _Resp(200, {"data": {"items": []}})


SECRET, REFRESH = "CLIENT-SECRET-123", "REFRESH-TOKEN-456"


def test_the_market_data_client_calls_only_market_data_paths_and_hides_its_credentials():
    from ba2_providers.options.tastytrade_quotes import TastyTradeMarketDataClient

    http = _Http()
    c = TastyTradeMarketDataClient(SECRET, REFRESH, http=http)
    assert SECRET not in repr(c) and REFRESH not in repr(c)
    assert c.get_json("/market-data/by-type", {"equity": ["MSFT"]}) == {"items": []}
    c.get_json("/option-chains/MSFT/nested")
    assert [m for m, _u in http.calls] == ["POST", "GET", "GET"]          # one token, reused
    for path in ("/accounts/5WX/orders", "/customers/me", "/accounts", "/sessions"):
        with pytest.raises(PermissionError):
            c.get_json(path)
    assert len(http.calls) == 3                                           # refused before any request


def test_blank_or_rejected_credentials_make_the_source_unavailable_without_leaking():
    from ba2_providers.options.tastytrade_quotes import TastyTradeMarketDataClient

    for secret, refresh in (("", ""), (SECRET, ""), (None, None)):
        c = TastyTradeMarketDataClient(secret, refresh, http=_Http())
        assert not c.configured
        with pytest.raises(OptionChainSourceUnavailable, match="tastytrade_market_client_secret"):
            c.get_json("/market-data/by-type", {})
    c = TastyTradeMarketDataClient(SECRET, REFRESH, http=_Http(token_status=401))
    with pytest.raises(OptionChainSourceUnavailable) as e:
        c.get_json("/market-data/by-type", {})
    assert "HTTP 401" in str(e.value) and SECRET not in str(e.value) and REFRESH not in str(e.value)
