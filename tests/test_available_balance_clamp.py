"""Regression: MarketExpertInterface.get_available_balance() must clamp the per-expert
virtual-equity figure to the account's ACTUAL available balance (2026-07-21 fix).

Without this, an expert's own virtual-equity bookkeeping has no visibility into (a) other
experts on the SAME account oversubscribing their own virtual slices (virtual_equity_pct is
allowed to sum past 100% across an account's experts), or (b) a manual trade placed outside any
expert's tracking -- both silently consume REAL account cash the expert's own math never learns
about, letting it believe it can afford more than the account actually has.
"""
import contextlib
import logging

import pytest
from types import SimpleNamespace

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

from tests import factories


class _FakeAccount:
    """Exposes only what get_available_balance()'s new clamp step reads."""

    def __init__(self, id_val, balance, account_info):
        self.id = id_val
        self._balance = balance
        self._account_info = account_info

    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        return self._balance   # margin off in these tests

    def get_account_info(self):
        return self._account_info

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        return {} if isinstance(symbol_or_list, (list, tuple, set)) else None


class _BalanceExpert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "balance-clamp test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _resolver_for(account):
    class _R:
        def get_account_instance(self, account_id):
            return account
    return _R()


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_clamped_when_account_has_less_than_virtual_slice():
    """virtual_equity_pct=100 says the expert can spend the WHOLE 100k account balance, but the
    account's ACTUAL buying power is only 10k (another expert's oversubscribed fills, or a
    manual trade, already spent the rest) -- the real figure must win."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_BalanceExpert", virtual_equity_pct=100.0)
    expert = _BalanceExpert(inst.id)
    account = _FakeAccount(acct_def.id, balance=100_000.0,
                           account_info={"buying_power": 10_000.0})

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_resolver_for(account))
        available = expert.get_available_balance()
    finally:
        set_instance_resolver(prev)

    assert available == 10_000.0  # clamped, not the naive 100_000.0 virtual figure


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_untouched_when_actual_is_higher():
    """The clamp only ever LOWERS the figure -- when the account has plenty of real buying
    power, the expert's own (tighter) virtual-equity number stands unchanged."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_BalanceExpert", virtual_equity_pct=10.0)
    expert = _BalanceExpert(inst.id)
    account = _FakeAccount(acct_def.id, balance=100_000.0,
                           account_info={"buying_power": 90_000.0})

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_resolver_for(account))
        available = expert.get_available_balance()
    finally:
        set_instance_resolver(prev)

    assert available == 10_000.0  # the expert's own virtual figure, untouched


# ---------------------------------------------------------------------------
# _get_actual_available_balance: field-name fallback order, in isolation
# ---------------------------------------------------------------------------
class _LogCapture:
    def __init__(self):
        self.errors, self.warnings = [], []


class _ListHandler(logging.Handler):
    def __init__(self, cap):
        super().__init__(level=logging.DEBUG)
        self.cap = cap

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            self.cap.errors.append(record.getMessage())
        elif record.levelno == logging.WARNING:
            self.cap.warnings.append(record.getMessage())


@contextlib.contextmanager
def _ba2_logs():
    """``ba2_common.logger`` has propagate=False, so caplog sees nothing: attach a handler."""
    from ba2_common.logger import logger as ba2_logger
    cap = _LogCapture()
    handler = _ListHandler(cap)
    ba2_logger.addHandler(handler)
    try:
        yield cap
    finally:
        ba2_logger.removeHandler(handler)


@pytest.fixture
def no_bp_sleep(monkeypatch):
    """Record the backoff instead of sleeping."""
    slept = []
    monkeypatch.setattr(MarketExpertInterface, "_ACTUAL_BP_SLEEP", staticmethod(slept.append))
    return slept


