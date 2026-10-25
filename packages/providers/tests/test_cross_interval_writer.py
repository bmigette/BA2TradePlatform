"""The WRITER side of the cross-interval basis defect: a daily file rewritten on a new split basis must not
leave the symbol's intraday file silently on the old one, and an intraday refresh on a new basis must not be
merged into a file (or beside a daily cache) on the old one.

THE DEFECT (measured 2026-10-08): 7 of 250 random symbols had a 5-minute history a constant multiple
(0.10 .. 6.25) of their daily history for a whole year. ``force_full_refetch`` replaces the DAILY file on the
vendor's current basis; nothing touched the intraday files of the symbol.

THE MECHANISM (``MarketDataProviderInterface``):
  forward   daily replaced   -> every intraday file of the symbol is checked against the new daily levels and
                                MARKED STALE if it disagrees (no vendor call, no data change); a stale file
                                refuses every read (``get_ohlcv_data``) and every write until
                                ``force_full_refetch(<intraday>)`` replaces it on the vendor's basis.
  reverse   intraday replaced on a new basis while the daily file is old -> the daily file is re-fetched too
            (one cheap call) and the pair is verified; intraday FRAGMENTS (top-up / extension) on another
            basis than the daily file are refused before anything is persisted.
  live      daily reads never look at the intraday file; only an intraday read of a mismatched / stale file
            refuses (loudly, naming the symbol and the repair).

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_cross_interval_writer.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pandas as pd
import pytest

from ba2_common.core import native_cache, split_basis
from ba2_common.core.split_basis import IntradayBasisError, IntradayBasisMismatch, IntradayBasisStale
from ba2_providers.ohlcv import cross_interval_basis as cib
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

from .test_cross_interval_basis import make_pair

PROV = "FMPOHLCVProvider"
SYM = "ZSPLIT"
FIRST, LAST = "2023-01-03", "2023-12-29"


def _by_factor(df, f):
    out = df.copy()
    for c in ("Open", "High", "Low", "Close"):
        out[c] = out[c] * f
    return out


class _P(FMPOHLCVProvider):
    """The real provider, the network replaced by a truth store {interval: frame}."""

    def __init__(self, truth):
        super().__init__(api_key="test-key")
        self.truth = truth
        self.calls = []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.calls.append((symbol, interval))
        key = "1d" if interval in ("1d", "1day", "daily") else "5min"
        df = self.truth[key]
        lo, hi = pd.Timestamp(start_date), pd.Timestamp(end_date) + pd.Timedelta(days=1)
        lo = lo.tz_localize(None) if lo.tzinfo else lo
        hi = hi.tz_localize(None) if hi.tzinfo else hi
        return df[(df["Date"] >= lo) & (df["Date"] < hi)].reset_index(drop=True).copy()

    def _split_calendar(self, symbol, interval):
        return []


class _Blind(_P):
    """A provider with NO cross-interval check available: the base class's conservative direction."""
    HAS_CROSS_INTERVAL_CHECK = False


# the cache folder of an instance is its CLASS name: the stubs must share the real provider's folder
_P.__name__ = _P.__qualname__ = PROV
_Blind.__name__ = _Blind.__qualname__ = PROV


def _put(symbol, interval, df):
    out = df.copy()
    out["effective_date"] = out["Date"]
    assert native_cache.write_timeseries(PROV, symbol, interval, out)
    return native_cache.find_timeseries_path(PROV, symbol, interval)


