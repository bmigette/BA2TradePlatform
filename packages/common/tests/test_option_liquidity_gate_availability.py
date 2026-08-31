"""A liquidity gate whose field the data source never publishes must be a LOUD config
error, not a silent zero-result (2026-08-23).

WHY: ``passes_liquidity`` fails CLOSED on ``None`` — correct when the field is published
and this one contract lacks it, catastrophic when NO contract publishes it. Measured
against the real options cache: ``option_chain.open_interest`` is NULL for all
1,440,782 rows, so ``min_open_interest=100`` — the LIVE UI DEFAULT, present on all 14 live
option entry actions — rejected 16/16 structures on 16/16 symbol-date-capital combinations
and reported "No liquid <structure>", indistinguishable from a genuinely illiquid chain.

The fix is tri-state: the gate stays fail-closed per contract (an illiquid contract must
never slip through), but a gate the CHAIN cannot answer at all raises
``OptionLiquidityDataUnavailable`` naming the field.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core import option_selector
from ba2_common.core.option_selector import (
    OptionLiquidityDataMissingToday,
    OptionLiquidityDataUnavailable,
    OptionSelectionConfigError,
    check_liquidity_data_available,
)


@pytest.fixture(autouse=True)
def _forget_which_fields_have_been_seen():
    """``_FIELDS_SEEN_PUBLISHED`` is process-wide by design (it is how a source that never
    publishes a field is told from one whose fetch missed it). Tests must not inherit each
    other's evidence, or which exception they get depends on collection order."""
    option_selector._FIELDS_SEEN_PUBLISHED.clear()
    yield
    option_selector._FIELDS_SEEN_PUBLISHED.clear()
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import ExpertActionType, OptionRight

TODAY = date(2024, 6, 3)
EXP = date(2024, 7, 19)


def _c(strike, *, oi=None, volume=None, bid=1.20, ask=1.30, right=OptionRight.CALL):
    return OptionContract(
        symbol=f"XYZ240719{right.value[0].upper()}{int(strike * 1000):08d}",
        underlying="XYZ", option_type=right, strike=float(strike), expiry=EXP,
        bid=bid, ask=ask, last=1.25, open_interest=oi, volume=volume)


# --------------------------------------------------------------------------- #
# the probe itself
# --------------------------------------------------------------------------- #
def test_open_interest_gate_on_a_chain_that_never_publishes_it_raises():
    chain = [_c(95.0), _c(100.0), _c(105.0)]          # open_interest NULL everywhere
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, min_open_interest=100, underlying="XYZ")
    assert ei.value.field == "open_interest"
    assert "open_interest" in str(ei.value) and "XYZ" in str(ei.value)


def test_volume_gate_on_a_chain_that_never_publishes_it_raises():
    chain = [_c(95.0, oi=500), _c(100.0, oi=500)]
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, min_volume=25, underlying="XYZ")
    assert ei.value.field == "volume"


def test_spread_gate_on_a_chain_with_no_two_sided_quotes_raises():
    chain = [_c(95.0, bid=None, ask=None), _c(100.0, bid=None, ask=None)]
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")
    assert ei.value.field == "spread"


def test_gate_is_evaluable_when_even_ONE_contract_publishes_the_field():
    """Partial publication is a real liquidity signal, not missing data: the contracts that
    do NOT publish stay fail-closed (rejected), exactly as before."""
    chain = [_c(95.0), _c(100.0, oi=800), _c(105.0)]
    check_liquidity_data_available(chain, min_open_interest=100, underlying="XYZ")  # no raise


def test_gates_that_are_off_are_never_probed():
    chain = [_c(95.0), _c(100.0)]                     # nothing published at all
    check_liquidity_data_available(chain, min_open_interest=None, min_volume=None,
                                   max_spread_pct=None, underlying="XYZ")


def test_empty_chain_is_not_a_gate_error():
    """An empty chain is its own (already-reported) condition — do not mislabel it."""
    check_liquidity_data_available([], min_open_interest=100, min_volume=25,
                                   max_spread_pct=15.0, underlying="XYZ")


def test_the_error_is_a_selection_config_error():
    assert issubclass(OptionLiquidityDataUnavailable, OptionSelectionConfigError)
    assert issubclass(OptionSelectionConfigError, ValueError)