class _InfoObj:
    """Attribute-style account info (mirrors the raw Alpaca SDK object)."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_actual_balance_prefers_buying_power_attr():
    account = _FakeAccount(1, balance=999.0, account_info=_InfoObj(buying_power=5_000.0))
    assert MarketExpertInterface._get_actual_available_balance(account) == 5_000.0


def test_actual_balance_falls_back_through_known_field_names():
    # no buying_power -> cash -> cash_balance -> equity_buying_power, in that order
    account = _FakeAccount(1, balance=999.0, account_info={"cash_balance": 42.0})
    assert MarketExpertInterface._get_actual_available_balance(account) == 42.0

    account = _FakeAccount(1, balance=999.0, account_info={"equity_buying_power": 7.0})
    assert MarketExpertInterface._get_actual_available_balance(account) == 7.0


def test_actual_balance_no_longer_substitutes_equity_when_no_known_field():
    """Equity is not buying power: an account that publishes no figure is NOT clamped (None), loudly."""
    account = _FakeAccount(1, balance=250.0, account_info={"account_number": "abc"})
    with _ba2_logs() as logs:
        assert MarketExpertInterface._get_actual_available_balance(account) is None
    assert len(logs.errors) == 1 and "SKIPPED" in logs.errors[0]


def test_actual_balance_is_none_after_bounded_retries_when_account_info_raises(no_bp_sleep):
    class _BrokenInfoAccount(_FakeAccount):
        calls = 0

        def get_account_info(self):
            type(self).calls += 1
            raise ConnectionError("broker hiccup")

    account = _BrokenInfoAccount(1, balance=88.0, account_info=None)
    with _ba2_logs() as logs:
        assert MarketExpertInterface._get_actual_available_balance(account) is None
    assert _BrokenInfoAccount.calls == MarketExpertInterface._ACTUAL_BP_ATTEMPTS == 3
    assert len(logs.errors) == 1                       # ONE error per sizing call, not one per attempt
    assert "account" in logs.errors[0].lower() and "broker hiccup" in logs.errors[0]
    assert "SKIPPED" in logs.errors[0] and "broker" in logs.errors[0]
    assert no_bp_sleep == list(MarketExpertInterface._ACTUAL_BP_BACKOFFS_S)   # short, bounded backoff between the 3 tries


def test_actual_balance_prefers_the_snapshot_seam_over_the_raw_info_probe():
    """Alpaca's raw TradeAccount.buying_power is the EFFECTIVE figure; the snapshot carries the
    REMAINING (Reg-T) power. The clamp must read the same number the UI shows."""
    class _SnapAccount(_FakeAccount):
        def get_account_snapshot(self):
            return SimpleNamespace(buying_power=869.43)

    account = _SnapAccount(1, balance=999.0, account_info=_InfoObj(buying_power=2_077.27))
    assert MarketExpertInterface._get_actual_available_balance(account) == 869.43


def test_actual_balance_falls_back_to_the_info_probe_when_the_snapshot_has_none():
    class _SnapAccount(_FakeAccount):
        def get_account_snapshot(self):
            return SimpleNamespace(buying_power=None)

    account = _SnapAccount(1, balance=999.0, account_info=_InfoObj(buying_power=5_000.0))
    assert MarketExpertInterface._get_actual_available_balance(account) == 5_000.0


def test_actual_balance_falls_back_when_the_snapshot_call_raises(no_bp_sleep):
    class _SnapAccount(_FakeAccount):
        def get_account_snapshot(self):
            raise ConnectionError("broker down")

    account = _SnapAccount(1, balance=999.0, account_info={"buying_power": 4_000.0})
    assert MarketExpertInterface._get_actual_available_balance(account) == 4_000.0


# ---------------------------------------------------------------------------
# Retry, then proceed LOUDLY without the clamp (2026-10-07, audit item 19)
# ---------------------------------------------------------------------------
class _FlakySnapshotAccount(_FakeAccount):
    """The snapshot read raises ``failures`` times, then answers."""

    def __init__(self, failures, **kw):
        super().__init__(**kw)
        self.failures = failures
        self.snapshot_calls = 0

    def get_account_snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls <= self.failures:
            raise ConnectionError("transient broker error")
        return SimpleNamespace(buying_power=10_000.0)

    def get_account_info(self):
        raise ConnectionError("info down too")


def test_a_snapshot_that_fails_twice_then_succeeds_clamps_without_an_error(no_bp_sleep):
    account = _FlakySnapshotAccount(2, id_val=1, balance=100_000.0, account_info=None)
    with _ba2_logs() as logs:
        assert MarketExpertInterface._get_actual_available_balance(account) == 10_000.0
    assert account.snapshot_calls == 3
    assert logs.errors == []
    assert no_bp_sleep == list(MarketExpertInterface._ACTUAL_BP_BACKOFFS_S)


def test_a_snapshot_that_never_answers_logs_one_error_and_skips_the_clamp(no_bp_sleep):
    account = _FlakySnapshotAccount(99, id_val=7, balance=100_000.0, account_info=None)
    with _ba2_logs() as logs:
        assert MarketExpertInterface._get_actual_available_balance(account) is None   # no raise
    assert account.snapshot_calls == 3
    assert len(logs.errors) == 1
    assert "Account 7" in logs.errors[0] and "SKIPPED" in logs.errors[0]


@pytest.mark.usefixtures("reset_test_db")
def test_sizing_proceeds_unclamped_when_buying_power_never_reads(no_bp_sleep):
    """End to end: the virtual figure stands (no equity substitute clamps it), one ERROR per sizing call."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_BalanceExpert", virtual_equity_pct=100.0)
    expert = _BalanceExpert(inst.id)
    # equity (get_balance) 5_000 is LOWER than the virtual figure would be if it were a clamp:
    # prove it is not used as one.
    account = _FlakySnapshotAccount(99, id_val=acct_def.id, balance=100_000.0, account_info=None)
    account.get_balance = lambda: 5_000.0
    account.get_tradable_balance = lambda: 100_000.0

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_resolver_for(account))
        with _ba2_logs() as logs:
            available = expert.get_available_balance()
    finally:
        set_instance_resolver(prev)
    assert available == 100_000.0
    skipped = [m for m in logs.errors if "SKIPPED" in m]
    assert len(skipped) == 1
    assert f"Account {acct_def.id} (expert {inst.id})" in skipped[0] and "3 attempts" in skipped[0]


def test_a_backtest_style_account_never_sleeps_or_retries(no_bp_sleep):
    account = _FakeAccount(1, balance=1.0, account_info={"buying_power": 321.0})
    assert MarketExpertInterface._get_actual_available_balance(account) == 321.0
    assert no_bp_sleep == []
