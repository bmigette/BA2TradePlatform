"""Plan Part G3 (BT/live option parity): META <- FB option-root alias in the ThetaData provider.

Meta Platforms' options traded under root FB until 2022-06-08 and META from 2022-06-09; before
that the META root was the Roundhill Metaverse ETF (strikes $4-27 in the local store), and the
partitions spanning the rename (exp 2022-08-19, 2022-09-16) mixed both companies. Every request
window is split at the cutover, the earlier part asked for under FB, and every row stored under
META with META OCC ids. A fake client answers per root; no network.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from ba2_common.core.interfaces import OptionContractMeta
from ba2_providers.options import thetadata as T
from ba2_providers.options.thetadata import ThetaDataOptionsProvider, _occ_symbol, _root_segments

CUT = date(2022, 6, 9)
EXP = date(2022, 8, 19)          # listed before the rename, expires after it


def test_the_provider_under_test_is_the_worktree_copy():
    assert "BA2-optparity" in T.__file__
    assert T.ROOT_HISTORY["META"] == ((CUT, "FB"),)


class _NoData(Exception):
    pass


def _days(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class _RootClient:
    """Answers FB with Meta's real chain (strike 190) and META with the ETF before the cutover
    (strike 10) / Meta after it (strike 190) -- what ThetaData serves. Records every call."""

    def __init__(self):
        self.calls = []

    def _strike(self, symbol, day):
        if symbol == "FB":
            return 190.0 if day < CUT else None
        if symbol == "META":
            return 10.0 if day < CUT else 190.0
        return None

    def _rows(self, symbol, a, b, date_col, expiry="*"):
        rows = []
        for d in _days(a, b):
            k = self._strike(symbol, d)
            if k is None:
                continue
            for right in ("CALL", "PUT"):
                rows.append({"symbol": symbol, "expiration": EXP.isoformat(), "strike": k,
                             "right": right, date_col: d.isoformat(), "open": 1.0, "high": 1.2,
                             "low": 0.9, "close": 1.1, "volume": 10, "bid": 1.0, "ask": 1.2,
                             "implied_vol": 0.4})
        if not rows:
            raise _NoData(symbol)
        return pd.DataFrame(rows)

    def option_history_eod(self, **kw):
        self.calls.append(("history_eod", kw["symbol"], kw["start_date"], kw["end_date"]))
        return self._rows(kw["symbol"], kw["start_date"], kw["end_date"], "created")

    def option_history_greeks_eod(self, **kw):
        self.calls.append(("greeks_eod", kw["symbol"], kw["start_date"], kw["end_date"]))
        return self._rows(kw["symbol"], kw["start_date"], kw["end_date"], "timestamp")

    def option_history_open_interest(self, **kw):
        self.calls.append(("open_interest", kw["symbol"], kw["start_date"], kw["end_date"]))
        df = self._rows(kw["symbol"], kw["start_date"], kw["end_date"], "timestamp")
        df["open_interest"] = 100
        return df

    def option_list_dates(self, **kw):
        self.calls.append(("list_dates", kw["symbol"], None, None))
        raise _NoData("no dates")

    def option_list_expirations(self, symbol):
        self.calls.append(("list_expirations", symbol, None, None))
        # the ETF's own June expiry under META; Meta's 2022-06-03 weekly under FB
        exps = {"FB": ["2022-06-03", EXP.isoformat()], "META": ["2022-06-03", EXP.isoformat()]}
        return pd.DataFrame({"expiration": exps[symbol]})

    def option_list_strikes(self, symbol, expiration):
        self.calls.append(("list_strikes", symbol, expiration, None))
        return pd.DataFrame({"strike": [190.0] if symbol == "FB" else [10.0, 190.0]})


def _wired(client):
    p = ThetaDataOptionsProvider(api_key="test-key")
    p._client = client
    p._no_data_exc = _NoData
    return p


def test_root_segments_split_meta_at_the_rename_and_leave_everything_else_alone():
    assert _root_segments("META", date(2022, 5, 1), date(2022, 7, 1)) == [
        ("FB", date(2022, 5, 1), date(2022, 6, 8)), ("META", CUT, date(2022, 7, 1))]
    assert _root_segments("meta", date(2022, 7, 1), date(2022, 8, 1)) == [
        ("META", date(2022, 7, 1), date(2022, 8, 1))]
    assert _root_segments("META", date(2021, 1, 4), date(2021, 2, 1)) == [
        ("FB", date(2021, 1, 4), date(2021, 2, 1))]
    assert _root_segments("AAPL", date(2022, 5, 1), date(2022, 7, 1)) == [
        ("AAPL", date(2022, 5, 1), date(2022, 7, 1))]


def test_the_wide_shape_asks_fb_before_the_rename_and_meta_after_all_stored_as_meta():
    client = _RootClient()
    bars = list(_wired(client).fetch_underlying_eod_bars(
        "META", start=date(2022, 5, 23), end=date(2022, 6, 17)))
    eod = [(sym, a, b) for name, sym, a, b in client.calls if name == "history_eod"]
    assert all(b < CUT for sym, a, b in eod if sym == "FB")
    assert all(a >= CUT for sym, a, b in eod if sym == "META")
    assert {sym for sym, _a, _b in eod} == {"FB", "META"}
    # greeks + OI follow the same root as their bars
    for name in ("greeks_eod", "open_interest"):
        for _n, sym, a, b in (c for c in client.calls if c[0] == name):
            assert (sym == "FB") == (b < CUT) and (sym == "META") == (a >= CUT)
    # every bar is Meta (strike 190, never the ETF's 10) under a META OCC id
    assert bars and {b.occ_symbol[:4] for b in bars} == {"META"}
    assert {b.occ_symbol for b in bars} == {_occ_symbol("META", EXP, "call", 190.0),
                                           _occ_symbol("META", EXP, "put", 190.0)}
    # before the rename the rows exist (from FB), with IV and OI joined across the alias
    early = [b for b in bars if b.bar_date < CUT]
    assert early and all(b.iv == 0.4 and b.open_interest == 100 for b in early)
    # the ordering contract holds across the root boundary
    assert [b.bar_date for b in bars] == sorted(b.bar_date for b in bars)


def test_the_per_expiry_shape_asks_fb_then_meta_and_keys_rows_on_meta_occ_ids():
    client = _RootClient()
    contracts = [OptionContractMeta(occ_symbol=_occ_symbol("META", EXP, r, 190.0),
                                    underlying="META", option_type=r, strike=190.0, expiry=EXP)
                 for r in ("call", "put")]
    bars = list(_wired(client).fetch_eod_bars(contracts, start=date(2022, 5, 23),
                                              end=date(2022, 6, 17)))
    greeks = [(sym, a, b) for name, sym, a, b in client.calls if name == "greeks_eod"]
    assert [sym for sym, _a, _b in greeks] == ["FB", "META"]
    assert greeks[0][2] == date(2022, 6, 8) and greeks[1][1] == CUT
    assert ("list_dates", "FB", None, None) in client.calls          # listed under FB
    assert {b.occ_symbol for b in bars} == {c.occ_symbol for c in contracts}
    assert min(b.bar_date for b in bars) == date(2022, 5, 23)       # pre-rename rows kept


def test_the_etf_never_reaches_the_store_even_for_a_window_entirely_before_the_rename():
    client = _RootClient()
    bars = list(_wired(client).fetch_underlying_eod_bars(
        "META", start=date(2022, 5, 2), end=date(2022, 5, 27)))
    assert {sym for name, sym, _a, _b in client.calls if name == "history_eod"} == {"FB"}
    assert bars and all("C00190000" in b.occ_symbol or "P00190000" in b.occ_symbol for b in bars)


def test_discovery_lists_fb_for_pre_rename_expiries_and_emits_meta_contracts():
    client = _RootClient()
    out = _wired(client).discover_contracts("META", expiry_gte=date(2022, 6, 1),
                                            expiry_lte=date(2022, 9, 30))
    assert {c.underlying for c in out} == {"META"}
    assert all(c.occ_symbol.startswith("META") for c in out)
    by_exp = {}
    for c in out:
        by_exp.setdefault(c.expiry, set()).add(c.strike)
    # the pre-rename weekly comes from FB only: never the ETF's $10 strike
    assert by_exp[date(2022, 6, 3)] == {190.0}
    assert ("list_strikes", "META", date(2022, 6, 3), None) not in client.calls
    # the expiry spanning the rename: META lists the ETF's $10 strike too, but it has no
    # quote/trade under META after 2022-06-09, so it is excluded; Meta's 190 stays, once
    assert by_exp[EXP] == {190.0}
    assert len([c for c in out if c.expiry == EXP and c.strike == 190.0]) == 2
    probes = [(sym, a, b) for name, sym, a, b in client.calls if name == "greeks_eod"]
    assert probes == [("META", EXP - timedelta(days=14), EXP)]      # after the cutover only


def test_the_live_strike_probe_never_reaches_before_the_cutover():
    client = _RootClient()
    live = _wired(client)._strikes_live_under(client, "META", date(2022, 6, 17), CUT)
    assert live == {190.0}
    assert [(a, b) for name, _s, a, b in client.calls if name == "greeks_eod"] == [
        (CUT, date(2022, 6, 17))]


def test_a_symbol_without_root_history_issues_exactly_the_old_requests():
    class _Plain(_RootClient):
        def _strike(self, symbol, day):
            return 150.0 if symbol == "AAPL" else None

    client = _Plain()
    list(_wired(client).fetch_underlying_eod_bars("AAPL", start=date(2022, 5, 23),
                                                  end=date(2022, 6, 17)))
    eod = [(sym, a, b) for name, sym, a, b in client.calls if name == "history_eod"]
    assert eod == [("AAPL", a, b) for a, b in T._chunk_window(date(2022, 5, 23), date(2022, 6, 17))]
