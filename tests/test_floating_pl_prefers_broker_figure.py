"""The Floating P/L card takes the BROKER's own open P/L when the broker publishes one.

A manual account's row used to be the sum of each position's ``unrealized_pl``. For
TastyTrade that figure has to be derived from ``mark_price`` -- the bid/ask MIDPOINT -- and on
a book of thin ETFs the midpoint is not where the broker marks. Measured 2026-09-17 on the
live account: $66.91 away from the broker's screen across 75 positions, against a cost basis
that agreed to the cent, with one untraded name (quoted 22.55/37.99) contributing $18.96 of it.

``get_broker_floating_pl()`` defaults to ``None``, so every broker that does publish a usable
per-position figure keeps the old path. These pin that the card asks first, uses the answer
when there is one, and falls back -- rather than blanking or zeroing -- when there is not.
"""
from types import SimpleNamespace

import pytest

import sys

from ba2_trade_platform.ui.components.FloatingPLPerAccountWidget import (
    FloatingPLPerAccountWidget,
)

# The MODULE, via sys.modules. The package re-exports the CLASS under the module's own
# name, so both `from ...components import FloatingPLPerAccountWidget` and
# `import ...components.FloatingPLPerAccountWidget as m` hand back the class instead.
mod = sys.modules[FloatingPLPerAccountWidget.__module__]


class _Account:
    """An account whose broker-level answer and per-position list are both dialable."""

    def __init__(self, broker_pl, positions, *, raises=False):
        self._broker_pl = broker_pl
        self._positions = positions
        self._raises = raises
        self.broker_pl_calls = 0
        self.positions_calls = 0

    def get_broker_floating_pl(self):
        self.broker_pl_calls += 1
        if self._raises:
            raise RuntimeError("broker 503")
        return self._broker_pl

    def get_positions(self):
        self.positions_calls += 1
        return self._positions

    def get_balance(self):
        return 3718.08


@pytest.fixture
def widget(monkeypatch):
    w = object.__new__(FloatingPLPerAccountWidget)
    # The money columns are a separate concern with their own tests; hold them still so
    # these assert only on which P/L SOURCE won.
    monkeypatch.setattr(FloatingPLPerAccountWidget, "_read_money",
                        lambda self, account, account_id: (3718.08, None, 262.97))
    return w


def _row(widget, monkeypatch, account):
    monkeypatch.setattr(mod, "get_account_instance_from_id",
                        lambda account_id, session=None: account)
    rows = widget._rows_for_manual_account(2, [], "TastyTrade", session=None)
    assert len(rows) == 1
    return rows[0], account


#: Two positions whose mid-quote P/L (+16.66 - 2.37) is nothing like the broker's.
_MISMARKED = [
    {"symbol": "CAS", "unrealized_pl": 16.66},
    {"symbol": "PSQL", "unrealized_pl": -2.37},
]


def test_the_brokers_figure_wins_over_the_per_position_sum(widget, monkeypatch):
    row, account = _row(widget, monkeypatch, _Account(-155.55, _MISMARKED))
    assert row.pl == pytest.approx(-155.55)
    assert row.pl != pytest.approx(16.66 - 2.37), "the mid-quote sum must not be what shows"
    assert account.broker_pl_calls == 1


def test_the_positions_are_not_even_fetched_when_the_broker_answers(widget, monkeypatch):
    """A saved round trip, and no chance of the two sources disagreeing in one row."""
    _, account = _row(widget, monkeypatch, _Account(-155.55, _MISMARKED))
    assert account.positions_calls == 0


def test_a_measured_zero_from_the_broker_is_used_not_treated_as_missing(widget, monkeypatch):
    row, _ = _row(widget, monkeypatch, _Account(0.0, _MISMARKED))
    assert row.pl == pytest.approx(0.0)


def test_none_falls_back_to_the_per_position_sum(widget, monkeypatch):
    """Every other broker, unchanged: the default hook answers None."""
    row, account = _row(widget, monkeypatch, _Account(None, _MISMARKED))
    assert row.pl == pytest.approx(16.66 - 2.37)
    assert account.positions_calls == 1


def test_a_raising_hook_falls_back_rather_than_losing_the_row(widget, monkeypatch):
    row, account = _row(widget, monkeypatch, _Account(None, _MISMARKED, raises=True))
    assert row.pl == pytest.approx(16.66 - 2.37)
    assert account.positions_calls == 1


def test_the_money_columns_survive_the_broker_path(widget, monkeypatch):
    """The row still carries balance/BP -- the card draws them from the same row."""
    row, _ = _row(widget, monkeypatch, _Account(-155.55, _MISMARKED))
    assert row.balance == pytest.approx(3718.08)
    assert row.broker_bp == pytest.approx(262.97)


def test_a_failed_position_fetch_is_still_unknown_when_the_broker_is_silent(widget, monkeypatch):
    """The tri-state contract is untouched: None positions means unknown, not zero."""
    row, _ = _row(widget, monkeypatch, _Account(None, None))
    assert row.pl is None
