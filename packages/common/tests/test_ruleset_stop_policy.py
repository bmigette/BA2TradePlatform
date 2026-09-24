"""One stop-loss policy for every ruleset path (plan 2026-09-24 Task B4).

Before B4 the ratchet ("a ruleset stop only tightens") lived inside
``AdjustStopLossAction._call_broker``, so it applied to an SL-only rule pass but NOT to the merged
TP+SL branch of ``TradeActionEvaluator``: a pass carrying both a TP and an SL action called
``account.adjust_tp_sl`` with whatever SL it computed, loosening freely. Both now go through
``ruleset_stop_policy``.

The opt-in expert setting ``allow_ruleset_sl_loosen`` (default False) lets a rule loosen the stop
down to the trade's max-loss stop (recorded at entry by B3) and never past it; a trade with no
recorded bound is never loosened.

Pinned here:
  * the policy itself, long and short, setting off and on, including the display fix for two
    prices that round to the same cents;
  * the SL-only action path (unchanged with the setting off);
  * the merged TP+SL path, through the REAL evaluator branch, with a fake account recording what
    ``adjust_tp_sl`` receives: with the setting off a looser SL is REFUSED exactly as on the
    SL-only path, and the TP still applies;
  * the setting's spellings ("1" reads True, a missing setting reads False);
  * the SL min-distance safety still applies to a stop clamped at the bound.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

TA = importlib.import_module("ba2_common.core.TradeActions")
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver
from ba2_common.core.models import ExpertRecommendation, Transaction, TradingOrder
from ba2_common.core.position_sizing import MAX_LOSS_STOP_KEY
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.TradeActions import (
    RULESET_SL_LOOSEN_SETTING, RULESET_STOP_KEPT_REASONS, AdjustStopLossAction,
    AdjustTakeProfitAction, ruleset_sl_loosen_allowed, ruleset_stop_policy,
)
from ba2_common.core.types import (
    OrderDirection, OrderRecommendation, OrderStatus, OrderType, RiskLevel, TimeHorizon,
    TransactionStatus,
)

EXPERT_ID = 4242


class _Expert:
    """Answers ``get_setting_with_interface_default`` from a dict; a missing key reads None,
    which is what the real method returns for a declared setting with no stored value only
    when the declaration has no default -- the policy must read that as False too."""

    def __init__(self, **settings):
        self._settings = settings

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._settings.get(name)


def _on():
    return _Expert(**{RULESET_SL_LOOSEN_SETTING: True})


def _off():
    return _Expert(**{RULESET_SL_LOOSEN_SETTING: False})


def _txn(stop, *, long=True, bound=None, txn_id=7):
    meta = {MAX_LOSS_STOP_KEY: bound} if bound is not None else {}
    return SimpleNamespace(id=txn_id, side=OrderDirection.BUY if long else OrderDirection.SELL,
                           stop_loss=stop, meta_data=meta)


@pytest.fixture
def info_log(monkeypatch):
    """INFO messages the policy logs. Not caplog: the package logger sets propagate=False, so
    caplog's root handler never sees a record (see test_account_seams._capture_errors)."""
    messages = []
    monkeypatch.setattr(TA.logger, "info", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _text(messages):
    return " | ".join(messages)


def _must_not_resolve():
    raise AssertionError("the expert/price must only be resolved on a LOOSENING request")


# =============================================================================================
# The policy: LONG
# =============================================================================================
class TestLongPolicy:
    def test_no_existing_stop_applies_the_request(self):
        assert ruleset_stop_policy(_txn(None), 85.0, True, _must_not_resolve) == (85.0, "no_existing_stop")

    def test_a_tighter_request_applies_without_resolving_the_expert(self):
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 99.0, True, _must_not_resolve,
                                   current_price=_must_not_resolve) == (99.0, "tighter_or_equal")

    def test_an_equal_request_is_applied_not_kept(self):
        """Before the policy an equal request reached ``adjust_sl`` (the account treats it as
        unchanged). It must still, so "kept" never swallows a call that used to happen."""
        price, reason = ruleset_stop_policy(_txn(97.0), 97.0, True, _must_not_resolve)
        assert (price, reason) == (97.0, "tighter_or_equal")
        assert reason not in RULESET_STOP_KEPT_REASONS

    def test_setting_off_a_looser_request_is_the_ratchet_with_todays_log_line(self, info_log):
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, _off()) == (97.0, "ratchet")
        assert ("SL ratchet: keeping existing stop $97.00 for transaction 7 — ruleset asked for "
                "$85.00, which would LOOSEN the long stop") in _text(info_log)

    def test_setting_on_loosening_within_the_bound_is_applied(self, info_log):
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 94.0, True, _on(),
                                   current_price=110.0) == (94.0, "loosen_within_bound")
        assert "$92.0000" in _text(info_log), "the log must name the bound"

    def test_setting_on_loosening_past_the_bound_is_clamped_at_it(self, info_log):
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, _on(),
                                   current_price=110.0) == (92.0, "loosen_clamped")
        for needle in ("$97.00", "$85.00", "$92.0000", "applied=$92.0000"):
            assert needle in _text(info_log)

    def test_setting_on_with_no_bound_recorded_is_refused_and_logged(self, info_log):
        assert ruleset_stop_policy(_txn(97.0), 85.0, True, _on(),
                                   current_price=110.0) == (97.0, "no_max_loss_stop")
        assert "no max-loss stop is recorded" in _text(info_log)

    def test_setting_on_with_an_invalid_bound_is_refused(self):
        """``max_loss_stop_of`` reads an unusable stored value as None: never loosen on it."""
        txn = _txn(97.0)
        txn.meta_data[MAX_LOSS_STOP_KEY] = "garbage"
        assert ruleset_stop_policy(txn, 85.0, True, _on(), current_price=110.0)[1] == "no_max_loss_stop"

    def test_setting_on_an_existing_stop_already_looser_than_the_bound_is_kept(self):
        """Clamping to 92 would move a 90 stop CLOSER to the market -- a tightening performed
        as a side effect of a loosen request. The stop stays at 90."""
        assert ruleset_stop_policy(_txn(90.0, bound=92.0), 85.0, True, _on(),
                                   current_price=110.0) == (90.0, "existing_beyond_bound")

    def test_the_result_is_never_looser_than_the_bound_nor_than_requested(self):
        for requested in (96.5, 94.0, 92.0, 91.99, 85.0, 1.0):
            price, _ = ruleset_stop_policy(_txn(97.0, bound=92.0), requested, True, _on(),
                                           current_price=200.0)
            assert price >= 92.0 and price >= requested, requested


