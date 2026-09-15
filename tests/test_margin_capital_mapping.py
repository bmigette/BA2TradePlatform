"""Every LIVE sizing decision says how its capital was derived (plan step 5).

Leverage is live-only, and the whole design rests on ONE equivalence: an account with
equity E, margin on, factor f and broker multiplier m deploys C = E x min(f, m), and it
then behaves exactly like an UNLEVERED account funded with C. That equivalence is what
makes a levered live run checkable against the unlevered backtest it is supposed to
reproduce -- but only if the live log actually says what C was and where it came from.
Before this, a $2,000 account trading like a $4,000 one logged "Virtual balance:
$4,000.00" and nothing else: indistinguishable from a $4,000 account, which is precisely
the confusion the 2026-09-09 review had to reconstruct by hand.

So ``describe_capital()`` (account) and ``describe_capital_mapping()`` (expert) publish
every term of that arithmetic, and both risk managers log it through ONE function.

MARGIN OFF is the backtest, and it is not merely equal here -- it is not reached: the
account answers from ``get_balance()`` alone, with no snapshot and no order-store read,
and the line drops to DEBUG. Pinned below with call counters, because "backtests are
unchanged" has to be true by construction.

Log assertions monkeypatch the module's own ``logger`` rather than using ``caplog``:
``ba2_common.logger`` sets ``propagate = False``, so caplog sees nothing and every log
pin would pass vacuously.
"""
import logging

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.MarketExpertInterface import (
    MarketExpertInterface, log_capital_mapping,
)
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface

from tests import factories


class _Account(ReadOnlyAccountInterface):
    """A REAL ReadOnlyAccountInterface with a canned snapshot and stored settings, built
    bare (no ``__init__`` chain) exactly as ``packages/common/tests``'
    ``test_margin_tradable_balance._Stub`` does.

    Real, and not a hand-written double, because the arithmetic under test IS the
    account's: a double that returned 4,000 would pin nothing about how 2,000 becomes it.
    """

    def __init__(self, id_val, *, balance, snapshot, settings):
        self.id = id_val
        self._balance = balance
        self._snap = snapshot
        self._stored = settings
        self.snapshot_calls = 0

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_account_snapshot(self):
        self.snapshot_calls += 1
        return self._snap

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        # The expert's broker-BP clamp reads this; it is a separate broker call from the
        # snapshot, which is why it does not touch snapshot_calls.
        return {"buying_power": self._snap.buying_power}

    # --- remaining abstract methods, stubbed ---------------------------------
    def get_positions(self):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type="bid"):
        return 100.0

    def refresh_positions(self):
        return True

    def refresh_orders(self):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []


class _Expert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "capital mapping test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


class _Recorder:
    """A logger-shaped double: which METHOD was called is the assertion."""

    def __init__(self):
        self.lines = []

    def debug(self, msg, *a, **k):
        self.lines.append((logging.DEBUG, str(msg)))

    def info(self, msg, *a, **k):
        self.lines.append((logging.INFO, str(msg)))

    def error(self, msg, *a, **k):
        self.lines.append((logging.ERROR, str(msg)))


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


def _setup(*, balance, multiplier=2.0, factor=2.0, margin_enabled=True, pct=100.0,
           buying_power=None):
    """An expert owning 100% (by default) of an account whose broker lends ``multiplier``
    and whose configured ceiling is ``factor``. Buying power defaults to the levered
    capital, so the broker's own clamp never binds and the mapping under test is the
    only thing shaping the numbers."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=pct)
    snapshot = AccountSnapshot(
        equity=balance, margin_multiplier=multiplier,
        buying_power=balance * multiplier if buying_power is None else buying_power,
        long_market_value=0.0, short_market_value=0.0)
    account = _Account(acct_def.id, balance=balance, snapshot=snapshot,
                       settings={"margin_enabled": margin_enabled,
                                 "margin_factor": factor})
    return account, _Expert(inst.id)


@pytest.mark.usefixtures("reset_test_db")
def test_two_thousand_at_2x_is_the_equivalent_of_a_four_thousand_unlevered_account():
    """The review table's row, as a machine-checkable dict: $2,000 of equity with the
    broker lending 2x and the platform's factor at 2.0 IS a $4,000 unlevered account."""
    account, expert = _setup(balance=2_000.0)

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert mapping["balance"] == 2_000.0
    assert mapping["margin_enabled"] is True
    assert mapping["margin_factor"] == 2.0
    assert mapping["broker_multiplier"] == 2.0
    assert mapping["effective_factor"] == 2.0
    assert mapping["tradable_balance"] == 4_000.0
    assert mapping["equivalent_unlevered_balance"] == 4_000.0
    assert mapping["virtual_equity_pct"] == 100.0
    assert mapping["virtual_balance"] == 4_000.0
    assert mapping["used_balance"] == 0.0
    assert mapping["available_balance"] == 4_000.0
    assert mapping["expert_id"] == expert.id
    assert "error" not in mapping


