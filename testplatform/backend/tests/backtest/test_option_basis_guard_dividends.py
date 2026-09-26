"""The run's dividend source for the E4 guard's carry separation (2026-09-26).

``load_cached_dividend_calendar`` reads CACHE_FOLDER/fmp_history/mc_stock_dividend__<SYM>.json
(the payload ``tools/warm_dividend_calendars.py`` caches; a backtest never fetches) and
``options_store._dividends_of(split_basis)`` serves DividendRow(ex-date, amount, declared) in the
SESSION's as-traded basis: FMP's ``adjDividend`` is split-adjusted like its closes, so it is multiplied by the run's
as-traded factor of the session, exactly as the spot is. An absent calendar is None -- the
guard then says "dividend calendar unavailable" in any refusal, it never assumes "no dividend".
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    os.makedirs(tmp_path / "fmp_history")
    return tmp_path


def _write(cache, sym, rows):
    with open(cache / "fmp_history" / f"mc_stock_dividend__{sym}.json", "w") as f:
        json.dump({"symbol": sym, "historical": rows}, f)


def test_the_namespace_is_the_warmup_sources_own():
    from ba2_providers.market_conditions.fmp_source import DIVIDEND_CALENDAR_NAMESPACE
    from app.services.backtest.option_split_basis import dividend_calendar_path
    assert DIVIDEND_CALENDAR_NAMESPACE == "mc_stock_dividend"
    assert dividend_calendar_path("stla").endswith(os.path.join("fmp_history",
                                                                "mc_stock_dividend__STLA.json"))


def test_the_loader_reads_adj_dividends_with_declaration_dates_and_answers_none_when_absent(cache):
    from app.services.backtest.option_split_basis import load_cached_dividend_calendar
    assert load_cached_dividend_calendar("STLA") is None
    _write(cache, "STLA", [{"date": "2022-04-19", "dividend": 1.04, "adjDividend": 1.1221,
                            "declarationDate": "2022-02-25"},
                           {"date": "2021-04-19", "dividend": 0.3849, "adjDividend": 0.3849,
                            "declarationDate": ""},
                           {"date": "2020-01-02", "dividend": 0.5, "adjDividend": None,
                            "declarationDate": "2019-12-01"}])
    assert load_cached_dividend_calendar("STLA") == [
        (date(2020, 1, 2), None, date(2019, 12, 1)),        # NO fallback to the raw dividend
        (date(2021, 4, 19), 0.3849, None),                   # unknown declaration kept as None
        (date(2022, 4, 19), 1.1221, date(2022, 2, 25))]


def test_no_raw_dividend_is_ever_scaled_by_a_split_factor(cache):
    """AMC's calendar: dividend 0.3529 (as declared) vs adjDividend 1.76471 (split-adjusted like
    the closes). Only the adjusted one may be multiplied by the as-traded factor."""
    from app.services.backtest.options_store import _dividends_of
    _write(cache, "AMC", [{"date": "2019-12-02", "dividend": 0.3529, "adjDividend": None,
                           "declarationDate": "2019-10-25"},
                          {"date": "2019-09-06", "dividend": 0.3529, "adjDividend": 1.76471,
                           "declarationDate": "2019-08-02"}])
    src = _dividends_of(_Basis(lambda s, d: 0.1))
    got = src("AMC", date(2019, 8, 15))
    assert [(r.ex_date, r.amount) for r in got] == [(date(2019, 9, 6), pytest.approx(0.176471)),
                                                    (date(2019, 12, 2), None)]


class _Basis:
    def __init__(self, factor):
        self._f = factor

    def factor(self, symbol, day):
        return self._f(symbol, day)


def test_dividends_are_served_in_the_sessions_as_traded_basis_with_declarations(cache):
    from ba2_common.core.option_carry import DividendRow
    from app.services.backtest.options_store import _dividends_of
    _write(cache, "X", [{"date": "2024-06-03", "adjDividend": 0.5, "declarationDate": "2024-05-02"},
                        {"date": "2023-06-01", "adjDividend": 0.4, "declarationDate": "2023-05-01"}])
    # a 4-for-1 split on 2024-07-01: before it, the as-traded price is 4x the adjusted one
    src = _dividends_of(_Basis(lambda s, d: 4.0 if d < date(2024, 7, 1) else 1.0))
    assert src("X", date(2024, 5, 1)) == [DividendRow(date(2024, 6, 3), 2.0, date(2024, 5, 2))]
    assert src("X", date(2024, 8, 1)) == []
    assert src("Y", date(2024, 5, 1)) is None                          # no calendar cached


def test_the_run_wires_the_dividend_source_into_the_guard(cache, tmp_path, monkeypatch):
    from app.services.backtest import options_store as S
    captured = {}

    class _Provider:
        def __init__(self, root, **kw):
            captured.update(kw)

    monkeypatch.setattr("app.services.backtest.parquet_options_provider.ParquetOptionsProvider",
                        _Provider)
    monkeypatch.setattr(S, "resolve_options_store", lambda cfg: "thetadata")
    monkeypatch.setattr(S, "price_source_spot", lambda ps, sb: (lambda s, d: 1.0))
    monkeypatch.setattr(S, "spot_scope", lambda cfg, sb: "scope")
    basis = type("B", (), {"basis_of": lambda self, s: None,
                           "factor": lambda self, s, d: 1.0})()
    S.build_options_provider({"options_cache_db": "x", "options_parquet_root": str(tmp_path),
                              "options_risk_free_rate": 0.04}, price_source=None,
                             split_basis=basis)
    assert callable(captured["basis_guard_dividends"])
    assert captured["basis_guard"] is True


# ---- the contract-detail view uses the run's own guard wiring (review round 2, I4) ------------
def test_a_fresh_guard_carries_the_runs_split_dates_and_dividends(tmp_path):
    from app.services.backtest.parquet_options_provider import ParquetOptionsProvider
    sd = lambda s: ()
    dv = lambda s, d: []
    p = ParquetOptionsProvider(str(tmp_path), spot_source=lambda s, d: 1.0, risk_free_rate=0.04,
                               spot_scope="s", basis_guard=True, basis_guard_split_dates=sd,
                               basis_guard_dividends=dv)
    g = p.new_basis_guard()
    assert g is not p._basis_guard and g.split_dates is sd and g.dividends is dv
    assert g.spot_source is p.spot_source


def test_the_contract_detail_view_builds_its_guard_from_the_provider(monkeypatch):
    """It used to build ``BasisGuard(spot_source)`` with no dividend source, so it refused STLA
    bars the run itself had passed and blamed 'the caches changed'."""
    import numpy as np
    import app.services.parquet_contract_detail as cd

    made = []

    class _Guard:
        unevaluable = 0

        def check(self, u, underlying, day):
            made.append((underlying, day))

    class _Provider:
        spot_source = staticmethod(lambda s, d: 1.0)
        risk_free_rate = 0.04

        def new_basis_guard(self):
            return _Guard()

    class _Raw:
        date_of_ord = {date(2022, 4, 12).toordinal(): date(2022, 4, 12)}

    class _U:
        raw = _Raw()
        c_index = {"STLA220520C00015000": 0}
        bar_ord_l = [date(2022, 4, 12).toordinal()]

        def latest_row_on_or_before(self, ci, o):
            return 0

        def bar_dict(self, i, ci, spot):
            return {"date": "2022-04-12"}

    reader = cd.ParquetContractReader.__new__(cd.ParquetContractReader)
    reader.inputs = type("I", (), {"as_traded": True, "store": "thetadata",
                                   "risk_free_rate": 0.04})()
    reader._contexts = {"STLA": cd._UnderlyingContext(provider=_Provider(), overlay=_U())}
    monkeypatch.setattr(reader, "_notes", lambda ctx, d, checked: [])
    row = reader.latest_bar_on_or_before("STLA220520C00015000", "2022-04-12")
    assert row["date"] == "2022-04-12" and made == [("STLA", date(2022, 4, 12))]