# =============================================================================================
# The policy: SHORT (the mirror)
# =============================================================================================
class TestShortPolicy:
    def test_no_existing_stop_applies_the_request(self):
        assert ruleset_stop_policy(_txn(None, long=False), 115.0, False, _must_not_resolve)[0] == 115.0

    def test_a_tighter_request_applies(self):
        assert ruleset_stop_policy(_txn(103.0, long=False, bound=108.0), 101.0, False,
                                   _must_not_resolve) == (101.0, "tighter_or_equal")

    def test_setting_off_a_looser_request_is_the_ratchet_with_todays_log_line(self, info_log):
        assert ruleset_stop_policy(_txn(103.0, long=False, bound=108.0), 115.0, False,
                                   _off()) == (103.0, "ratchet")
        assert "which would LOOSEN the short stop" in _text(info_log)

    def test_setting_on_loosening_within_the_bound_is_applied(self):
        assert ruleset_stop_policy(_txn(103.0, long=False, bound=108.0), 106.0, False, _on(),
                                   current_price=90.0) == (106.0, "loosen_within_bound")

    def test_setting_on_loosening_past_the_bound_is_clamped_at_it(self):
        assert ruleset_stop_policy(_txn(103.0, long=False, bound=108.0), 115.0, False, _on(),
                                   current_price=90.0) == (108.0, "loosen_clamped")

    def test_setting_on_with_no_bound_recorded_is_refused(self):
        assert ruleset_stop_policy(_txn(103.0, long=False), 115.0, False, _on(),
                                   current_price=90.0) == (103.0, "no_max_loss_stop")

    def test_setting_on_an_existing_stop_already_looser_than_the_bound_is_kept(self):
        assert ruleset_stop_policy(_txn(110.0, long=False, bound=108.0), 115.0, False, _on(),
                                   current_price=90.0) == (110.0, "existing_beyond_bound")

    def test_a_clamp_too_close_to_the_market_is_refused(self):
        # 108 is 1.89% above 106: below the 3% minimum.
        assert ruleset_stop_policy(_txn(103.0, long=False, bound=108.0), 115.0, False, _on(),
                                   current_price=106.0) == (103.0, "bound_too_close")

    def test_the_result_is_never_looser_than_the_bound_nor_than_requested(self):
        for requested in (103.5, 106.0, 108.0, 108.01, 115.0, 1000.0):
            price, _ = ruleset_stop_policy(_txn(103.0, long=False, bound=108.0), requested, False,
                                           _on(), current_price=50.0)
            assert price <= 108.0 and price <= requested, requested