@pytest.mark.usefixtures("reset_test_db")
def test_the_mapping_tracks_the_equity_it_describes():
    """Not a constant: $1,900 x 2 is $3,800, and the equivalent unlevered account moves
    with the equity. (The review's own worked example of why the mapping must be logged
    per decision rather than at deploy time -- equity moves every day.)"""
    account, expert = _setup(balance=1_900.0)

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert mapping["tradable_balance"] == 3_800.0
    assert mapping["equivalent_unlevered_balance"] == 3_800.0
    assert mapping["virtual_balance"] == 3_800.0


@pytest.mark.usefixtures("reset_test_db")
def test_the_expert_slice_and_the_account_capital_are_separate_columns():
    """virtual_equity_pct scales the EXPERT's slice, not the account's capital: at 50%
    the expert may deploy 2,000 of an equivalent 4,000 account. Reporting one figure for
    both is how a half-funded expert reads as a fully deployed account."""
    account, expert = _setup(balance=2_000.0, pct=50.0)

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert mapping["equivalent_unlevered_balance"] == 4_000.0
    assert mapping["virtual_equity_pct"] == 50.0
    assert mapping["virtual_balance"] == 2_000.0
    assert mapping["available_balance"] == 2_000.0


@pytest.mark.usefixtures("reset_test_db")
def test_the_broker_multiplier_still_binds_below_the_configured_factor():
    """effective_factor is min(factor, multiplier) -- the mapping must report what the
    broker will actually lend, not what the setting asks for, or it would explain a
    quantity the account cannot support."""
    account, expert = _setup(balance=2_000.0, multiplier=1.5, factor=2.0)

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert mapping["margin_factor"] == 2.0
    assert mapping["broker_multiplier"] == 1.5
    assert mapping["effective_factor"] == 1.5
    assert mapping["equivalent_unlevered_balance"] == 3_000.0


@pytest.mark.usefixtures("reset_test_db")
def test_margin_off_costs_no_snapshot_and_no_order_store_read(monkeypatch):
    """THE BACKTEST PIN. With margin off the description is ``get_balance()`` and
    nothing else: no broker snapshot, no orders query, factor 1.0, and the equivalent
    unlevered account is the account itself. Anything else here would be new work (and
    new failure modes) on the backtest's per-bar sizing path."""
    from ba2_common.core import trade_store

    account, expert = _setup(balance=2_000.0, margin_enabled=False)
    store_reads = []
    monkeypatch.setattr(trade_store, "orders_where",
                        lambda *a, **k: store_reads.append((a, k)) or [])

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert mapping["margin_enabled"] is False
    assert mapping["effective_factor"] == 1.0
    assert mapping["balance"] == 2_000.0
    assert mapping["tradable_balance"] == 2_000.0
    assert mapping["equivalent_unlevered_balance"] == 2_000.0
    assert mapping["virtual_balance"] == 2_000.0
    for key in ("margin_factor", "broker_multiplier", "broker_buying_power",
                "gross_exposure", "pending_entries", "headroom"):
        assert mapping[key] is None, key
    assert account.snapshot_calls == 0, "margin off must not cost a broker round trip"
    assert store_reads == [], "margin off must not query the order store"


@pytest.mark.usefixtures("reset_test_db")
def test_a_refused_broker_figure_becomes_an_error_entry_not_an_exception():
    """A description is a DIAGNOSTIC: it must never be the thing that breaks a sizing
    pass. The account still refuses loudly (finding 5: a NaN multiplier is not a
    multiplier) -- the refusal is carried in the dict, and the caller logs it at ERROR.
    """
    account, expert = _setup(balance=2_000.0, multiplier=float("nan"))

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert "non-finite" in mapping["error"]
    assert mapping["expert_id"] == expert.id
    assert "effective_factor" not in mapping, "no fabricated factor behind the refusal"
    # And the refusal is real, not swallowed upstream:
    with pytest.raises(ValueError, match="non-finite"):
        account.describe_capital()