# --------------------------------------------------------------------------- #
# a field that is PRESENT but constant-zero is a placeholder, not published data
# --------------------------------------------------------------------------- #
def test_a_chain_whose_every_quote_is_zero_width_does_not_publish_a_spread():
    """THE PLACEHOLDER CASE. Measured read-only on the real cache:
    ``SELECT sum(bid<>ask) FROM option_chain`` -> 0 across all 1,440,782 rows, so every
    quoted contract has bid == ask and ``spread_pct`` is exactly 0.0. That is non-None, so
    a ``is not None`` probe green-lights the gate — after which ``max_spread_pct`` measures
    nothing (0 <= any ceiling) yet still rejects every contract with no two-sided quote
    (357,211 of those rows). A field the source writes the same constant into for the
    whole dataset is a placeholder, and must be treated exactly like an absent one."""
    chain = [_c(95.0, bid=1.25, ask=1.25), _c(100.0, bid=2.50, ask=2.50),
             _c(105.0, bid=0.80, ask=0.80)]
    assert [c.spread_pct for c in chain] == [0.0, 0.0, 0.0]      # present, and useless
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")
    assert ei.value.field == "spread"


def test_zero_width_quotes_mixed_with_unquoted_contracts_still_do_not_publish():
    """The mix that actually occurs in the cache: 36% of rows have no quote at all and the
    rest are zero-width. Neither kind carries a spread."""
    chain = [_c(95.0, bid=None, ask=None), _c(100.0, bid=2.50, ask=2.50)]
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")
    assert ei.value.field == "spread"


def test_one_genuinely_two_sided_quote_makes_the_spread_field_available():
    """Same partial-publication rule as the other fields: one real spread is evidence the
    source publishes spreads, and the zero-width peers keep passing the gate (0 <= any
    ceiling) exactly as before. The check must not become a majority vote."""
    chain = [_c(95.0, bid=1.25, ask=1.25), _c(100.0, bid=2.40, ask=2.60)]
    check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")  # no raise


def test_the_zero_check_short_circuits_on_the_first_real_spread():
    """No full scan on the healthy path: the probe stops at the first publishing contract,
    so a chain whose head quotes two-sided costs O(1), not O(len(chain))."""
    seen = []

    class _Spy(OptionContract):
        @property
        def spread_pct(self):
            seen.append(self.strike)
            return OptionContract.spread_pct.fget(self)

    chain = [_Spy(symbol="A", underlying="XYZ", option_type=OptionRight.CALL, strike=float(s),
                  expiry=EXP, bid=2.40, ask=2.60, last=2.50) for s in range(1, 501)]
    check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")
    assert seen == [1.0], "the availability probe scanned past the first real spread"


def test_a_wholly_crossed_chain_is_reported_rather_than_rejecting_everything():
    """ask < bid gives a NEGATIVE spread_pct, which ``passes_liquidity`` refuses (sp < 0).
    If that were the whole chain the gate would again reject 100% in silence, so it counts
    as 'no usable spread published' too."""
    chain = [_c(95.0, bid=1.40, ask=1.20), _c(100.0, bid=2.60, ask=2.40)]
    assert all(c.spread_pct < 0 for c in chain)
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")
    assert ei.value.field == "spread"


def test_an_all_zero_volume_chain_is_a_MARKET_verdict_not_missing_data():
    """The constant-zero rule is SPECIFIC to the derived spread and must not leak.

    ``volume`` and ``open_interest`` are observed quantities: 0 means "nobody traded it",
    which is a real, discriminating fact about a contract and precisely what the gate is
    there to reject. ``spread_pct`` is DERIVED from two columns; 0 across a whole chain
    means bid and ask hold the same number, which is not a market anyone quoted.
    ``HistoricalOptionsProvider`` deliberately coerces a missing bar's volume to a known 0
    (see testplatform test_options_provider_volume_is_known_zero) — turning that into a
    config error would call a quiet day a broken data source."""
    chain = [_c(95.0, volume=0), _c(100.0, volume=0)]
    check_liquidity_data_available(chain, min_volume=1, underlying="XYZ")        # no raise
    chain = [_c(95.0, oi=0), _c(100.0, oi=0)]
    check_liquidity_data_available(chain, min_open_interest=100, underlying="XYZ")


