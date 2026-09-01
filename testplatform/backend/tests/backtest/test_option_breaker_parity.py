"""The option sleeve's drawdown breaker transitions IDENTICALLY in both runtimes.

Design ``docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`` 11.5, and the
operator ruling of 2026-09-01 that resolved it.

The state before this file existed
----------------------------------
The breaker LATCH was shared -- ``check_rails`` refuses every candidate while ``halted``,
in both runtimes -- but the TRANSITIONS were not. ``option_lifecycle_service`` was the only
production caller of ``option_book.update_breaker``, it lives in the live tree and it is
reached only from ``JobManager``. So in a backtest ``get_breaker_state`` answered
``BreakerState()`` on every bar: the peak was never ratcheted, the breaker never tripped,
never re-armed, and ``RAIL_BREAKER_HALTED`` was UNREACHABLE. A ``classic_options`` backtest
was systematically more permissive than live, which is the opposite of what a backtest is
for.

The fix was blocked on a real question, because wiring the breaker through the existing
reader would have hidden the divergence one layer deeper: ``sleeve_equity`` called
``account.get_balance()``, which is account EQUITY on Alpaca and spendable CASH on
``BacktestAccount``. A breaker on cash trips when the sleeve DEPLOYS capital and clears when
it CLOSES a position, regardless of P&L -- and the same mismatch was already the denominator
of ``max_deployment_pct`` and ``max_notional_leverage``. The ruling: ONE definition,
``account.get_account_snapshot().equity``, for the breaker and for those rails, in both
runtimes.

How this test is built, and what it deliberately is NOT
------------------------------------------------------
A test that fed the same SEQUENCE OF EQUITY NUMBERS to both sides would pass with the defect
fully intact -- it is precisely the test that would have masked it. So:

* the shared thing is an account STATE (cash, positions, marks), never an equity number;
* the BACKTEST side is a real ``DailyBacktestEngine.run()`` over a real ``BacktestAccount``:
  the breaker is transitioned by the engine's own per-bar call, and the equity it measures is
  computed by the account from its own ledger and price source. Delete that call from the bar
  loop and this test fails, not only the engine test;
* the LIVE side is ``AlpacaAccount.get_account_snapshot`` -- the real live reader, borrowed
  as a function -- over a real ``alpaca.trading.models.TradeAccount``, whose equity is what a
  BROKER publishes: computed here from the state, independently of anything the backtest
  account said;
* both sides then go through the one shared transition,
  ``OptionRiskManagement.update_sleeve_breaker``, which is the function
  ``option_lifecycle_service.run_option_lifecycle_pass`` calls (pinned by name in
  ``tests/test_option_risk_manager_live_wiring.py``).

The two sides are cross-checked for state equivalence before their transitions are compared,
so "the same state" is asserted rather than assumed.

Run from the backend dir (with the worktree on PYTHONPATH):
    python -m pytest tests/backtest/test_option_breaker_parity.py -q
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ba2_common.core.interfaces.ExtendableSettingsInterface import (
    ExtendableSettingsInterface,
)
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


# --------------------------------------------------------------------------- #
# The price path. ONE underlying, one position, so the sleeve's equity is
# cash + qty x close and the two sides can be compared exactly.
#
# It deploys at flat P&L, then collapses far past a 20% breaker, then recovers past the 10%
# re-arm line: the sequence has to CONTAIN a trip and a re-arm, or "identical transitions" is
# satisfied by two breakers that both did nothing.
# --------------------------------------------------------------------------- #
BARS = [
    (date(2024, 1, 2), 100, 101, 99, 100),     # entry bar: the stub buys
    (date(2024, 1, 3), 100, 101, 99, 100),     # fill @ open 100 -- ~90% of the account DEPLOYED
    (date(2024, 1, 4), 100, 101, 99, 100),     # flat P&L, cash at ~10%: a CASH breaker trips HERE
    (date(2024, 1, 5), 90, 91, 59, 60),        # -40% on the position -> the EQUITY breaker trips
    (date(2024, 1, 8), 60, 61, 39, 40),        # deeper, still standing down
    (date(2024, 1, 9), 45, 96, 44, 95),        # recovery past the re-arm line -> cleared
    (date(2024, 1, 10), 95, 101, 94, 100),     # and held
]
START = datetime(2024, 1, 2)
END = datetime(2024, 1, 10)

#: 20% peak-to-trough stand-down; ``option_book.BREAKER_REARM_DEPTH_FRACTION`` puts the
#: re-arm line at 10%.
BREAKER_PCT = 20.0

#: A complete, ordinary sleeve configuration. Every one of these is REQUIRED and none of them
#: has a default (review finding M1 for the rails, the 2026-09-01 ruling for the breaker).
SLEEVE_SETTINGS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 40.0,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
    "circuit_breaker_pct": BREAKER_PCT,
}

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


def _bar_rows(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


class _StubExpert(MarketExpertInterface):
    """BUY AAPL on the first bar, HOLD afterwards. No providers, no network."""

    def __init__(self, id: int, price_source):
        super().__init__(id)
        self._ps = price_source
        self._buy_on = BARS[0][0]

    @classmethod
    def description(cls) -> str:            # abstract
        return "Stub expert for the option-breaker parity test."

    def render_market_analysis(self, market_analysis) -> str:   # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:   # abstract
        return None

    def analyze_as_of(self, as_of, context):
        close = self._ps.close_at("AAPL", as_of)
        day = as_of.date() if hasattr(as_of, "date") else as_of
        signal = OrderRecommendation.BUY if day == self._buy_on else OrderRecommendation.HOLD
        return Recommendation(signal=signal, confidence=80.0, current_price=float(close),
                              details="stub", expected_profit_percent=10.0)


class _SleeveExpert:
    """The LIVE side's expert: the same sleeve settings, read through the REAL accessor.

    ``get_setting_with_interface_default`` over ``MarketExpertInterface``'s own declarations,
    borrowed as functions rather than imitated -- the style commit 50ea80cc established, so
    an undeclared threshold behaves here exactly as it does in production instead of being
    modelled by a double that raises where the real accessor returns.
    """

    get_setting_with_interface_default = (
        ExtendableSettingsInterface.get_setting_with_interface_default)
    get_merged_settings_definitions = (
        MarketExpertInterface.get_merged_settings_definitions)

    def __init__(self, instance_id: int, settings: Dict[str, Any]):
        self.id = instance_id
        self.settings = dict(settings)


# --------------------------------------------------------------------------- #
# The account STATE: what both runtimes are shown. Never an equity number.
# --------------------------------------------------------------------------- #
class _State(tuple):
    """``(cash, ((symbol, qty, mark), ...))`` -- the whole description of an account."""

    @property
    def cash(self) -> float:
        return self[0]

    @property
    def positions(self) -> Tuple[Tuple[str, float, float], ...]:
        return self[1]

    @property
    def market_value(self) -> float:
        """What a BROKER would publish as the long market value of this state."""
        return sum(qty * mark for _sym, qty, mark in self.positions)


def _read_state(account) -> _State:
    """Describe the backtest account WITHOUT asking it for its equity.

    ``get_balance()`` is the cash leg -- that is exactly what it means on this account -- and
    ``get_positions()`` carries each holding's quantity and current mark. What those add up
    to is then each side's own business, which is the whole point of the exercise.
    """
    positions = tuple(
        (p["symbol"], float(p["qty"]), float(p["current_price"]))
        for p in account.get_positions() if p["qty"]
    )
    return _State((float(account.get_balance()), positions))


def _live_account(state: _State):
    """The same state as a LIVE Alpaca account, read by Alpaca's own snapshot mapper.

    ``AlpacaAccount.get_account_snapshot`` is borrowed as a function onto a minimal object
    that answers ``get_account_info()`` with a real ``TradeAccount``, so the equity this side
    reports travels the production Alpaca chain (``TradeAccount.equity`` -- a STRING --
    through Alpaca's own ``float()`` coercion) rather than a hand-built ``AccountSnapshot``.
    Constructing a real ``AlpacaAccount`` would need credentials and a broker; its reader
    does not.
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from alpaca.trading.models import TradeAccount

    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    # The BROKER's arithmetic, done here from the state -- never copied from the other side.
    equity = state.cash + state.market_value
    info = TradeAccount(
        id=uuid4(),
        account_number="PARITY",
        status="ACTIVE",
        currency="USD",
        cash=str(state.cash),
        equity=str(equity),
        last_equity=str(equity),
        long_market_value=str(state.market_value),
        short_market_value="0",
        buying_power=str(state.cash),
        multiplier="1",
        pattern_day_trader=False,
        trading_blocked=False,
        transfers_blocked=False,
        account_blocked=False,
        shorting_enabled=False,
    )

    account = SimpleNamespace(
        id=9001,
        _account_snapshot_cache=None,
        _ACCOUNT_SNAPSHOT_CACHE_TTL=AlpacaAccount._ACCOUNT_SNAPSHOT_CACHE_TTL,
        get_account_info=lambda: info,
    )
    # THE live reader, borrowed rather than imitated.
    account.get_account_snapshot = lambda: AlpacaAccount.get_account_snapshot(account)
    return account


def _shape(states) -> List[Tuple[Optional[float], bool, bool]]:
    """A breaker history reduced to what parity means: (peak, halted, tripped) per call."""
    return [(s.peak_equity, s.halted, s.tripped) for s in states]


# --------------------------------------------------------------------------- #
# The backtest run
# --------------------------------------------------------------------------- #
def _run_backtest(monkeypatch, *, opted_in: bool, account_id: int, expert_id: int):
    """Run the engine once and record every per-bar breaker call it made.

    Returns ``(recorded, account)``. ``recorded`` is a list of ``(state, breaker-after)`` and
    is EMPTY for a run in which the engine made no call at all -- which is what the
    equity-trial pin and the deleted-caller mutation both look at.

    The spy WRAPS the real shared function (it does not replace it), so the breaker states it
    records are the ones production would have produced.
    """
    import app.services.backtest.daily_engine as engine_mod
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"breaker-parity-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        ruleset_id = seed_enter_long_ruleset(name=f"breaker-parity-{account_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_StubExpert",
                             enter_market_ruleset_id=ruleset_id, instance_id=expert_id)

        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _bar_rows(BARS))

        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)

        expert = _StubExpert(expert_id, price_source=ps)
        settings: Dict[str, Any] = {
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            # DEPLOY most of the account into one name. The default 10% per-instrument cap
            # would leave cash at 90% of equity, where the cash and equity readings are too
            # close to distinguish -- and no single position could then draw the ACCOUNT down
            # past a 20% breaker at all. ``test_the_two_sides_would_DISAGREE_...`` pins that
            # this run really does separate the two numbers.
            "max_virtual_equity_per_instrument_percent": (90.0, "float"),
            # A far safeguard stop. The classic RM always attaches one (an entry must never
            # trade unprotected), and at its default ~7% it would liquidate the position on
            # the first down bar -- ending the drawdown the breaker is supposed to measure
            # and removing the recovery entirely.
            "use_atr_stop": (False, "bool"),
            "risk_per_trade_pct": (95.0, "float"),
            "min_stop_loss_pct": (95.0, "float"),
        }
        if opted_in:
            settings["risk_manager_mode"] = ("classic_options", "str")
            for key, value in SLEEVE_SETTINGS.items():
                settings[key] = (value, "int" if isinstance(value, int) else "float")
        expert.save_settings(settings)
        resolver.register_expert(expert_id, expert)

        recorded: List[Tuple[_State, Any]] = []
        real = engine_mod.update_sleeve_breaker

        def spy(*, expert, account, expert_instance_id):
            state = _read_state(account)
            result = real(expert=expert, account=account,
                          expert_instance_id=expert_instance_id)
            recorded.append((state, result))
            return result

        monkeypatch.setattr(engine_mod, "update_sleeve_breaker", spy)

        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, expert.settings, ruleset_id)],
            price_source=ps, config={"start_date": START, "end_date": END,
                                     "enabled_instruments": ["AAPL"], "seed": 42},
            indicator_provider=None)
        engine._indicator_provider = None
        engine.run()
        return recorded, account
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    """The breaker latch is process state keyed by sleeve; a leak would decide a later test."""
    import ba2_common.core.OptionRiskManagement as rm

    rm.reset_breaker_states()
    rm.reset_mode_warnings()
    yield
    rm.reset_breaker_states()
    rm.reset_mode_warnings()


