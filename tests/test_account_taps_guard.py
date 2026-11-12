"""Guard: every SHIPPED account class has its stock-decision taps, overrides included.

``ReadOnlyAccountInterface.__init_subclass__`` taps the sizing reads and the five (six) effect
methods of each subclass. This pins the outcome on the real accounts (a root test: it imports the live
ones), so a new broker, a new override or a hand-rolled wrapper that loses a tap fails here rather
than silently recording nothing on 8080/8081/8082. A second guard greps the decision path for
``account.<method>(`` and fails on a method that is neither tapped nor allowlisted with a reason.
"""
import os
import re
import sys

import pytest

from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.replay.account_taps import (
    EFFECT_METHODS, SIZING_READ_TAPPED_METHODS, effect_boundary, untapped_methods,
)
from ba2_common.core.replay.observe import tapped_boundary
from ba2_common.core.replay.option_taps import OPTION_TAPPED_METHODS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TP_BACKEND = os.path.join(_ROOT, "testplatform", "backend")


def _load_backend(module, attr):
    path = os.path.join(_TP_BACKEND, *module.split(".")[:-1], module.split(".")[-1] + ".py")
    if not os.path.exists(path):
        pytest.skip("testplatform backend not in this checkout")
    if _TP_BACKEND not in sys.path:
        sys.path.insert(0, _TP_BACKEND)
    # An ImportError here is a real failure, as in the other testplatform-importing tests.
    return getattr(__import__(module, fromlist=[attr]), attr)


def _alpaca():
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    return AlpacaAccount


def _ibkr():
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
    return IBKRAccount


def _tasty():
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount
    return TastyTradeAccount


def _backtest():
    return _load_backend("app.services.backtest.backtest_account", "BacktestAccount")


def _tape_options():
    return _load_backend("app.services.replay.option_selection", "TapeOptionsAccount")


@pytest.fixture(params=[_alpaca, _ibkr, _tasty, _backtest],
                ids=["alpaca", "ibkr", "tastytrade", "backtest"])
def account_class(request):
    return request.param()


def test_every_tapped_name_resolves_to_a_tapped_callable(account_class):
    for name in list(SIZING_READ_TAPPED_METHODS) + list(EFFECT_METHODS):
        resolved = getattr(account_class, name)
        assert callable(resolved), f"{account_class.__name__}.{name}"
        tapped = tapped_boundary(resolved) is not None or effect_boundary(resolved) is not None
        assert tapped, (
            f"{account_class.__name__}.{name} is not tapped (resolved from "
            f"{[k.__name__ for k in account_class.__mro__ if name in vars(k)][:1]})")


def test_no_class_in_the_mro_defines_an_untapped_override(account_class):
    assert untapped_methods(account_class) == []


def test_every_known_account_class_is_tapped():
    """Whatever concrete account classes the process has loaded (shipped brokers, the backtest account,
    the replay tape accounts, test doubles) are tapped; a new subclass is covered without a list."""
    for factory in (_alpaca, _ibkr, _tasty, _backtest, _tape_options):
        factory()
    stack, seen = list(ReadOnlyAccountInterface.__subclasses__()), []
    while stack:
        klass = stack.pop()
        if klass not in seen:
            seen.append(klass)
            stack.extend(klass.__subclasses__())
    assert len(seen) >= 6
    assert [p for klass in seen for p in untapped_methods(klass)] == []


def test_the_replay_tape_account_is_exempt_so_a_replay_never_records_into_its_own_tape():
    tape = _tape_options()
    assert tape._replay_taps_exempt is True
    assert tapped_boundary(tape.get_positions) is None and effect_boundary(tape.adjust_tp) is None


def test_the_overlapping_reads_keep_the_option_provider_on_option_accounts(account_class):
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    for name in ("get_account_snapshot", "get_positions"):
        provider = tapped_boundary(getattr(account_class, name))[0]
        expected = OPTION_TAPPED_METHODS[name][0] if issubclass(
            account_class, OptionsAccountInterface) else "broker"
        assert provider == expected, f"{account_class.__name__}.{name}"


def test_alpacas_quote_override_is_tapped_and_the_inherited_inner_tap_stays():
    """AlpacaAccount.get_instrument_current_price (normalises price_type, then super()) escaped the base
    tap: the failing tests/test_replay_capture_host.py case. Now the override is wrapped (the caller's
    identity at depth 0) around the inherited tap (the normalised read at depth 1)."""
    alpaca = _alpaca()
    assert "get_instrument_current_price" in vars(alpaca)
    assert hasattr(alpaca.get_instrument_current_price, "__wrapped__")
    assert tapped_boundary(alpaca.get_instrument_current_price) == ("broker", "get_instrument_current_price")
    assert tapped_boundary(ReadOnlyAccountInterface.get_instrument_current_price) == (
        "broker", "get_instrument_current_price")