def test_the_spread_gate_still_fails_CLOSED_on_a_contract_it_cannot_measure():
    """THE OTHER HALF OF THE TRI-STATE, and the half with no test until now: making the
    availability probe stricter must not soften the PER-CONTRACT rule.

    Mutating ``sp is None or sp < 0 or ...`` to let an unmeasurable contract through
    survived the entire 1041-test package suite, so the fail-closed behaviour the whole
    design rests on was resting on nothing. It is the point of the gate: a contract nobody
    is quoting two-sided is the illiquid one, and "I could not measure it" must never be
    read as "it passed"."""
    from ba2_common.core.option_selector import passes_liquidity

    no_quote = _c(95.0, bid=None, ask=None)               # last=1.25, so not a penny reject
    assert no_quote.spread_pct is None
    assert passes_liquidity(no_quote, None, None, None) is True      # gate off: not our call
    assert passes_liquidity(no_quote, None, 15.0, None) is False     # gate on: refused

    crossed = _c(95.0, bid=1.40, ask=1.20)                # ask below bid
    assert crossed.spread_pct < 0
    assert passes_liquidity(crossed, None, 15.0, None) is False

    tight, wide = _c(95.0, bid=1.20, ask=1.30), _c(95.0, bid=1.00, ask=1.60)
    assert passes_liquidity(tight, None, 15.0, None) is True
    assert passes_liquidity(wide, None, 15.0, None) is False


def test_a_zero_width_chain_is_not_let_through_by_the_spread_gate():
    """Fail-OPEN guard. The fix must not turn "the source publishes no spread" into "the
    spread gate passes": every one of these contracts reports 0.0 <= 15.0."""
    from ba2_common.core.option_selector import passes_liquidity

    chain = [_c(95.0, bid=1.25, ask=1.25), _c(100.0, bid=2.50, ask=2.50)]
    assert all(passes_liquidity(c, None, 15.0, None) for c in chain)   # ... individually
    with pytest.raises(OptionLiquidityDataUnavailable):                # ... but not as a set
        check_liquidity_data_available(chain, max_spread_pct=15.0, underlying="XYZ")


# --------------------------------------------------------------------------- #
# end to end through a real option entry action
# --------------------------------------------------------------------------- #
from ba2_common.core.TradeActions import create_action                    # noqa: E402
from ba2_common.core.interfaces.OptionsAccountInterface import (          # noqa: E402
    OptionsAccountInterface,
)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "liqgate.sqlite"))
    db.init_db()
    yield