# --------------------------------------------------------------------------- #
# 1. an equity trial does no option work at all
# --------------------------------------------------------------------------- #
def test_an_equity_trial_makes_ZERO_breaker_calls(monkeypatch):
    """Design 11: nothing existing changes until an expert is switched over -- and that has
    to be a property of the code, measured by CALL COUNT rather than by a timing sample that
    a fast no-op would also satisfy.

    The engine resolves its option sleeves ONCE per run from the same
    ``option_risk_manager_enabled`` dispatch the entry gate uses. A run with no
    ``classic_options`` expert leaves that list empty, so the per-bar flow reaches the option
    risk manager zero times over the whole simulation.

    MUTATION KILL: drop the ``option_risk_manager_enabled`` filter (call the breaker for
    every expert) and this counts one call per bar.
    """
    recorded, _account = _run_backtest(monkeypatch, opted_in=False,
                                       account_id=9101, expert_id=9101)
    assert recorded == []


def test_a_classic_options_trial_transitions_the_breaker_on_EVERY_bar(monkeypatch):
    """The other half: the call count is one per simulated bar, not one per entry bar.

    The peak has to be ratcheted on bars where nothing trades, or a sleeve measures its
    drawdown from whatever equity it happened to have on its last entry.
    """
    recorded, account = _run_backtest(monkeypatch, opted_in=True,
                                      account_id=9102, expert_id=9102)
    assert len(recorded) == len(BARS)
    assert len(account.get_balance_history()) == len(BARS)