def _digest(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def _verdict(symbol):
    return cib.store_for(PROV).check(symbol, "5min", FIRST, LAST)


@pytest.fixture(scope="module")
def new_basis():
    """(daily, intraday) on the vendor's CURRENT basis; the old basis is x2 of it (an un-adjusted 2:1)."""
    return make_pair()


@pytest.fixture
def sym(request):
    # one symbol per test: the caches live for the whole session
    return f"{SYM}{abs(hash(request.node.name)) % 10_000_000}"


def _seed_old(sym, new_basis):
    daily, intra = new_basis
    d_path = _put(sym, "1d", _by_factor(daily, 2.0))
    i_path = _put(sym, "5min", _by_factor(intra, 2.0))
    assert _verdict(sym).klass == cib.KLASS_OK                       # the PAIR is consistent on the old basis
    return d_path, i_path


# --------------------------------------------------------------------------------------- forward
def test_daily_rewritten_on_a_new_basis_marks_the_intraday_file_stale_and_every_read_refuses(sym, new_basis):
    daily, intra = new_basis
    d_path, i_path = _seed_old(sym, new_basis)
    p = _P({"1d": daily, "5min": intra})

    p.force_full_refetch(sym, "1d")                                  # the split-aware top-up's replacement

    marker = split_basis.read_intraday_stale(i_path)
    assert marker is not None, "the defect: daily rewritten, intraday silently left on the old basis"
    assert "constant_factor" in marker["reason"] and "x2" in marker["reason"]
    assert split_basis.read_intraday_stale(d_path) is None           # the daily file itself is never marked
    assert _verdict(sym).klass == cib.KLASS_CONSTANT_FACTOR          # ... and the price check agrees

    before = _digest(i_path)
    # READ: loud
    with pytest.raises(IntradayBasisStale, match=sym):
        p.get_ohlcv_data(sym, start_date=datetime(2023, 6, 1), end_date=datetime(2023, 6, 30), interval="5min")
    # WRITE through the one choke point: refused, file untouched
    with pytest.raises(IntradayBasisStale):
        native_cache.write_timeseries(PROV, sym, "5min", pd.read_parquet(i_path))
    # WRITE through the provider's merge / top-up / extension paths: refused
    with pytest.raises(IntradayBasisError):
        p._write_ohlcv_parquet(intra.tail(78 * 3), PROV, sym, "5min")
    assert _digest(i_path) == before

    # LIVE: the DAILY series of the same symbol keeps working (live reads daily bars)
    got = p.get_ohlcv_data(sym, start_date=datetime(2023, 6, 1), end_date=datetime(2023, 6, 30), interval="1d")
    window = daily[(daily["Date"] >= "2023-06-01") & (daily["Date"] <= "2023-06-30")]
    assert len(got) == len(window) > 15
    assert float(got["Close"].median()) == pytest.approx(float(window["Close"].median()), rel=1e-9)


def test_replacing_the_intraday_file_on_the_vendor_basis_clears_the_marker(sym, new_basis):
    daily, intra = new_basis
    _d, i_path = _seed_old(sym, new_basis)
    p = _P({"1d": daily, "5min": intra})
    p.force_full_refetch(sym, "1d")
    assert split_basis.read_intraday_stale(i_path) is not None
    n_daily = sum(1 for c in p.calls if c[1] == "1d")

    p.force_full_refetch(sym, "5min")

    assert split_basis.read_intraday_stale(i_path) is None
    assert _verdict(sym).klass == cib.KLASS_OK
    assert sum(1 for c in p.calls if c[1] == "1d") == n_daily        # the pair agrees: no second daily fetch
    got = p.get_ohlcv_data(sym, start_date=datetime(2023, 6, 1), end_date=datetime(2023, 6, 30), interval="5min")
    assert len(got) > 1000


def test_a_daily_rewrite_that_leaves_the_levels_alone_marks_nothing(sym, new_basis):
    daily, intra = new_basis
    d_path = _put(sym, "1d", daily)
    i_path = _put(sym, "5min", intra)
    p = _P({"1d": daily, "5min": intra})
    p.force_full_refetch(sym, "1d")                                  # e.g. a repair of a gap: same prices
    assert split_basis.read_intraday_stale(i_path) is None
    assert _verdict(sym).klass == cib.KLASS_OK
    assert d_path


def test_without_a_cross_interval_check_the_safe_direction_is_taken(sym, new_basis):
    daily, intra = new_basis
    _put(sym, "1d", daily)
    i_path = _put(sym, "5min", intra)                                # perfectly consistent
    p = _Blind({"1d": daily, "5min": intra})
    p.force_full_refetch(sym, "1d")
    marker = split_basis.read_intraday_stale(i_path)
    assert marker is not None and "not refetched" in marker["reason"]


# --------------------------------------------------------------------------------------- reverse
def test_intraday_refetched_on_the_new_basis_while_daily_is_old_refetches_the_daily_too(sym, new_basis):
    daily, intra = new_basis
    d_path, i_path = _seed_old(sym, new_basis)                       # both old (x2), consistent
    p = _P({"1d": daily, "5min": intra})                             # the vendor is on the new basis now

    p.force_full_refetch(sym, "5min")                                # only the intraday history is replaced

    assert ("%s" % sym, "1d") in p.calls                             # the stale side (daily) was re-fetched
    assert _verdict(sym).klass == cib.KLASS_OK
    assert split_basis.read_intraday_stale(i_path) is None
    new_daily = pd.read_parquet(d_path)
    assert abs(float(new_daily["Close"].iloc[-1]) - float(daily["Close"].iloc[-1])) < 1e-9


def test_a_top_up_on_the_new_basis_is_refused_before_it_reaches_the_file(sym, new_basis):
    daily, intra = new_basis
    cut = pd.Timestamp("2023-12-20")
    # both files on the OLD basis, consistent: the daily file is current (it is topped up first), the
    # intraday file ends on the 20th. (A tail whose sessions the daily file does not hold yet cannot be
    # compared with it: that case is "unjudged", never refused.)
    old_d, old_i = _by_factor(daily, 2.0), _by_factor(intra[intra["Date"] < cut + pd.Timedelta(days=1)], 2.0)
    _put(sym, "1d", old_d)
    i_path = _put(sym, "5min", old_i)
    before = _digest(i_path)
    # the vendor (new basis) returns the sessions after the 20th
    tail = intra[intra["Date"] >= cut + pd.Timedelta(days=1)]
    p = _P({"1d": daily, "5min": intra})

    # (a) the merge path (cold fill / extension of a window)
    with pytest.raises(IntradayBasisMismatch, match="WRITE REFUSED"):
        p._write_ohlcv_parquet(tail, PROV, sym, "5min")
    assert _digest(i_path) == before
    # (b) the live top-up: NOT swallowed with the vendor errors (the merged frame is never served)
    cached = pd.read_parquet(i_path)
    with pytest.raises(IntradayBasisMismatch):
        p._refresh_parquet_if_stale(cached, sym, "5min", PROV)
    assert _digest(i_path) == before
    # (c) a top-up on the SAME basis as the file is accepted
    same_basis_tail = _by_factor(tail, 2.0)
    p._write_ohlcv_parquet(same_basis_tail, PROV, sym, "5min")
    assert _digest(i_path) != before and len(pd.read_parquet(i_path)) == len(old_i) + len(tail)


def test_an_intraday_file_that_contradicts_the_daily_file_is_not_served(sym, new_basis):
    daily, intra = new_basis
    _put(sym, "1d", daily)
    _put(sym, "5min", _by_factor(intra, 0.5))                        # no marker: found by the price check
    p = _P({"1d": daily, "5min": intra})
    with pytest.raises(IntradayBasisMismatch, match="READ REFUSED"):
        p.get_ohlcv_data(sym, start_date=datetime(2023, 6, 1), end_date=datetime(2023, 6, 30), interval="5min")


def test_the_unfinished_bar_rule_still_holds_through_the_new_guard(sym, new_basis):
    """The guard sits in front of ``write_timeseries``; the never-persist-an-unfinished-bar rule is inside it."""
    daily, intra = new_basis
    _put(sym, "1d", daily)
    _put(sym, "5min", intra)
    now_bar = intra.tail(3).copy()
    now_bar["Date"] = pd.Timestamp.now().floor("5min") + pd.Timedelta(minutes=5)     # a bar that has not ended
    out = now_bar.assign(effective_date=now_bar["Date"])
    assert native_cache.write_timeseries(PROV, sym, "5min", out) is False            # dropped, nothing written


# --------------------------------------------------------------------------------------- review round
def _legacy_defective(sym, new_basis, cut="2023-12-01"):
    """A 5-minute file with a LEGACY x1.325 segment (like T) before ``cut`` and a daily file on the new basis."""
    daily, intra = new_basis
    _put(sym, "1d", daily)
    old = intra.copy()
    early = old["Date"] < pd.Timestamp(cut)
    for c in ("Open", "High", "Low", "Close"):
        old.loc[early, c] = old.loc[early, c] * 1.325
    head = old[old["Date"] < pd.Timestamp("2023-12-20")]
    path = _put(sym, "5min", head)
    return path, intra[intra["Date"] >= pd.Timestamp("2023-12-20")]


def test_M1_a_legacy_defective_file_can_be_extended_by_a_correct_tail(sym, new_basis):
    daily, intra = new_basis
    path, tail = _legacy_defective(sym, new_basis)
    assert _verdict(sym).klass == cib.KLASS_FACTOR_CHANGES                      # the file IS defective
    p = _P({"1d": daily, "5min": intra})
    n0 = len(pd.read_parquet(path))
    p._write_ohlcv_parquet(tail, PROV, sym, "5min")                             # accepted: only the tail is judged
    assert len(pd.read_parquet(path)) == n0 + len(tail)


def test_M1_a_tail_on_the_wrong_basis_is_still_refused(sym, new_basis):
    daily, intra = new_basis
    path, tail = _legacy_defective(sym, new_basis)
    p = _P({"1d": daily, "5min": intra})
    before = _digest(path)
    with pytest.raises(IntradayBasisMismatch, match="incoming bars"):
        p._write_ohlcv_parquet(_by_factor(tail, 2.0), PROV, sym, "5min")
    assert _digest(path) == before
    # ... and so is a SHORT wrong tail (3 sessions: below the whole-history verdict, caught by the tail check)
    short = _by_factor(tail[tail["Date"] < pd.Timestamp("2023-12-26")], 0.4)
    with pytest.raises(IntradayBasisMismatch):
        p._write_ohlcv_parquet(short, PROV, sym, "5min")
    assert _digest(path) == before


def test_M2_a_marker_that_cannot_be_written_never_fails_a_successful_daily_replacement(sym, new_basis, monkeypatch):
    import sys
    M = sys.modules["ba2_common.core.interfaces.MarketDataProviderInterface"]
    daily, intra = new_basis
    d_path, i_path = _seed_old(sym, new_basis)
    p = _P({"1d": daily, "5min": intra})
    errors = []
    monkeypatch.setattr(M.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))

    def boom(*a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(split_basis, "write_intraday_stale", boom)

    out = p.force_full_refetch(sym, "1d")                                       # returns normally: the daily IS replaced
    assert len(out) == len(daily)
    assert abs(float(pd.read_parquet(d_path)["Close"].iloc[-1]) - float(daily["Close"].iloc[-1])) < 1e-9
    assert split_basis.read_intraday_stale(i_path) is None
    assert any("could NOT be written" in e and "PermissionError" in e and "5min" in e and "daily history IS replaced" in e
               for e in errors), errors
    # the price check still refuses the (now contradicted) intraday file at read time
    with pytest.raises(IntradayBasisMismatch):
        p.get_ohlcv_data(sym, start_date=datetime(2023, 6, 1), end_date=datetime(2023, 6, 30), interval="5min")


def test_M2_the_split_top_up_path_does_not_report_a_false_refusal(sym, new_basis, monkeypatch):
    """``_verified_tail_topup`` turns ANY exception of ``force_full_refetch`` into 'top-up REFUSED, cache left
    untouched' and memoises it: after a successful replacement that would be false."""
    daily, intra = new_basis
    _seed_old(sym, new_basis)
    p = _P({"1d": daily, "5min": intra})
    monkeypatch.setattr(split_basis, "write_intraday_stale", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    p.force_full_refetch(sym, "1d")          # would raise OSError before the fix
    from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface as MDP
    assert (PROV, sym.upper(), "1d") not in MDP._TOPUP_REFUSED


def test_M3_the_vendor_serving_two_bases_writes_nothing_and_says_so(sym, new_basis):
    daily, intra = new_basis
    # the cache: daily current (new basis), intraday the old as-traded history (x2); the vendor serves the daily
    # endpoint on the NEW basis and the intraday endpoint AS-TRADED (x2) for old dates: they disagree with each other
    d_path = _put(sym, "1d", daily)
    i_path = _put(sym, "5min", _by_factor(intra, 2.0))
    vendor_intra = _by_factor(intra, 2.0)
    p = _P({"1d": daily, "5min": vendor_intra})
    before_i, before_d = _digest(i_path), _digest(d_path)
    with pytest.raises(split_basis.IntradayVendorBasisConflict, match="vendor's intraday basis differs from its daily basis"):
        p.force_full_refetch(sym, "5min")
    assert _digest(i_path) == before_i and _digest(d_path) == before_d         # NOTHING written
    assert split_basis.read_intraday_stale(i_path) is None                      # unmarked stays unmarked
    # a MARKED file stays marked, byte for byte
    split_basis.write_intraday_stale(i_path, reason="earlier marker")
    with pytest.raises(split_basis.IntradayVendorBasisConflict):
        p.force_full_refetch(sym, "5min")
    assert split_basis.read_intraday_stale(i_path)["reason"] == "earlier marker" and _digest(i_path) == before_i


def test_M6_a_marker_does_not_outlive_its_file(sym, new_basis):
    daily, intra = new_basis
    d_path, i_path = _seed_old(sym, new_basis)
    p = _P({"1d": daily, "5min": intra})
    p.force_full_refetch(sym, "1d")
    assert split_basis.read_intraday_stale(i_path) is not None
    import os
    os.remove(i_path)                                                           # deleted for a cold refill
    p._write_ohlcv_parquet(intra.head(78 * 12), PROV, sym, "5min")             # cold fill: must not raise IntradayBasisStale
    assert os.path.exists(i_path) and split_basis.read_intraday_stale(i_path) is None


def test_the_replacement_of_a_rebased_file_by_vendor_data_clears_its_provenance(sym, new_basis):
    daily, intra = new_basis
    _put(sym, "1d", daily)
    i_path = _put(sym, "5min", intra)
    split_basis.write_intraday_rebase(i_path, {"kind": "rebased_by_tool", "segments": []})
    p = _P({"1d": daily, "5min": intra})
    p.force_full_refetch(sym, "5min")
    assert split_basis.read_intraday_rebase(i_path) is None