# =============================================================================================
# Safeties that must still hold after a clamp
# =============================================================================================
class TestMinDistanceAfterAClamp:
    """The caller applies the SL min-distance rule (3%, get_min_tp_sl_percent) to the REQUESTED
    price. A clamp replaces it with the bound, which nobody had checked -- so the policy checks
    it, and refuses (keeping the existing stop) rather than pushing it past the bound."""

    def test_a_clamped_stop_within_the_minimum_distance_is_refused(self, info_log):
        # bound 92 is 2.13% under 94.
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, _on(),
                                   current_price=94.0) == (97.0, "bound_too_close")
        assert "below the 3.0% minimum" in _text(info_log)

    def test_a_clamped_stop_through_the_market_is_refused(self):
        """Price has fallen under the bound (the 97 stop has not fired yet): placing a 92 stop
        above a 91 market is the "never a stop above market" trap."""
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, _on(),
                                   current_price=91.0) == (97.0, "bound_too_close")

    def test_a_clamp_with_no_current_price_is_refused(self):
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, _on(),
                                   current_price=lambda: None) == (97.0, "bound_unverifiable")

    def test_a_clamp_exactly_at_the_minimum_distance_is_applied(self):
        price = 92.0 / 0.97   # bound sits exactly 3% under the market
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, _on(),
                                   current_price=price) == (92.0, "loosen_clamped")


# =============================================================================================
# The display fix
# =============================================================================================
def test_two_prices_that_round_to_the_same_cents_are_logged_at_four_decimals(info_log):
    ruleset_stop_policy(_txn(97.0012), 97.0001, True, _off())
    assert "keeping existing stop $97.0012" in _text(info_log)
    assert "ruleset asked for $97.0001" in _text(info_log)


# =============================================================================================
# The setting
# =============================================================================================
class TestTheSetting:
    @pytest.mark.parametrize("raw", [True, 1, "1", '"1"', "true"])
    def test_truthy_spellings_read_true(self, raw):
        """A GA gene arrives as the integer 1; a legacy row holds the JSON string "1"."""
        assert ruleset_sl_loosen_allowed(_Expert(**{RULESET_SL_LOOSEN_SETTING: raw})) is True

    @pytest.mark.parametrize("raw", [False, 0, "0", "false"])
    def test_falsy_spellings_read_false(self, raw):
        assert ruleset_sl_loosen_allowed(_Expert(**{RULESET_SL_LOOSEN_SETTING: raw})) is False

    def test_a_missing_setting_reads_false(self):
        assert ruleset_sl_loosen_allowed(_Expert()) is False

    def test_no_expert_reads_false(self):
        assert ruleset_sl_loosen_allowed(None) is False

    def test_a_garbled_value_raises_rather_than_guessing(self):
        with pytest.raises(ValueError):
            ruleset_sl_loosen_allowed(_Expert(**{RULESET_SL_LOOSEN_SETTING: "maybe"}))

    def test_the_policy_reads_a_string_one_as_on(self):
        expert = _Expert(**{RULESET_SL_LOOSEN_SETTING: "1"})
        assert ruleset_stop_policy(_txn(97.0, bound=92.0), 85.0, True, expert,
                                   current_price=110.0) == (92.0, "loosen_clamped")

    def test_every_expert_declares_it_off_by_default(self):
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
        MarketExpertInterface._ensure_builtin_settings()
        declared = MarketExpertInterface._builtin_settings[RULESET_SL_LOOSEN_SETTING]
        assert declared["type"] == "bool"
        assert declared["default"] is False
        assert declared["required"] is False

    def test_it_is_not_a_forced_backtest_setting(self):
        """A genome may set it: forcing it would overwrite the gene on every trial and deploy."""
        from ba2_common.core.deploy_parity import (
            BACKTEST_FORCED_SETTINGS, BacktestRunFacts, forced_expert_settings)
        facts = BacktestRunFacts(enable_short=True, hold_assigned_stock=False, entry_action=None)
        assert RULESET_SL_LOOSEN_SETTING not in forced_expert_settings(facts)
        assert all(row.key != RULESET_SL_LOOSEN_SETTING for row in BACKTEST_FORCED_SETTINGS)