# --------------------------------------------------------------------------- #
# 2. THE parity: the same states, each runtime's own reader, identical transitions
# --------------------------------------------------------------------------- #
def test_the_breaker_transitions_identically_in_both_runtimes(monkeypatch):
    """The ruling of 2026-09-01, as a test.

    The BACKTEST side is a real engine run; the states it passed through are read off the
    account (cash, positions, marks). The LIVE side is those same states presented to
    Alpaca's own snapshot reader and driven through the same shared transition. The two
    breakers must trip at the same drawdown and re-arm at the same recovery.

    MUTATION KILLS, both of which this test catches and a common-number-sequence test would
    not:

    * restore ``sleeve_equity`` to ``account.get_balance()``: the backtest side reads
      spendable CASH, so it measures a ~50% drawdown on the bar the position is BOUGHT (P&L
      flat) and stands the sleeve down there, while the live side sails on. The recorded
      shapes stop matching on bar 2;
    * delete the per-bar call from ``daily_engine.run()``: the backtest side records nothing
      and the lengths stop matching on the first assertion.
    """
    import ba2_common.core.OptionRiskManagement as rm

    recorded, _account = _run_backtest(monkeypatch, opted_in=True,
                                       account_id=9103, expert_id=9103)
    assert recorded, "the engine made no breaker call at all"
    states = [state for state, _breaker in recorded]
    backtest_shape = _shape([breaker for _state, breaker in recorded])

    # The sequence must actually EXERCISE the thing: a trip and a re-arm.
    assert any(halted for _peak, halted, _tripped in backtest_shape), backtest_shape
    assert any(tripped for _peak, _halted, tripped in backtest_shape), backtest_shape
    assert not backtest_shape[-1][1], "the sleeve never recovered -- no re-arm to compare"
    # ...and the position was really DEPLOYED, or "equity vs cash" is untested.
    assert any(state.positions for state in states)

    live_expert = _SleeveExpert(9203, {"risk_manager_mode": "classic_options",
                                       **SLEEVE_SETTINGS})
    live_shape = []
    for state in states:
        account = _live_account(state)
        # STATE EQUIVALENCE, asserted rather than assumed: the live account this test builds
        # must report the same equity the backtest account computed for itself from its own
        # ledger. If it did not, the two sides would be comparing different accounts and
        # "identical transitions" would mean nothing.
        assert account.get_account_snapshot().equity == pytest.approx(
            state.cash + state.market_value)
        live_shape.append(rm.update_sleeve_breaker(
            expert=live_expert, account=account, expert_instance_id=9203))

    assert _shape(live_shape) == backtest_shape


def test_the_two_sides_would_DISAGREE_if_the_backtest_read_cash(monkeypatch):
    """The guard on the guard: prove the states above can tell equity from cash.

    If every recorded state had cash == equity (a run that never deployed anything, or one
    whose position was worthless), the parity test would pass against a breaker reading
    either number and would be pinning nothing at all. So: at least one state where the two
    differ by more than the breaker's own threshold -- i.e. a state where a cash-based
    breaker MUST have tripped and an equity-based one must not.
    """
    recorded, _account = _run_backtest(monkeypatch, opted_in=True,
                                       account_id=9104, expert_id=9104)
    states = [state for state, _breaker in recorded]
    peak_equity = max(state.cash + state.market_value for state in states)
    divergent = [
        state for state in states
        if state.cash <= peak_equity * (1.0 - BREAKER_PCT / 100.0)
        and state.cash + state.market_value > peak_equity * (1.0 - BREAKER_PCT / 100.0)
    ]
    assert divergent, (
        "no state in this run distinguishes cash from equity by more than the breaker "
        f"threshold -- the parity test would pass against either reader. States: {states}")
