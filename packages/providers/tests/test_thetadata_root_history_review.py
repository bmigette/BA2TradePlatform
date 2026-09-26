"""ROOT_HISTORY review (2026-09-26): import-time validation, a LOUD empty alias segment, and
discovery that respects the end of an old root's segment.

* ``_validate_root_history`` refuses cutovers out of order, a root that is neither a str nor
  None, and a root equal to its own symbol -- at import, so a bad table never reaches a fetch.
* A wide fetch whose OLD-root segment of >= ``ALIAS_EMPTY_MIN_DAYS`` returns no row at all
  raises ``RootHistorySegmentEmpty`` (a mistyped or wrong old root used to be skipped silently:
  NoDataFound is also the answer for a quiet window).
* ``discover_contracts`` keeps an expiry AFTER an old root's segment only for the strikes that
  root actually quoted before the cutover (BN must not list the new BAM's 2024 expiries).
A fake client answers per root; no network.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from ba2_providers.options import thetadata as T
from ba2_providers.options.thetadata import ThetaDataOptionsProvider, _occ_symbol


def _in_this_checkout(module_file) -> bool:
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[2]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False


def test_the_provider_under_test_is_the_worktree_copy():
    assert _in_this_checkout(T.__file__), T.__file__


# ---- import-time validation ------------------------------------------------------------------
def test_the_shipped_table_validates():
    T._validate_root_history(T.ROOT_HISTORY)


@pytest.mark.parametrize("table,match", [
    ({"X": ((date(2022, 1, 3), "A"), (date(2021, 1, 4), "B"))}, "order"),
    ({"X": ((date(2022, 1, 3), "A"), (date(2022, 1, 3), "B"))}, "order"),
    ({"X": ((date(2022, 1, 3), 7),)}, "str or None"),
    ({"X": ((date(2022, 1, 3), ""),)}, "str or None"),
    ({"X": ((date(2022, 1, 3), "X"),)}, "its own symbol"),
    ({"X": (("2022-01-03", "A"),)}, "date"),
])
def test_a_bad_table_is_refused(table, match):
    with pytest.raises(ValueError, match=match):
        T._validate_root_history(table)


# ---- a LOUD empty alias segment ---------------------------------------------------------------
class _NoData(Exception):
    pass


CUT = date(2022, 12, 12)
EXP = date(2023, 1, 20)
LATE = date(2024, 1, 19)          # a new-company expiry the old root lists too


def _days(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class _Client:
    """``alive``: {root: (first, last, strike)} -- the root quotes EXP at ``strike`` on
    business days in [first, last]; LATE only from the cutover under the new root."""

    def __init__(self, alive):
        self.alive = alive
        self.calls = []

    def _rows(self, symbol, a, b, date_col, expiration="*"):
        rows = []
        if symbol in self.alive:
            first, last, k = self.alive[symbol]
            for d in _days(max(a, first), min(b, last)):
                for exp in (EXP, LATE):
                    if expiration not in ("*", exp):
                        continue
                    if exp == LATE and d < CUT:
                        continue
                    for right in ("CALL", "PUT"):
                        rows.append({"expiration": exp.isoformat(), "strike": k, "right": right,
                                     date_col: d.isoformat(), "open": 1.0, "high": 1.2, "low": 0.9,
                                     "close": 1.1, "volume": 10, "bid": 1.0, "ask": 1.2,
                                     "implied_vol": 0.3})
        if not rows:
            raise _NoData(symbol)
        return pd.DataFrame(rows)

    def option_history_eod(self, **kw):
        self.calls.append(("history_eod", kw["symbol"], kw["start_date"], kw["end_date"]))
        return self._rows(kw["symbol"], kw["start_date"], kw["end_date"], "created")

    def option_history_greeks_eod(self, **kw):
        self.calls.append(("greeks_eod", kw["symbol"], kw["start_date"], kw["end_date"]))
        return self._rows(kw["symbol"], kw["start_date"], kw["end_date"], "timestamp",
                          kw.get("expiration", "*"))

    def option_history_open_interest(self, **kw):
        df = self._rows(kw["symbol"], kw["start_date"], kw["end_date"], "timestamp")
        df["open_interest"] = 5
        return df

    def option_list_dates(self, **kw):
        raise _NoData("no dates")

    def option_list_expirations(self, symbol):
        self.calls.append(("list_expirations", symbol, None, None))
        return pd.DataFrame({"expiration": [EXP.isoformat(), LATE.isoformat()]})

    def option_list_strikes(self, symbol, expiration):
        return pd.DataFrame({"strike": [30.0, 35.0, 45.0]})


def _wired(client):
    p = ThetaDataOptionsProvider(api_key="test-key")
    p._client = client
    p._no_data_exc = _NoData
    return p


def test_an_alias_segment_that_returns_nothing_raises_loudly():
    # BN <- BAM, but the BAM root answers nothing before the cutover (a wrong old root)
    client = _Client({"BN": (CUT, date(2023, 1, 20), 35.0)})
    with pytest.raises(T.RootHistorySegmentEmpty, match=r"BN.*BAM.*2022-10-03"):
        list(_wired(client).fetch_underlying_eod_bars("BN", start=date(2022, 10, 3),
                                                      end=date(2022, 12, 30)))


def test_a_short_alias_segment_may_be_empty():
    client = _Client({"BN": (CUT, date(2023, 1, 20), 35.0)})
    bars = list(_wired(client).fetch_underlying_eod_bars("BN", start=date(2022, 12, 1),
                                                         end=date(2022, 12, 30)))
    assert bars and min(b.bar_date for b in bars) == CUT


def test_an_alias_segment_with_rows_passes():
    client = _Client({"BAM": (date(2022, 1, 3), date(2022, 12, 9), 45.0),
                      "BN": (CUT, date(2023, 1, 20), 35.0)})
    bars = list(_wired(client).fetch_underlying_eod_bars("BN", start=date(2022, 10, 3),
                                                         end=date(2022, 12, 30)))
    assert {b.bar_date < CUT for b in bars} == {True, False}


def test_a_none_root_segment_is_never_an_empty_alias():
    client = _Client({"BAM": (CUT, date(2023, 1, 20), 30.0)})
    bars = list(_wired(client).fetch_underlying_eod_bars("BAM", start=date(2022, 6, 1),
                                                         end=date(2022, 12, 30)))
    assert bars and min(b.bar_date for b in bars) == CUT


# ---- discovery respects the end of an old root's segment --------------------------------------
def test_bn_discovery_does_not_list_the_new_bam_expiries_under_the_bam_root():
    client = _Client({"BAM": (date(2022, 1, 3), date(2025, 12, 31), 45.0),
                      "BN": (CUT, date(2025, 12, 31), 35.0)})
    client.alive["BAM"] = (date(2022, 1, 3), date(2022, 12, 9), 45.0)
    out = _wired(client).discover_contracts("BN", expiry_gte=date(2022, 11, 1),
                                            expiry_lte=date(2024, 12, 31))
    by = {}
    for c in out:
        by.setdefault(c.expiry, set()).add(c.strike)
    # EXP spans the cutover: the old company's 45 (quoted under BAM before it) + BN's own 35
    assert by[EXP] == {45.0, 35.0}
    # LATE is the new BAM's: under BAM it has no row before the cutover -> only BN's strikes
    assert by[LATE] == {35.0}
    assert all(c.occ_symbol.startswith("BN") for c in out)


# ---- review round 2 ---------------------------------------------------------------------------
@pytest.mark.parametrize("table,match", [
    ({"x": ((date(2022, 1, 3), "A"),)}, "upper-case"),
    ({" X": ((date(2022, 1, 3), "A"),)}, "upper-case"),
    ({"X": ((date(2022, 1, 3), " A"),)}, "surrounding spaces"),
    ({"X": ((date(2022, 1, 3), "a"),)}, "upper-case"),
    ({"X": ((date(2022, 1, 3), "x"),)}, "upper-case"),
    ({"X": ((__import__("datetime").datetime(2022, 1, 3), "A"),)}, "date, not a datetime"),
])
def test_round_2_table_rules(table, match):
    with pytest.raises(ValueError, match=match):
        T._validate_root_history(table)


def test_the_source_literal_has_no_duplicate_keys():
    """A dict literal silently keeps the LAST of two equal keys; read the source instead."""
    import ast
    tree = ast.parse(open(T.__file__, encoding="utf-8").read())
    lit = next(n.value for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
               and getattr(getattr(n, "target", None) or (n.targets[0] if getattr(n, "targets", None) else None), "id", None) == "ROOT_HISTORY")
    keys = [k.value for k in lit.keys]
    assert len(keys) == len(set(keys)) == len(T.ROOT_HISTORY)
    assert T._duplicate_root_history_keys(T.__file__) == []


class _Boom(Exception):
    pass


def test_a_network_error_in_an_alias_segment_is_not_masked():
    """The empty-alias check ran in a ``finally``: a network error inside the old root's last
    window was replaced by RootHistorySegmentEmpty. It now only runs after a window completes
    normally, so the real error propagates."""
    class _Failing(_Client):
        def option_history_eod(self, **kw):
            if kw["symbol"] == "BAM" and kw["end_date"] == CUT - timedelta(days=1):
                raise _Boom("connection reset")          # the segment's LAST window
            return super().option_history_eod(**kw)

    client = _Failing({"BN": (CUT, date(2023, 1, 20), 35.0)})
    with pytest.raises(_Boom):
        list(_wired(client).fetch_underlying_eod_bars("BN", start=date(2022, 10, 3),
                                                      end=date(2022, 12, 30)))