# =============================================================================================
# Through the actions and the evaluator (session SQLite DB)
# =============================================================================================
class _Resolver:
    def __init__(self, expert):
        self._expert = expert

    def get_expert_instance(self, expert_id):
        return self._expert if expert_id == EXPERT_ID else None

    def get_account_instance(self, account_id):
        raise AssertionError("not used")

    def get_account_instance_from_transaction(self, transaction):
        raise AssertionError("not used")


@pytest.fixture
def resolver():
    previous = get_instance_resolver()

    def install(expert):
        set_instance_resolver(_Resolver(expert))

    yield install
    set_instance_resolver(previous)


class _RecordingAccount:
    """Records every TP/SL call. Current price is fixed; nothing is persisted, so the stored
    transaction keeps the stop the test seeded and every assertion is about the CALL."""

    def __init__(self, current_price=110.0):
        self.id = 91
        self.current_price = current_price
        self.adjust_sl_calls = []
        self.adjust_tp_calls = []
        self.adjust_tp_sl_calls = []

    def get_instrument_current_price(self, symbol, price_type="bid"):
        return self.current_price

    def adjust_sl(self, transaction, new_sl_price, source=""):
        self.adjust_sl_calls.append((transaction.id, new_sl_price, source))
        return True

    def adjust_tp(self, transaction, new_tp_price, source=""):
        self.adjust_tp_calls.append((transaction.id, new_tp_price, source))
        return True

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        self.adjust_tp_sl_calls.append((transaction.id, new_tp_price, new_sl_price, source))
        return True


def _position(account_id, *, long=True, stop, bound):
    """An OPEN position at entry $100 with a protective stop and (optionally) a max-loss stop."""
    side = OrderDirection.BUY if long else OrderDirection.SELL
    rec_id = add_instance(ExpertRecommendation(
        instance_id=EXPERT_ID, symbol="AAPL",
        recommended_action=OrderRecommendation.BUY if long else OrderRecommendation.SELL,
        expected_profit_percent=10.0, price_at_date=100.0, details=None, confidence=80.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM))
    meta = {MAX_LOSS_STOP_KEY: bound} if bound is not None else {}
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=10.0, side=side, status=TransactionStatus.OPENED,
        open_price=100.0, take_profit=None, stop_loss=stop, meta_data=meta))
    order_id = add_instance(TradingOrder(
        account_id=account_id, symbol="AAPL", quantity=10.0, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
        open_price=100.0, expert_recommendation_id=rec_id))
    return txn_id, order_id, rec_id


def _sl_action(account, order, rec, *, long=True, percent=-15.0):
    return AdjustStopLossAction(
        "AAPL", account, OrderRecommendation.BUY if long else OrderRecommendation.SELL,
        existing_order=order, expert_recommendation=rec,
        reference_value="order_open_price", percent=percent)


def _tp_action(account, order, rec, *, long=True, percent=20.0):
    return AdjustTakeProfitAction(
        "AAPL", account, OrderRecommendation.BUY if long else OrderRecommendation.SELL,
        existing_order=order, expert_recommendation=rec,
        reference_value="order_open_price", percent=percent)