class _Acct(OptionsAccountInterface):
    """Chain shaped like the real historical cache: quotes present, OI and volume NULL."""

    def __init__(self, *, oi=None, volume=None):
        self.id = 1
        self._oi = oi
        self._vol = volume
        self.submitted = []

    def _as_of_date(self):
        return date(2024, 6, 1)

    def get_balance(self):
        return 100_000.0

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_current_price(self, symbol=None):
        return 100.0

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            otm = abs(float(s) - 100.0)
            bid = max(0.2, 5.0 - 0.08 * otm)
            out.append(OptionContract(
                symbol=f"{underlying}{s}{option_type.value[0].upper()}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=self._oi, volume=self._vol))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        order = SimpleNamespace(id=len(self.submitted) + 1, data={})
        self.submitted.append(option_strategy)
        return order

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 100_000.0


_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


def _run(action_type, acct, **kw):
    act = create_action(ExpertActionType(action_type), "AAPL", acct, SimpleNamespace(),
                        None, _REC, **kw)
    act.submit_to_broker = True
    return act.execute()


def test_live_default_min_oi_on_an_oi_less_chain_reports_the_real_cause():
    """The live UI default (min_open_interest=100) against a cache-shaped chain used to say
    'No liquid call contract' — which reads as 'the market is thin'. It must now name the
    missing FIELD, and must not silently look like a normal no-selection."""
    res = _run("buy_call", _Acct(oi=None), strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is False
    assert "open_interest" in res["message"]
    assert "No liquid" not in res["message"]


def test_the_same_rule_trades_normally_once_the_chain_publishes_open_interest():
    acct = _Acct(oi=1000)
    res = _run("buy_call", acct, strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is True, res["message"]
    assert acct.submitted == ["long_call"]


def test_a_published_but_low_open_interest_is_still_rejected_by_the_gate():
    """The gate must NOT become fail-open: a contract that publishes OI below the floor is
    still refused."""
    res = _run("buy_call", _Acct(oi=5), strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is False
    assert "No liquid" in res["message"]


# --------------------------------------------------------------------------- #
# the availability check is scoped to the chain that is actually filtered
# --------------------------------------------------------------------------- #
class _SidedAcct(_Acct):
    """A source that publishes a liquidity field on ONE side of the chain only.

    Real and unremarkable: calls and puts are separate fetches, and a vendor can answer one
    and not the other (an underlying whose puts are barely quoted, a partial snapshot page)."""

    def __init__(self, *, call_oi=None, put_oi=None):
        super().__init__()
        self._call_oi = call_oi
        self._put_oi = put_oi

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        oi = self._call_oi if option_type == OptionRight.CALL else self._put_oi
        chain = super().get_option_chain(underlying, expiry_min, expiry_max, option_type,
                                         strike_min, strike_max)
        for c in chain:
            c.open_interest = oi
        return chain


def test_a_publishing_CALL_chain_does_not_vouch_for_a_silent_PUT_chain():
    """POOLING THE TWO SIDES RE-ARMS THE SILENT 100% REJECTION.

    ``_liq`` flattened call+put into one ``universe`` list, so ONE publishing contract
    anywhere satisfied the probe for both. Here every call publishes open_interest and no
    put does: the pooled check passed, the call leg selected, and every put was then
    fail-closed out — reported as "No liquid ATM put", i.e. as a thin market, which is the
    exact silent-rejection the availability check exists to abolish. The check has to be
    meaningful for the chain each leg is really selected from."""
    res = _run("open_straddle", _SidedAcct(call_oi=1000, put_oi=None),
               strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
               sizing=50.0, min_open_interest=100)
    assert res["success"] is False
    assert "open_interest" in res["message"]
    assert "No liquid" not in res["message"]


def test_a_publishing_PUT_chain_does_not_vouch_for_a_silent_CALL_chain():
    """The mirror image, so the fix cannot be a one-sided special case."""
    res = _run("open_straddle", _SidedAcct(call_oi=None, put_oi=1000),
               strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
               sizing=50.0, min_open_interest=100)
    assert res["success"] is False
    assert "open_interest" in res["message"]
    assert "No liquid" not in res["message"]


def test_both_sides_publishing_still_trades_normally():
    """Positive control: per-chain scoping must not turn a healthy two-sided fetch into an
    error (that would be the gate failing the other way — refusing valid trades)."""
    acct = _SidedAcct(call_oi=1000, put_oi=1000)
    res = _run("open_straddle", acct, strike_method="percent_otm", strike_param=0.0,
               dte_min=10, dte_max=40, sizing=50.0, min_open_interest=100)
    assert res["success"] is True, res["message"]
    assert acct.submitted == ["straddle"]


def test_a_thin_but_publishing_put_side_is_still_a_market_verdict():
    """Fail-OPEN guard for the per-chain scoping: when the put chain DOES publish and is
    genuinely below the floor, that stays a rejection, not a config error."""
    res = _run("open_straddle", _SidedAcct(call_oi=1000, put_oi=5),
               strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
               sizing=50.0, min_open_interest=100)
    assert res["success"] is False
    assert "No liquid" in res["message"]
    assert "open_interest" not in res["message"]


# --------------------------------------------------------------------------- #
# "this source never publishes it" vs "today's fetch lacked it"
# --------------------------------------------------------------------------- #
def _capture_logs(monkeypatch):
    """Collect TradeActions' logger.error / logger.warning calls. NOT caplog."""
    from ba2_common.core import TradeActions as TA

    errors, warnings = [], []
    monkeypatch.setattr(TA.logger, "error", lambda m, *a, **k: errors.append(str(m)))
    monkeypatch.setattr(TA.logger, "warning", lambda m, *a, **k: warnings.append(str(m)))
    return errors, warnings


def test_a_field_the_source_has_never_published_stays_a_loud_config_error(monkeypatch):
    """The structural case (the cache's all-NULL open_interest): the advice to change the
    configuration is correct, and ERROR is the right volume for it."""
    errors, warnings = _capture_logs(monkeypatch)
    res = _run("buy_call", _Acct(oi=None), strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is False
    assert "Clear this gate" in res["message"]
    assert len(errors) == 1 and "open_interest" in errors[0]
    assert warnings == []


def test_a_field_the_source_HAS_published_before_is_a_transient_gap(monkeypatch):
    """The live case. Alpaca types ``open_interest`` Optional and a snapshot page can come
    back without it. Once the source has demonstrably published the field, a later empty
    fetch is a data gap — so: no "clear this gate" advice, and WARNING rather than an ERROR
    logged once per symbol per day."""
    good, blank = _Acct(oi=1000), _Acct(oi=None)
    assert type(good) is type(blank)                       # the SAME source, twice

    ok = _run("buy_call", good, strike_method="percent_otm", strike_param=2.0,
              dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert ok["success"] is True, ok["message"]            # ... it publishes; now it doesn't

    errors, warnings = _capture_logs(monkeypatch)
    res = _run("buy_call", blank, strike_method="percent_otm", strike_param=2.0,
               dte_min=20, dte_max=40, sizing=20.0, min_open_interest=100)
    assert res["success"] is False                         # still refuses to trade blind
    assert "transient" in res["message"]
    assert "Clear this gate" not in res["message"]
    assert errors == [], "a transient broker gap must not log an ERROR per symbol-day"
    assert len(warnings) == 1 and "open_interest" in warnings[0]


def test_the_transient_variant_is_still_a_selection_config_error():
    """Callers catch the base class; the subclass must not escape ``execute``'s handler."""
    assert issubclass(OptionLiquidityDataMissingToday, OptionLiquidityDataUnavailable)
    assert issubclass(OptionLiquidityDataMissingToday, OptionSelectionConfigError)


def test_evidence_from_one_source_does_not_vouch_for_another():
    """The memo is keyed by source: a backtest cache publishing volume says nothing about
    what a live broker publishes, and vice versa."""
    published = [_c(95.0, oi=800)]
    blank = [_c(95.0)]
    check_liquidity_data_available(published, min_open_interest=100, source="AlpacaAccount")
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(blank, min_open_interest=100, source="BacktestAccount")
    assert type(ei.value) is OptionLiquidityDataUnavailable      # not the transient subclass
    with pytest.raises(OptionLiquidityDataMissingToday):
        check_liquidity_data_available(blank, min_open_interest=100, source="AlpacaAccount")


def test_a_source_that_publishes_a_real_spread_once_gets_the_transient_reading_after():
    """The evidence rule applies to the zero-width spread too: a cache that has only ever
    emitted bid == ask never counts as having published one."""
    zero_width = [_c(95.0, bid=1.25, ask=1.25)]
    with pytest.raises(OptionLiquidityDataUnavailable) as ei:
        check_liquidity_data_available(zero_width, max_spread_pct=15.0, source="Cache")
    assert type(ei.value) is OptionLiquidityDataUnavailable
    check_liquidity_data_available([_c(95.0, bid=1.20, ask=1.30)], max_spread_pct=15.0,
                                   source="Cache")
    with pytest.raises(OptionLiquidityDataMissingToday):
        check_liquidity_data_available(zero_width, max_spread_pct=15.0, source="Cache")


# --------------------------------------------------------------------------- #
# DTE window
# --------------------------------------------------------------------------- #
def test_dte_max_none_is_a_config_error_not_an_empty_chain():
    """dte_min=30 with dte_max unset built the INVERTED fetch window [today+30, today] and
    reported 'Empty option chain' — a data problem, when it is a config problem."""
    res = _run("buy_call", _Acct(oi=1000), strike_method="percent_otm", strike_param=2.0,
               dte_min=30, dte_max=None, sizing=20.0)
    assert res["success"] is False
    assert "dte_max" in res["message"]
    assert "Empty option chain" not in res["message"]


def test_dte_min_greater_than_dte_max_is_a_config_error():
    res = _run("buy_call", _Acct(oi=1000), strike_method="percent_otm", strike_param=2.0,
               dte_min=45, dte_max=20, sizing=20.0)
    assert res["success"] is False
    assert "dte_min" in res["message"] and "dte_max" in res["message"]