@pytest.mark.usefixtures("reset_test_db")
def test_the_ceiling_terms_travel_with_the_mapping():
    """gross/pending/headroom ride along, so a clamped quantity can be read back to the
    exposure that clamped it without a second investigation."""
    account, expert = _setup(balance=2_000.0)
    account._snap = AccountSnapshot(
        equity=2_000.0, margin_multiplier=2.0, buying_power=4_000.0,
        long_market_value=1_500.0, short_market_value=-500.0)

    mapping = _with_account(account, expert.describe_capital_mapping)

    assert mapping["gross_exposure"] == 2_000.0        # 1,500 long + |-500| short
    assert mapping["pending_entries"] == 0.0
    assert mapping["headroom"] == 2_000.0              # 4,000 ceiling - 2,000 held
    assert mapping["broker_buying_power"] == 4_000.0


@pytest.mark.usefixtures("reset_test_db")
def test_a_provided_balance_pass_is_not_run_a_second_time(monkeypatch):
    """THE POINT OF THE ``balances`` ARGUMENT. Both risk managers take ONE
    ``_available_balance_breakdown()`` and hand it here, so the mapping explains the pass
    the order was sized from -- not a second reading taken microseconds later against a
    moved book -- and the pass (a transactions query plus a bulk price fetch) runs once.

    What the mapping still costs on top of that, with margin ON, is pinned exactly:
    ONE more snapshot and ONE more working-order scan, both from ``describe_capital()``
    measuring gross/pending exposure. The breakdown's headroom clamp took its own
    reading; sharing them would mean threading the account's StockExposure out through
    ``get_available_balance``, past the seam guard that lets a narrower account be wired
    at all. Undercounting this in a docstring is how a round-trip budget goes wrong.
    """
    from ba2_common.core import trade_store

    account, expert = _setup(balance=2_000.0)
    scans = []
    real_orders_where = trade_store.orders_where
    monkeypatch.setattr(trade_store, "orders_where",
                        lambda *a, **k: (scans.append(k) or real_orders_where(*a, **k)))

    balances = _with_account(account, expert._available_balance_breakdown)
    assert balances.available == 4_000.0
    snapshots_for_the_pass, scans_for_the_pass = account.snapshot_calls, len(scans)

    mapping = _with_account(
        account, lambda: expert.describe_capital_mapping(balances=balances))

    assert mapping["available_balance"] == balances.available
    assert mapping["virtual_balance"] == balances.virtual
    assert mapping["used_balance"] == balances.used
    assert account.snapshot_calls - snapshots_for_the_pass == 1
    assert len(scans) - scans_for_the_pass == 1


@pytest.mark.usefixtures("reset_test_db")
def test_an_unavailable_expert_balance_is_an_error_not_three_quiet_nones():
    """A mapping whose expert figures are all None is not an ordinary INFO line: the
    expert could not say what it may deploy, and the level policy has to see it."""
    account, expert = _setup(balance=2_000.0)

    class _Broken(_Expert):
        def _available_balance_breakdown(self, exclude_transaction_id=None):
            return None

    mapping = _with_account(
        account, _Broken(expert.id).describe_capital_mapping)

    assert "expert balance unavailable" in mapping["error"]
    assert mapping["available_balance"] is None
    assert mapping["tradable_balance"] == 4_000.0, "the account half still stands"


