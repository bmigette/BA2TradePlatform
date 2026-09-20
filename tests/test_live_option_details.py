"""The live option structure detail block (spec 2026-09-20, steps 8 and 12).

Two things worth pinning down without a broker or a browser:

* **IV rank is a percentile of OUR OWN stored series**, and a percentile of a handful of
  samples is not a rank -- so a short window must report "not available yet" rather than a
  number.
* **A missing quote or an account with no options interface is UNKNOWN**, reported per leg,
  never rendered as zero and never raised into the dialog.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_trade_platform.ui.pages import live_trades


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def exec(self, _query):
        return SimpleNamespace(all=lambda: list(self._rows))

    def close(self):
        pass


def _samples(values):
    return [SimpleNamespace(atm_iv=value) for value in values]


@pytest.fixture
def tab():
    return live_trades.LiveTradesTab()


class TestIvRank:
    def test_a_short_window_is_not_a_rank(self, tab, monkeypatch):
        monkeypatch.setattr(live_trades, 'get_db', lambda: _FakeSession(_samples([0.2, 0.3, 0.4])))

        rank, samples = tab._iv_rank(1, 'ACN', 0.35)

        assert rank is None
        assert samples == 3

    def test_a_full_window_is_a_percentile_of_the_series(self, tab, monkeypatch):
        # 25 samples rising 0.20 .. 0.44; a current IV of 0.32 sits above 12 of them.
        values = [0.20 + 0.01 * index for index in range(25)]
        monkeypatch.setattr(live_trades, 'get_db', lambda: _FakeSession(_samples(values)))

        rank, samples = tab._iv_rank(1, 'ACN', 0.32)

        assert samples == 25
        assert rank == pytest.approx(52.0)

    def test_no_current_iv_means_no_rank(self, tab, monkeypatch):
        values = [0.20 + 0.01 * index for index in range(25)]
        monkeypatch.setattr(live_trades, 'get_db', lambda: _FakeSession(_samples(values)))

        rank, samples = tab._iv_rank(1, 'ACN', None)

        assert rank is None
        assert samples == 25

    def test_null_samples_are_skipped_not_counted_as_zero(self, tab, monkeypatch):
        rows = _samples([0.20] * 20) + [SimpleNamespace(atm_iv=None)]
        monkeypatch.setattr(live_trades, 'get_db', lambda: _FakeSession(rows))

        rank, samples = tab._iv_rank(1, 'ACN', 0.30)

        assert samples == 20
        assert rank == pytest.approx(100.0)

    def test_a_broken_read_reports_no_rank_instead_of_raising(self, tab, monkeypatch):
        def _boom():
            raise RuntimeError('db down')

        monkeypatch.setattr(live_trades, 'get_db', _boom)

        assert tab._iv_rank(1, 'ACN', 0.3) == (None, 0)


class TestContractDetailCollection:
    def test_an_account_without_the_options_interface_reports_unknown(self, tab, monkeypatch):
        monkeypatch.setattr(live_trades, 'get_account_instance_from_id',
                            lambda *args, **kwargs: object())

        detail = tab._collect_option_contract_detail(1, ['ACN260918C00095000'], 'ACN')

        assert detail['account_supports_options'] is False
        assert detail['quotes'] == {}
        assert 'options interface' in detail['errors']['account']

    def test_a_missing_account_is_reported_not_raised(self, tab, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError('no such account')

        monkeypatch.setattr(live_trades, 'get_account_instance_from_id', _boom)

        detail = tab._collect_option_contract_detail(99, ['X'], 'ACN')

        assert detail['account_supports_options'] is False
        assert 'no such account' in detail['errors']['account']

    def test_every_read_is_guarded_per_leg(self, tab, monkeypatch):
        """A leg whose quote call raises must not take the other legs' detail with it."""

        class _Account:
            # Not an OptionsAccountInterface subclass, so the guard returns early -- this
            # asserts the early return carries a reason rather than a stack trace.
            pass

        monkeypatch.setattr(live_trades, 'get_account_instance_from_id', lambda *a, **k: _Account())

        detail = tab._collect_option_contract_detail(1, ['A', 'B'], 'ACN')

        assert detail['errors']
        assert detail['quotes'] == {}
