"""The trade popup charts the underlying AS TRADED, the basis option strikes are on.

Reported 2026-09-24 on NVDA230616C00305000: the FMP cache is split-adjusted, so May-2023 NVDA
charted at $28-42 under a $305 strike, the whole chart read as the loss zone and the leg table
called a deep in-the-money exit (as traded $389.50) OTM. The factor is the run's own verified
split basis; when it cannot be verified the bars stay as they are and a notice says why.
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core.split_basis import SplitBasisRefused

import app.services.backtest.option_split_basis as option_split_basis
from app.services.backtest_trade_chart import as_traded_bars


def _bar(day, close):
    return {"date": day, "open": close, "high": close, "low": close, "close": close}


class _TenForOneBefore:
    """A basis with a 10:1 split effective 2024-06-10: earlier days multiply by 10."""

    def factor(self, symbol, day):
        return 10.0 if day < date(2024, 6, 10) else 1.0


def test_pre_split_bars_are_multiplied_into_the_strikes_basis(monkeypatch):
    monkeypatch.setattr(option_split_basis, "build_run_split_basis",
                        lambda symbols, provider, interval: _TenForOneBefore())
    bars = [_bar("2023-05-02", 28.21), _bar("2023-05-26", 38.95)]

    converted, refusal, changed = as_traded_bars(bars, "FMPOHLCVProvider", "NVDA")

    assert refusal is None and changed is True
    assert [round(bar["close"], 2) for bar in converted] == [282.1, 389.5]
    assert bars[0]["close"] == 28.21          # the input is not mutated


def test_a_window_after_every_split_is_left_as_is_and_says_nothing_changed(monkeypatch):
    monkeypatch.setattr(option_split_basis, "build_run_split_basis",
                        lambda symbols, provider, interval: _TenForOneBefore())
    bars = [_bar("2025-12-16", 232.51)]

    converted, refusal, changed = as_traded_bars(bars, "FMPOHLCVProvider", "MU")

    assert refusal is None and changed is False
    assert converted[0]["close"] == 232.51


def test_an_unverifiable_basis_leaves_the_bars_unconverted_with_the_reason(monkeypatch):
    def refuse(symbols, provider, interval):
        raise SplitBasisRefused("NVDA: no cached split calendar")

    monkeypatch.setattr(option_split_basis, "build_run_split_basis", refuse)
    bars = [_bar("2023-05-02", 28.21)]

    converted, refusal, changed = as_traded_bars(bars, "FMPOHLCVProvider", "NVDA")

    # Never an assumed factor of 1 presented as as-traded: the caller turns this into a notice.
    assert converted == bars and changed is False
    assert "no cached split calendar" in refusal


@pytest.mark.parametrize("provider, symbol", [(None, "NVDA"), ("FMPOHLCVProvider", None)])
def test_nothing_to_convert_without_a_source(provider, symbol):
    bars = [_bar("2023-05-02", 28.21)]
    assert as_traded_bars(bars, provider, symbol) == (bars, None, False)