def _run_sl_only(account, *, long=True, stop, bound, percent=-15.0):
    txn_id, order_id, rec_id = _position(account.id, long=long, stop=stop, bound=bound)
    order = get_instance(TradingOrder, order_id)
    rec = get_instance(ExpertRecommendation, rec_id)
    result = _sl_action(account, order, rec, long=long, percent=percent).execute()
    return txn_id, result


def _run_combined(account, *, long=True, stop, bound, sl_percent=-15.0, tp_percent=20.0):
    """Drive the REAL merged TP+SL branch of TradeActionEvaluator.execute (open_positions)."""
    txn_id, order_id, rec_id = _position(account.id, long=long, stop=stop, bound=bound)
    order = get_instance(TradingOrder, order_id)
    rec = get_instance(ExpertRecommendation, rec_id)
    evaluator = TradeActionEvaluator(account=account, instrument_name="AAPL",
                                     existing_transactions=[get_instance(Transaction, txn_id)])
    evaluator.expert_recommendation = rec
    evaluator.trade_actions = [_tp_action(account, order, rec, long=long, percent=tp_percent),
                               _sl_action(account, order, rec, long=long, percent=sl_percent)]
    results = evaluator.execute()
    return txn_id, results


class TestSlOnlyPath:
    def test_setting_off_the_ratchet_is_unchanged(self, resolver):
        resolver(_off())
        account = _RecordingAccount()
        txn_id, result = _run_sl_only(account, stop=97.0, bound=92.0)
        assert account.adjust_sl_calls == [], "a looser ruleset stop must not reach the broker"
        assert result["success"] is True
        assert result["data"]["new_sl_price"] == pytest.approx(97.0)

    def test_setting_off_a_tighter_stop_is_sent(self, resolver):
        resolver(_off())
        account = _RecordingAccount()
        txn_id, _ = _run_sl_only(account, stop=90.0, bound=92.0, percent=-5.0)
        assert account.adjust_sl_calls == [(txn_id, pytest.approx(95.0), "ruleset")]

    def test_setting_off_with_no_expert_resolved_is_the_ratchet(self, resolver):
        resolver(None)
        account = _RecordingAccount()
        _run_sl_only(account, stop=97.0, bound=92.0)
        assert account.adjust_sl_calls == []

    def test_setting_on_past_the_bound_sends_the_bound(self, resolver):
        resolver(_on())
        account = _RecordingAccount()
        txn_id, result = _run_sl_only(account, stop=97.0, bound=92.0)
        assert account.adjust_sl_calls == [(txn_id, pytest.approx(92.0), "ruleset")]
        assert result["data"]["new_sl_price"] == pytest.approx(92.0)

    def test_setting_on_the_min_distance_still_applies_after_a_clamp(self, resolver):
        """Market at 94: the -15% request (85) passes the 3% rule, the bound (92) does not."""
        resolver(_on())
        account = _RecordingAccount(current_price=94.0)
        _, result = _run_sl_only(account, stop=97.0, bound=92.0)
        assert account.adjust_sl_calls == []
        assert result["data"]["new_sl_price"] == pytest.approx(97.0)

    def test_setting_on_short_past_the_bound_sends_the_bound(self, resolver):
        resolver(_on())
        account = _RecordingAccount(current_price=90.0)
        txn_id, _ = _run_sl_only(account, long=False, stop=103.0, bound=108.0)
        assert account.adjust_sl_calls == [(txn_id, pytest.approx(108.0), "ruleset")]

    def test_setting_off_short_ratchet(self, resolver):
        resolver(_off())
        account = _RecordingAccount(current_price=90.0)
        _run_sl_only(account, long=False, stop=103.0, bound=108.0)
        assert account.adjust_sl_calls == []