@pytest.mark.usefixtures("reset_test_db")
def test_an_account_that_publishes_no_description_is_an_error_mapping():
    """The account is reached through a getattr-guarded seam (a host may wire a narrower
    object), so the mapping can legitimately come back with no factor in it. That is the
    ERROR branch -- never a KeyError inside a diagnostic."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)

    class _NarrowAccount:
        """Everything the balance pass reads, and no ``describe_capital``."""

        id = acct_def.id

        def get_balance(self):
            return 2_000.0

        def get_tradable_balance(self):
            return 2_000.0

        def get_account_info(self):
            return {"buying_power": 2_000.0}

        def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
            return {} if isinstance(symbol_or_list, (list, tuple, set)) else 100.0

        def get_stock_exposure_headroom(self, exclude_order_id=None):
            return None

    expert = _Expert(inst.id)
    log = _Recorder()
    mapping = _with_account(_NarrowAccount(),
                            lambda: log_capital_mapping(expert, log))

    assert "publishes no capital description" in mapping["error"]
    assert "effective_factor" not in mapping
    assert mapping["account_id"] == acct_def.id
    assert mapping["available_balance"] == 2_000.0, "the expert half still stands"
    assert [lvl for lvl, _ in log.lines] == [logging.ERROR], log.lines


# --------------------------------------------------------------------------
# The log line: the classic risk manager's real sizing entry point.
# --------------------------------------------------------------------------

@pytest.fixture
def rm_logs(monkeypatch):
    """``(levelno, message)`` for every line TradeRiskManagement logs. See the module
    docstring for why this is not ``caplog``."""
    # The module object, not ``from ... import logger``: the fixture patches methods on
    # the logger OBJECT the RM holds (self.logger IS this one).
    from ba2_common.core import TradeRiskManagement as module

    seen = []
    for name, level in (("debug", logging.DEBUG), ("info", logging.INFO),
                        ("warning", logging.WARNING), ("error", logging.ERROR)):
        monkeypatch.setattr(
            module.logger, name,
            lambda msg, *a, _lvl=level, **k: seen.append((_lvl, str(msg))))
    return seen


def _size_nothing(account, expert):
    """Drive the classic RM's real sizing core with an empty candidate list: the
    balance resolution and its logging is the whole point, and no order is needed to
    reach it."""
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertInstance

    expert_instance = get_instance(ExpertInstance, expert.id)
    return _with_account(account, lambda: TradeRiskManagement()._size_prioritized_orders(
        expert, expert_instance, expert.id, [], 0.1))


def _mapping_lines(records, level):
    return [msg for lvl, msg in records if lvl == level and msg.startswith("Capital mapping:")]


@pytest.mark.usefixtures("reset_test_db")
def test_the_classic_rm_logs_the_mapping_at_info_when_leverage_is_in_play(rm_logs):
    account, expert = _setup(balance=2_000.0)

    _size_nothing(account, expert)

    lines = _mapping_lines(rm_logs, logging.INFO)
    assert len(lines) == 1, rm_logs
    assert "equivalent_unlevered_balance=$4,000.00" in lines[0]
    assert not _mapping_lines(rm_logs, logging.DEBUG)


@pytest.mark.usefixtures("reset_test_db")
def test_the_classic_rm_line_says_which_balance_it_sized_from(rm_logs):
    """ONE pass, one story: the available balance in the mapping must be the very number
    the risk manager divided into orders. Two passes could differ (a fill lands between
    them) and the log would then explain a decision that was never made."""
    account, expert = _setup(balance=2_000.0, pct=50.0)

    (_, _, _, total_virtual_balance, _) = _size_nothing(account, expert)

    line = _mapping_lines(rm_logs, logging.INFO)[0]
    assert total_virtual_balance == 2_000.0
    assert f"available_balance=${total_virtual_balance:,.2f}" in line
    assert "equivalent_unlevered_balance=$4,000.00" in line


@pytest.mark.usefixtures("reset_test_db")
def test_a_cash_account_with_margin_enabled_is_a_debug_line(rm_logs):
    """DEBUG is not "margin_enabled is False", it is "leverage is not in play". A broker
    that will not lend (multiplier 1.0) leaves effective_factor at 1.0 however the
    setting is spelled, and the quantities then DO correspond to the account's own
    equity -- nothing to explain."""
    account, expert = _setup(balance=2_000.0, multiplier=1.0, factor=2.0)

    _size_nothing(account, expert)

    debug = _mapping_lines(rm_logs, logging.DEBUG)
    assert len(debug) == 1, rm_logs
    assert "effective_factor=1.0" in debug[0]
    assert "equivalent_unlevered_balance=$2,000.00" in debug[0]
    assert not _mapping_lines(rm_logs, logging.INFO)


@pytest.mark.usefixtures("reset_test_db")
def test_the_classic_rm_logs_the_mapping_at_debug_with_margin_off(rm_logs):
    """An unlevered account's mapping says nothing its balance did not already say, and
    this runs once per sizing pass for a whole backtest."""
    account, expert = _setup(balance=2_000.0, margin_enabled=False)

    _size_nothing(account, expert)

    assert len(_mapping_lines(rm_logs, logging.DEBUG)) == 1, rm_logs
    assert not _mapping_lines(rm_logs, logging.INFO)
    assert account.snapshot_calls == 0


@pytest.mark.usefixtures("reset_test_db")
def test_a_refused_mapping_is_logged_at_error():
    """A mapping built on refused broker figures is never quiet -- and never INFO, where
    it would read as a normal levered decision.

    Driven through the shared logging function rather than the risk manager, because the
    RM's own balance resolution refuses on the same NaN one line EARLIER (it raises
    before it can log anything); the level policy is what is under test here.
    """
    account, expert = _setup(balance=2_000.0, multiplier=float("nan"))

    log = _Recorder()
    mapping = _with_account(account, lambda: log_capital_mapping(expert, log))

    assert "error" in mapping
    assert [lvl for lvl, _ in log.lines] == [logging.ERROR], log.lines


@pytest.mark.usefixtures("reset_test_db")
def test_the_smart_rm_logs_the_same_mapping_from_the_same_function(monkeypatch):
    """The SECOND caller. Pinned because a mapping that appears on one live sizing path
    and not the other is how those two paths came to disagree about sizing in the first
    place (review finding 1); and because the Smart RM's own body is wrapped in a
    handler that turns any exception into a quantity of 0, so the call deliberately sits
    OUTSIDE it -- a diagnostic must never be able to silently zero a live order."""
    from ba2_trade_platform.core import SmartRiskManagerToolkit as srm
    from ba2_trade_platform.core.types import OrderDirection

    seen = []
    for name, level in (("debug", logging.DEBUG), ("info", logging.INFO),
                        ("error", logging.ERROR)):
        monkeypatch.setattr(
            srm.logger, name,
            lambda msg, *a, _lvl=level, **k: seen.append((_lvl, str(msg))))

    account, expert = _setup(balance=2_000.0)
    toolkit = object.__new__(srm.SmartRiskManagerToolkit)
    toolkit.expert = expert
    toolkit.logger = logging.getLogger("test_margin_capital_mapping")
    toolkit.get_current_price = lambda symbol: 100.0

    _with_account(account, lambda: toolkit._auto_size_by_risk(
        "AAPL", OrderDirection.BUY, sl_price=95.0))

    info = [msg for lvl, msg in seen
            if lvl == logging.INFO and msg.startswith("Capital mapping:")]
    assert len(info) == 1, seen
    assert "equivalent_unlevered_balance=$4,000.00" in info[0]


# --------------------------------------------------------------------------
# The mapping never raises, and "the caller's pass failed" is not "the caller
# passed nothing" (2026-09-09 final review, I4/I5).
# --------------------------------------------------------------------------

class _CountingExpert(_Expert):
    """Counts balance passes. The cost of this diagnostic is the thing under test."""

    def __init__(self, id_val):
        super().__init__(id_val)
        self.passes = 0

    def _available_balance_breakdown(self, exclude_transaction_id=None):
        self.passes += 1
        return super()._available_balance_breakdown(exclude_transaction_id)


@pytest.mark.usefixtures("reset_test_db")
def test_a_caller_reporting_a_failed_pass_does_not_trigger_a_second_one():
    """``None`` is the real answer a FAILED balance pass gives, and the Smart RM hands
    its pass's result straight through. With ``None`` as the default argument the two
    were indistinguishable: the mapping quietly re-ran the pass -- an extra broker round
    trip in the live margin path, describing a different instant than the order was
    sized from, and hiding the refusal the caller was reporting. The sentinel splits
    them."""
    account, expert = _setup(balance=2_000.0)
    counting = _CountingExpert(expert.id)

    mapping = _with_account(
        account, lambda: counting.describe_capital_mapping(balances=None))

    assert counting.passes == 0, "a reported failure must not be retried"
    assert "expert balance unavailable" in mapping["error"]
    assert mapping["available_balance"] is None
    assert mapping["tradable_balance"] == 4_000.0, "the account half still stands"


@pytest.mark.usefixtures("reset_test_db")
def test_omitting_the_argument_still_takes_exactly_one_pass():
    """The other half of the sentinel: a caller with no pass of its own gets one, once."""
    account, expert = _setup(balance=2_000.0)
    counting = _CountingExpert(expert.id)

    mapping = _with_account(account, counting.describe_capital_mapping)

    assert counting.passes == 1
    assert mapping["available_balance"] == 4_000.0
    assert "error" not in mapping


@pytest.mark.usefixtures("reset_test_db")
def test_an_unregistered_account_is_an_error_entry_not_a_raised_keyerror():
    """The resolver's "unregistered id" is a ``KeyError``
    (``BacktestInstanceResolver.get_account_instance``), and a DIAGNOSTIC that raises out
    of the sizing path it is only describing turns a missing log line into a dead
    order."""
    from ba2_common.core.instance_resolver import (
        get_instance_resolver, set_instance_resolver)

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)

    class _Unregistered:
        def get_account_instance(self, account_id):
            raise KeyError(f"no account registered for id={account_id}")

    expert = _Expert(inst.id)
    log = _Recorder()
    prev = get_instance_resolver()
    try:
        set_instance_resolver(_Unregistered())
        mapping = log_capital_mapping(expert, log)
    finally:
        set_instance_resolver(prev)

    assert "KeyError" in mapping["error"], mapping
    assert str(acct_def.id) in mapping["error"]
    assert [lvl for lvl, _ in log.lines] == [logging.ERROR], log.lines
