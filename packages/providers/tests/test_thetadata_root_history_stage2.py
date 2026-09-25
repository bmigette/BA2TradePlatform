"""Stage-2 universe (2026-09-25): ticker-reuse root history beyond META <- FB.

Measured 2026-09-25 with ``option_history_eod`` (expiration="*") around each rename: the OLD
root's last session, the NEW root's first session carrying the new company's chain (strike
ladder and put-call-parity spot vs the FMP close of the platform symbol):

  COHR <- IIVI  IIVI rows through 2022-09-07 (parity 41.98 vs FMP COHR 41.95); COHR from
                2022-09-08 (43.03 vs 43.26). The COHR root before it is Coherent Inc (acquired
                2022-07): parity / FMP 2.9-3.8 over 2020-2022.
  B     <- GOLD GOLD through 2025-05-08 (18.89 vs FMP B 18.86); B from 2025-05-09 (19.47 vs
                19.46). B before it is Barnes Group (acquired 2025-01): parity / FMP 1.7-3.5.
  IR    <- GDI  GDI through 2020-02-28 (32.08 vs FMP IR 32.79); IR from 2020-03-02 (strikes
                15-45). IR before it is Ingersoll-Rand plc (now TT), strikes 65-190: 3.8x.
  CZR   <- ERI  ERI through 2020-07-20 (38.12 vs FMP CZR 38.24); CZR from 2020-07-21 (37.82 vs
                38.40). CZR before it is Caesars Entertainment Corp ($12): 0.22-0.49x.
  DOC   <- PEAK PEAK quoted through 2024-03-01 (17.42 vs FMP DOC 17.10); DOC from 2024-03-04
                (strikes 2.5-35). DOC before it is Physicians Realty ($11): 0.50-0.69x.
  BN    <- BAM  the old Brookfield Asset Management chain (BAM root) through 2022-12-09:
                parity 43.17 = FMP BN 23.26 x 1.237 (the 2022-12-12 spin in BN's calendar) x
                1.5 (the 2025-10-10 split); the BN root has nothing before 2023-01-06.
  BAM   <- none the new Brookfield Asset Management listed 2022-12-12 (FMP BAM from
                2022-12-01, when-issued at $32); the BAM root before that is the OLD company
                (parity 43-45, ratio 1.357 vs FMP BAM): there is NO faithful chain before the
                cutover, so none is fetched. The new BAM's chain starts 2023-01-06.

MTCH is NOT a root change: FMP's MTCH history before 2020-07-01 is the old Match Group's own
price (old MTCH root parity / FMP 0.282-0.287 x the 3.502 calendar row = 0.99-1.00), and IAC's
chain does not match it (parity / FMP 2.65-3.17, not constant). It is a split-basis override.
A fake client answers per root; no network.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from ba2_common.core.interfaces import OptionContractMeta
from ba2_providers.options import thetadata as T
from ba2_providers.options.thetadata import ThetaDataOptionsProvider, _occ_symbol, _root_segments


def _in_this_checkout(module_file) -> bool:
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[2]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False


EXPECTED = {
    "META": ((date(2022, 6, 9), "FB"),),
    "COHR": ((date(2022, 9, 8), "IIVI"),),
    "B": ((date(2025, 5, 9), "GOLD"),),
    "IR": ((date(2020, 3, 2), "GDI"),),
    "CZR": ((date(2020, 7, 21), "ERI"),),
    "DOC": ((date(2024, 3, 4), "PEAK"),),
    "BN": ((date(2022, 12, 12), "BAM"),),
    "BAM": ((date(2022, 12, 12), None),),
}


def test_the_provider_under_test_is_the_worktree_copy():
    assert _in_this_checkout(T.__file__), T.__file__


def test_the_shipped_root_history_is_the_measured_one():
    assert dict(T.ROOT_HISTORY) == EXPECTED
    assert "MTCH" not in T.ROOT_HISTORY          # an override, not a root change


@pytest.mark.parametrize("sym,cut,old", [(s, h[0][0], h[0][1]) for s, h in EXPECTED.items()
                                         if h[0][1] is not None])
def test_every_alias_splits_the_window_at_its_cutover(sym, cut, old):
    a, b = cut - timedelta(days=20), cut + timedelta(days=20)
    assert _root_segments(sym, a, b) == [(old, a, cut - timedelta(days=1)), (sym, cut, b)]
    assert _root_segments(sym, a, cut - timedelta(days=1)) == [(old, a, cut - timedelta(days=1))]
    assert _root_segments(sym, cut, b) == [(sym, cut, b)]


BAM_CUT = date(2022, 12, 12)


def test_a_none_root_means_no_chain_before_the_cutover():
    assert _root_segments("BAM", date(2022, 11, 1), date(2022, 12, 31)) == [
        ("BAM", BAM_CUT, date(2022, 12, 31))]
    assert _root_segments("BAM", date(2020, 1, 1), date(2022, 12, 11)) == []
    assert _root_segments("bam", date(2023, 1, 2), date(2023, 2, 1)) == [
        ("BAM", date(2023, 1, 2), date(2023, 2, 1))]


class _NoData(Exception):
    pass


EXP = date(2023, 1, 20)          # listed before the cutover, expires after it


def _days(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class _BamClient:
    """BAM root: the OLD company (strike 45) before the cutover, the new one (strike 30) from
    it. BN root: nothing before the cutover (ThetaData's real answer), strike 35 after."""

    def __init__(self):
        self.calls = []

    def _strike(self, symbol, day):
        if symbol == "BAM":
            return 45.0 if day < BAM_CUT else 30.0
        if symbol == "BN":
            return None if day < BAM_CUT else 35.0
        return None

    def _rows(self, symbol, a, b, date_col):
        rows = []
        for d in _days(a, b):
            k = self._strike(symbol, d)
            if k is None:
                continue
            for right in ("CALL", "PUT"):
                rows.append({"symbol": symbol, "expiration": EXP.isoformat(), "strike": k,
                             "right": right, date_col: d.isoformat(), "open": 1.0, "high": 1.2,
                             "low": 0.9, "close": 1.1, "volume": 10, "bid": 1.0, "ask": 1.2,
                             "implied_vol": 0.3})
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
        df["open_interest"] = 7
        return df

    def option_list_dates(self, **kw):
        self.calls.append(("list_dates", kw["symbol"], None, None))
        raise _NoData("no dates")

    def option_list_expirations(self, symbol):
        self.calls.append(("list_expirations", symbol, None, None))
        return pd.DataFrame({"expiration": ["2022-12-02", EXP.isoformat()]})

    def option_list_strikes(self, symbol, expiration):
        self.calls.append(("list_strikes", symbol, expiration, None))
        return pd.DataFrame({"strike": [30.0, 45.0] if symbol == "BAM" else [35.0, 45.0]})


def _wired(client):
    p = ThetaDataOptionsProvider(api_key="test-key")
    p._client = client
    p._no_data_exc = _NoData
    return p


def test_bam_wide_fetch_never_asks_before_the_cutover_and_never_stores_the_old_company():
    client = _BamClient()
    bars = list(_wired(client).fetch_underlying_eod_bars(
        "BAM", start=date(2022, 11, 21), end=date(2022, 12, 30)))
    assert all(a >= BAM_CUT for name, sym, a, b in client.calls if a is not None)
    assert {sym for name, sym, _a, _b in client.calls} == {"BAM"}
    assert bars and min(b.bar_date for b in bars) == BAM_CUT
    assert {b.occ_symbol for b in bars} == {_occ_symbol("BAM", EXP, "call", 30.0),
                                           _occ_symbol("BAM", EXP, "put", 30.0)}


def test_bam_window_entirely_before_the_cutover_makes_no_request_and_yields_nothing():
    client = _BamClient()
    p = _wired(client)
    assert list(p.fetch_underlying_eod_bars("BAM", start=date(2022, 1, 3),
                                            end=date(2022, 12, 9))) == []
    contracts = [OptionContractMeta(occ_symbol=_occ_symbol("BAM", date(2022, 12, 2), r, 45.0),
                                    underlying="BAM", option_type=r, strike=45.0,
                                    expiry=date(2022, 12, 2)) for r in ("call", "put")]
    assert list(p.fetch_eod_bars(contracts, start=date(2022, 10, 3), end=date(2022, 12, 9))) == []
    assert client.calls == []


def test_bam_per_expiry_fetch_spanning_the_cutover_asks_only_from_it():
    client = _BamClient()
    contracts = [OptionContractMeta(occ_symbol=_occ_symbol("BAM", EXP, r, 30.0),
                                    underlying="BAM", option_type=r, strike=30.0, expiry=EXP)
                 for r in ("call", "put")]
    bars = list(_wired(client).fetch_eod_bars(contracts, start=date(2022, 11, 21), end=EXP))
    greeks = [(sym, a, b) for name, sym, a, b in client.calls if name == "greeks_eod"]
    assert greeks and all(sym == "BAM" and a >= BAM_CUT for sym, a, _b in greeks)
    assert bars and min(b.bar_date for b in bars) == BAM_CUT


def test_bam_discovery_skips_pre_cutover_expiries_and_the_old_company_strikes():
    client = _BamClient()
    out = _wired(client).discover_contracts("BAM", expiry_gte=date(2022, 11, 1),
                                            expiry_lte=date(2023, 3, 1))
    assert {c.expiry for c in out} == {EXP}
    assert {c.strike for c in out} == {30.0}      # 45 has no row under BAM after the cutover


def test_bn_gets_the_old_company_chain_from_the_bam_root_stored_as_bn():
    client = _BamClient()
    bars = list(_wired(client).fetch_underlying_eod_bars(
        "BN", start=date(2022, 11, 21), end=date(2022, 12, 30)))
    eod = [(sym, a, b) for name, sym, a, b in client.calls if name == "history_eod"]
    assert all(b < BAM_CUT for sym, a, b in eod if sym == "BAM")
    assert all(a >= BAM_CUT for sym, a, b in eod if sym == "BN")
    early = [b for b in bars if b.bar_date < BAM_CUT]
    late = [b for b in bars if b.bar_date >= BAM_CUT]
    assert early and {b.occ_symbol for b in early} == {_occ_symbol("BN", EXP, "call", 45.0),
                                                        _occ_symbol("BN", EXP, "put", 45.0)}
    assert late and {b.occ_symbol for b in late} == {_occ_symbol("BN", EXP, "call", 35.0),
                                                      _occ_symbol("BN", EXP, "put", 35.0)}
    assert [b.bar_date for b in bars] == sorted(b.bar_date for b in bars)