# --------------------------------------------------------------------------- #
# The decision path only calls account methods that are tapped or explained
# --------------------------------------------------------------------------- #
_DECISION_FILES = (
    "packages/common/ba2_common/core/TradeConditions.py",
    "packages/common/ba2_common/core/TradeActions.py",
    "packages/common/ba2_common/core/TradeActionEvaluator.py",
    "packages/common/ba2_common/core/TradeRiskManagement.py",
    "packages/common/ba2_common/core/interfaces/MarketExpertInterface.py",
    "packages/common/ba2_common/core/entry_order.py",
    "packages/common/ba2_common/core/trade_cycle.py",
    "packages/common/ba2_common/core/position_sizing.py",
)

#: Account methods the decision code calls that are NOT a sizing read or an effect, with the reason.
_ALLOWED = {
    # option family: the option taps and the option action record cover them
    "get_option_chain": "option tap", "get_option_quote": "option tap",
    "get_atm_implied_volatility": "option tap", "get_option_underlying_price": "option tap",
    "option_capital_equity": "option tap", "check_option_buying_power": "option tap",
    "check_short_put_assignment_capacity": "option tap",
    "submit_option_order": "option effect, noted at the call site by option_action_record.note_submit",
    "close_option_position": "option effect, noted at the call site by option_action_record.note_submit",
    # pure / derived from tapped reads / class constants
    "decision_label": "the decision clock label (OptionsAccountInterface): derived from the recorded clock",
    "records_option_trades": "class constant of the account",
    "equity_shares_per_option_share": "pure arithmetic on the contract multiplier",
    "option_shares_in_equity_units": "pure arithmetic on the contract multiplier",
    "option_reserve_required": "pure classmethod arithmetic",
    "check_cover_for_covered_call": "option family: derived from tapped get_positions",
    "get_iv_rank": "option family: derived from the tapped implied-volatility reads",
    "iv_rank_unavailable_reason": "option family: text explaining get_iv_rank",
    "get_tradable_balance": "derived from the tapped get_balance (margin off) / get_account_snapshot (margin on)",
    "get_tradable_equity": "derived from the tapped get_account_snapshot",
    "get_setting_with_interface_default": "a settings read (DB rows are recorded in pass_state)",
    # looked up with getattr(account, name, None): absent on a live account (backtest-only accessors) or
    # class constants / derived from the tapped reads
    "_as_of_date": "backtest-only accessor; absent on a live account (the lookup answers None)",
    "intraday_volume_so_far": "backtest-only accessor; absent on a live account (the lookup answers None)",
    "option_modelled_half_spread": "option family accessor of the backtest account; absent live",
    "option_order_quantity_limit": "option family accessor of the backtest account; absent live",
    "OPTION_GREEKS_SOURCE": "class declaration of the account (recorded in pass_state account_declarations)",
    "snapshot_is_derived_from_account_info": "class constant of the account",
    "buying_power_is_mandatory": "class constant of the account",
    "get_stock_exposure_headroom": "derived from the tapped get_account_snapshot / get_positions / prices",
    "describe_capital": "description derived from the tapped snapshot / balance reads",
    "_cfg": "a dict of the risk manager's own config (not an account call)",
    "id": "the account's own id (recorded)",
    "_no_exposure_ceiling_warned": "an in-process warning latch on the account object",
    "_create_transaction_for_order": "DB write inside the executed order build, not a broker call",
}

_CALL = re.compile(r"\b(?:self\.)?(?:_?account|acct)\.([A-Za-z_]\w*)\(")
#: ``getattr(account, "get_account_info", None)``: the same call by another spelling (the buying-power clamp).
_GETATTR_CALL = re.compile(r"getattr\((?:self\.)?(?:_?account|acct),\s*[\"']([A-Za-z_]\w*)[\"']")


def test_the_decision_path_only_calls_tapped_or_explained_account_methods():
    tapped = set(SIZING_READ_TAPPED_METHODS) | set(EFFECT_METHODS)
    called = {}
    for relative in _DECISION_FILES:
        with open(os.path.join(_ROOT, relative), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if line.lstrip().startswith("#"):
                    continue
                for name in _CALL.findall(line) + _GETATTR_CALL.findall(line):
                    called.setdefault(name, f"{relative}:{number}")
    unexplained = {name: where for name, where in called.items()
                   if name not in tapped and name not in _ALLOWED}
    assert unexplained == {}, (
        "an account method is called on the decision path that is neither tapped (account_taps) nor "
        f"allowlisted with a reason in this test: {unexplained}")
    stale = sorted(name for name in _ALLOWED if name not in called)
    assert stale == [], f"allowlist entries no longer called on the decision path: {stale}"