class TestCombinedPath:
    """THE GAP. The merged branch now applies the same policy; the TP half is unaffected."""

    def test_setting_off_a_looser_sl_is_refused_and_the_tp_still_applies(self, resolver):
        resolver(_off())
        account = _RecordingAccount()
        txn_id, results = _run_combined(account, stop=97.0, bound=92.0)
        assert account.adjust_tp_sl_calls == [(txn_id, pytest.approx(120.0), None, "ruleset")], (
            "the SL half must be None (don't adjust) and the TP must still be sent")
        merged, = [r for r in results if (r.get("data") or {}).get("sl_policy")]
        assert merged["success"] is True
        assert merged["data"]["sl_price"] == pytest.approx(97.0)
        assert merged["data"]["sl_requested"] == pytest.approx(85.0)
        assert merged["data"]["sl_policy"] == "ratchet"

    def test_the_combined_outcome_matches_the_sl_only_outcome(self, resolver):
        """Same transaction state, same rule, both branches: the SL that stands is the same."""
        resolver(_off())
        sl_only = _RecordingAccount()
        _, sl_result = _run_sl_only(sl_only, stop=97.0, bound=92.0)
        combined = _RecordingAccount()
        _, results = _run_combined(combined, stop=97.0, bound=92.0)
        merged, = [r for r in results if (r.get("data") or {}).get("sl_policy")]
        assert sl_only.adjust_sl_calls == [] and combined.adjust_tp_sl_calls[0][2] is None
        assert merged["data"]["sl_price"] == pytest.approx(sl_result["data"]["new_sl_price"])

    def test_setting_off_a_tighter_sl_is_sent(self, resolver):
        resolver(_off())
        account = _RecordingAccount()
        txn_id, _ = _run_combined(account, stop=90.0, bound=92.0, sl_percent=-5.0)
        assert account.adjust_tp_sl_calls == [(txn_id, pytest.approx(120.0), pytest.approx(95.0), "ruleset")]

    def test_no_existing_stop_the_entry_bracket_is_sent_unchanged(self, resolver):
        """The common live case (53 of 57 merged calls in the 2026-09-24 prod audit): an entry
        bracket on a transaction with no stop yet."""
        resolver(_off())
        account = _RecordingAccount()
        txn_id, _ = _run_combined(account, stop=None, bound=None)
        assert account.adjust_tp_sl_calls == [(txn_id, pytest.approx(120.0), pytest.approx(85.0), "ruleset")]

    def test_setting_on_past_the_bound_sends_the_bound(self, resolver):
        resolver(_on())
        account = _RecordingAccount()
        txn_id, _ = _run_combined(account, stop=97.0, bound=92.0)
        assert account.adjust_tp_sl_calls == [(txn_id, pytest.approx(120.0), pytest.approx(92.0), "ruleset")]

    def test_setting_on_no_bound_is_refused(self, resolver):
        resolver(_on())
        account = _RecordingAccount()
        txn_id, _ = _run_combined(account, stop=97.0, bound=None)
        assert account.adjust_tp_sl_calls == [(txn_id, pytest.approx(120.0), None, "ruleset")]

    def test_setting_on_existing_beyond_the_bound_is_kept(self, resolver):
        resolver(_on())
        account = _RecordingAccount()
        txn_id, _ = _run_combined(account, stop=90.0, bound=92.0)
        assert account.adjust_tp_sl_calls == [(txn_id, pytest.approx(120.0), None, "ruleset")]

    def test_short_mirror(self, resolver):
        resolver(_off())
        off = _RecordingAccount(current_price=90.0)
        txn_id, _ = _run_combined(off, long=False, stop=103.0, bound=108.0,
                                  sl_percent=-15.0, tp_percent=20.0)
        assert off.adjust_tp_sl_calls == [(txn_id, pytest.approx(80.0), None, "ruleset")]
        resolver(_on())
        on = _RecordingAccount(current_price=90.0)
        txn_id, _ = _run_combined(on, long=False, stop=103.0, bound=108.0,
                                  sl_percent=-15.0, tp_percent=20.0)
        assert on.adjust_tp_sl_calls == [(txn_id, pytest.approx(80.0), pytest.approx(108.0), "ruleset")]


def test_both_call_sites_use_the_one_policy():
    """A second copy of the ratchet is how the gap happened. Pin that neither site grows one."""
    import inspect
    from ba2_common.core import TradeActionEvaluator as TAE
    assert "ruleset_stop_policy(" in inspect.getsource(TA.AdjustStopLossAction._call_broker)
    assert "ruleset_stop_policy(" in inspect.getsource(TAE.TradeActionEvaluator.execute)
    assert "LOOSEN" not in inspect.getsource(TA.AdjustStopLossAction._call_broker)
