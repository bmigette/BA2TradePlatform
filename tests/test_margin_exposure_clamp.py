"""The expert's available balance is capped by the ACCOUNT's remaining stock-exposure
headroom (2026-09-09 review, finding 2).

The expert charges an open position to itself at ENTRY COST while the broker marks it
to MARKET. On a winner the two diverge in the dangerous direction: at equity $11,800
with 180 shares bought at $100 and now worth $110, the expert's own books say
$21,240 - $18,000 = $3,240 is free while the account is already $19,800 deep against a
$21,240 ceiling -- $1,440. The clamp closes that gap without touching the (deliberately
unchanged, backtest-affecting) cost-basis accounting.

The clamp is reachable only with margin ON: the ACCOUNT answers None with margin off,
and does so without a broker round trip -- pinned here with a snapshot counter, because
"backtests are unchanged" has to be true by construction.
"""
import logging

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderDirection, TransactionStatus

from tests import factories


class _Account:
    """Only what get_virtual_balance / get_available_balance read, plus the new
    exposure ceiling. ``margin_enabled`` gates the ceiling exactly as the real
    ReadOnlyAccountInterface does: off -> None, and the snapshot is never read."""

    def __init__(self, id_val, *, balance, tradable, buying_power, price,
                 margin_enabled, gross=0.0, pending=0.0, raises=None):
        self.id = id_val
        self._balance, self._tradable, self._bp = balance, tradable, buying_power
        self._price = price
        self._margin_enabled = margin_enabled
        self._gross, self._pending = gross, pending
        self._raises = raises
        self.snapshot_calls = 0
        self.headroom_calls = 0

    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        return self._tradable

    def get_option_tradable_balance(self):
        return self._balance

    def get_account_info(self):
        return {"buying_power": self._bp}

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        if isinstance(symbol_or_list, (list, tuple, set)):
            return {s: self._price for s in symbol_or_list}
        return self._price

    def get_stock_exposure_headroom(self, exclude_order_id=None):
        self.headroom_calls += 1
        if not self._margin_enabled:
            return None
        if self._raises is not None:
            raise self._raises
        self.snapshot_calls += 1          # the broker round trip the margin path costs
        return self._tradable - self._gross - self._pending


class _Expert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "margin exposure clamp test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _with_account(account, fn):
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    class _R:
        def get_account_instance(self, account_id):
            return account

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_R())
        return fn()
    finally:
        set_instance_resolver(prev)


@pytest.fixture
def errors(monkeypatch):
    """DEBUG/INFO/ERROR lines from MarketExpertInterface. Not caplog: ba2_common's logger
    sets propagate=False, so caplog would see nothing and pass vacuously."""
    import sys

    module = sys.modules["ba2_common.core.interfaces.MarketExpertInterface"]
    seen = []
    for name, level in (("debug", logging.DEBUG), ("info", logging.INFO),
                        ("error", logging.ERROR)):
        monkeypatch.setattr(
            module.logger, name,
            lambda msg, *a, _lvl=level, **k: seen.append((_lvl, str(msg))))
    return seen


def _finding_2_expert(margin_enabled, **account_kw):
    """equity 11,800 x 1.8 = 21,240 tradable; one OPEN 180-share winner bought at $100,
    now $110 -> the broker sees 19,800 of exposure, the expert's books see 18,000 used."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)
    factories.create_transaction(
        symbol="AAPL", quantity=180.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=100.0, expert_id=inst.id)
    kw = dict(balance=11_800.0, tradable=21_240.0, buying_power=50_000.0, price=110.0,
              margin_enabled=margin_enabled, gross=19_800.0)
    kw.update(account_kw)
    return _Account(acct_def.id, **kw), _Expert(inst.id)


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_is_clamped_to_the_account_headroom(errors):
    account, expert = _finding_2_expert(margin_enabled=True)
    assert _with_account(account, expert.get_available_balance) == pytest.approx(1_440.0)
    # DEBUG, not INFO: the clamp is the ceiling working normally and fires on every read
    # (353 times in one production session). The account reports the abnormal case.
    assert any(lvl == logging.DEBUG and "stock exposure" in msg for lvl, msg in errors), errors
    assert not any(lvl >= logging.INFO and "stock exposure" in msg for lvl, msg in errors)


@pytest.mark.usefixtures("reset_test_db")
def test_the_unclamped_figure_is_the_overstatement_the_review_measured():
    """The pin that stops the test above passing for the wrong reason: without the
    ceiling the expert really does report $3,240."""
    account, expert = _finding_2_expert(margin_enabled=False)
    assert _with_account(account, expert.get_available_balance) == pytest.approx(3_240.0)


@pytest.mark.usefixtures("reset_test_db")
def test_margin_off_costs_no_broker_round_trip():
    account, expert = _finding_2_expert(margin_enabled=False)
    _with_account(account, expert.get_available_balance)
    assert account.headroom_calls == 1, "the expert always asks"
    assert account.snapshot_calls == 0, "with margin off the account answers without reading"


@pytest.mark.usefixtures("reset_test_db")
def test_headroom_above_the_expert_figure_changes_nothing():
    account, expert = _finding_2_expert(margin_enabled=True, gross=0.0)
    assert _with_account(account, expert.get_available_balance) == pytest.approx(3_240.0)


@pytest.mark.usefixtures("reset_test_db")
def test_an_unreadable_exposure_is_a_refusal_not_a_number(errors):
    account, expert = _finding_2_expert(
        margin_enabled=True,
        raises=ValueError("account 7 published no long/short market value"))
    assert _with_account(account, expert.get_available_balance) is None
    assert any(lvl == logging.ERROR for lvl, _ in errors), errors
