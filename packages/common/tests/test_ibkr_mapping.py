"""Pure IBKR mapping rules (no ib_async, no network): status table totality, error classification,
OCC symbols, order ids, time in force, price ticks, account-value selection and the snapshot maths."""
from datetime import date

import pytest

from ba2_common.core import ibkr_mapping as M
from ba2_common.core.types import BrokerOrderErrorReason as R
from ba2_common.core.types import OptionRight, OrderStatus


class TestStatusTable:
    #: Every spelling ib_async / the TWS API can put in ``OrderStatus.status``. If ib_async grows a
    #: new one, ``test_every_ib_async_status_is_mapped`` (adapter-side test) fails first.
    ALL = ["ApiPending", "PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted", "ApiUpdate",
           "Filled", "Cancelled", "ApiCancelled", "Inactive", "ValidationError"]

    def test_every_known_status_maps_to_a_platform_status(self):
        for status in self.ALL:
            assert isinstance(M.map_ib_status(status), OrderStatus)
        assert set(self.ALL) == set(M.IB_STATUS_STRINGS)

    def test_the_mapping(self):
        assert M.map_ib_status("PendingSubmit") == OrderStatus.PENDING_NEW
        assert M.map_ib_status("ApiPending") == OrderStatus.PENDING_NEW
        assert M.map_ib_status("PendingCancel") == OrderStatus.PENDING_CANCEL
        assert M.map_ib_status("PreSubmitted") == OrderStatus.ACCEPTED
        assert M.map_ib_status("Submitted") == OrderStatus.ACCEPTED
        assert M.map_ib_status("Filled") == OrderStatus.FILLED
        assert M.map_ib_status("Cancelled") == OrderStatus.CANCELED
        assert M.map_ib_status("ApiCancelled") == OrderStatus.CANCELED
        assert M.map_ib_status("Inactive") == OrderStatus.REJECTED

    @pytest.mark.parametrize("bad", [None, "", "Teleported", "filled", "SUBMITTED"])
    def test_unknown_raises_loudly_never_defaults(self, bad):
        with pytest.raises(M.UnknownIBOrderStatus):
            M.map_ib_status(bad)

    def test_submitted_is_refined_by_the_quantities(self):
        assert M.map_ib_status("Submitted", filled=0, remaining=10) == OrderStatus.ACCEPTED
        assert M.map_ib_status("Submitted", filled=4, remaining=6) == OrderStatus.PARTIALLY_FILLED
        assert M.map_ib_status("Submitted", filled=10, remaining=0) == OrderStatus.FILLED
        assert M.map_ib_status("PreSubmitted", filled=4, remaining=6) == OrderStatus.PARTIALLY_FILLED

    def test_a_cancelled_order_stays_cancelled_whatever_it_filled(self):
        assert M.map_ib_status("Cancelled", filled=4, remaining=6) == OrderStatus.CANCELED

    def test_final_and_ack_sets(self):
        assert M.IB_FINAL_STATUSES == {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
        assert {"Submitted", "PreSubmitted", "Filled"} <= M.IB_ACK_STATUSES
        assert "PendingSubmit" not in M.IB_ACK_STATUSES


class TestNumbers:
    @pytest.mark.parametrize("raw", [None, float("nan"), float("inf"), 1.7976931348623157e308, "x"])
    def test_ib_unset_values_are_none(self, raw):
        assert M.ib_number(raw) is None

    def test_real_numbers_pass(self):
        assert M.ib_number(0.0) == 0.0 and M.ib_number("12.5") == 12.5 and M.ib_number(3) == 3.0


class TestErrors:
    def test_severity_buckets(self):
        assert M.error_severity(2104) == "info" and M.error_severity(2158) == "info"
        assert M.error_severity(1100) == "connection_lost"
        assert M.error_severity(1102) == "connection_restored"
        assert M.error_severity(354) == "market_data" and M.error_severity(10089) == "market_data"
        assert M.error_severity(202) == "cancelled"
        assert M.error_severity(201) == "order" and M.error_severity(103) == "order"

    @pytest.mark.parametrize("code", [105, 110, 165, 321, 329, 399, 404, 434, 492, 10349, 2100, 2119, 2148,
                                      2161, 2199])
    def test_every_ib_async_warning_is_a_warning(self, code):
        """ib_async (wrapper.py warningCodes + 2100-2199): the order stays live. Never a rejection."""
        assert M.error_severity(code) in ("order_warning", "info")

    def test_rejection_is_decided_from_the_status(self):
        assert M.IB_REJECTION_STATUSES == {"Cancelled", "ApiCancelled", "Inactive"}
        assert "ValidationError" not in M.IB_REJECTION_STATUSES

    def test_201_is_classified_by_its_text(self):
        c = M.classify_ib_error
        assert c(201, "Order rejected - reason: Insufficient margin") == R.INSUFFICIENT_FUNDS
        assert c(201, "Order rejected - available funds are not enough") == R.INSUFFICIENT_FUNDS
        assert c(201, "Order rejected - no shares available to short") == R.INSUFFICIENT_QTY
        assert c(201, "Order rejected - cross trade") == R.WASH_TRADE
        assert c(201, "stop price is through the market price") == R.STOP_THROUGH_MARKET
        assert c(201, "something nobody has seen") == R.UNKNOWN

    def test_other_codes(self):
        assert M.classify_ib_error(200, "No security definition has been found") == R.INVALID_SYMBOL
        assert M.classify_ib_error(321, "API interface is currently in Read-Only mode") == R.UNAUTHORIZED
        assert M.classify_ib_error(321, "Validation error") == R.UNKNOWN
        assert M.classify_ib_error(110, "The price does not conform to the minimum price variation") == R.UNKNOWN
        assert M.classify_ib_error(326, "client id in use") == R.UNKNOWN
        assert M.classify_ib_error(None, None) == R.UNKNOWN


class TestSymbols:
    def test_share_class_round_trip(self):
        assert M.to_ib_symbol("brk.b") == "BRK B" and M.from_ib_symbol("BRK B") == "BRK.B"
        assert M.from_ib_symbol(M.to_ib_symbol("BF.B")) == "BF.B"

    def test_occ_round_trip(self):
        root, expiry, right, strike = M.parse_occ("AAPL260116C00150000")
        assert (root, expiry, right, strike) == ("AAPL", date(2026, 1, 16), OptionRight.CALL, 150.0)
        assert M.build_occ(root, expiry, right, strike) == "AAPL260116C00150000"
        assert M.parse_occ("SPY261218P00412500")[3] == 412.5
        assert M.build_occ("spy", date(2026, 12, 18), OptionRight.PUT, 412.5) == "SPY261218P00412500"

    @pytest.mark.parametrize("bad", ["", "AAPL", "AAPL260116X00150000", "AAPL26011C00150000"])
    def test_malformed_occ_raises(self, bad):
        with pytest.raises(ValueError):
            M.parse_occ(bad)

    def test_standard_root_rule(self):
        assert M.is_standard_occ_root("AAPL", "AAPL") is True
        assert M.is_standard_occ_root("AAPL1", "AAPL") is False       # adjusted contract
        assert M.is_standard_occ_root("1SPY", "SPY") is False
        assert M.is_standard_occ_root("AAPLX", "AAPL") is False       # root is not the underlying

    def test_ib_option_strings(self):
        assert M.ib_expiry_string(date(2026, 1, 16)) == "20260116"
        assert M.ib_right(OptionRight.CALL) == "C" and M.ib_right(OptionRight.PUT) == "P"


class TestOrderIds:
    def test_order_ref_round_trip_with_nonce_and_suffix(self):
        assert M.make_order_ref(3, 41) == "ba2:3:41"
        assert M.make_order_ref(3, 41, nonce="0a1b2c3d") == "ba2:3:41:0a1b2c3d"
        assert M.make_order_ref(3, 41, "SL", nonce="0a1b2c3d") == "ba2:3:41:0a1b2c3d:SL"
        assert M.parse_order_ref("ba2:3:41:0a1b2c3d") == (3, 41, "0a1b2c3d", None)
        assert M.parse_order_ref("ba2:3:41:0a1b2c3d:SL") == (3, 41, "0a1b2c3d", "SL")
        assert M.parse_order_ref("ba2:3:41").nonce is None       # legacy: parsed, but never trusted

    def test_nonces_are_random_eight_hex_digits(self):
        a, b = M.new_nonce(), M.new_nonce()
        assert a != b and len(a) == 8 and int(a, 16) >= 0

    @pytest.mark.parametrize("junk", [None, "", "manual", "ba2:x:1", "ba2:1", "ba2:1:2:sl",
                                      "ba2:1:2:XYZ12345", "ba2:1:2:0a1b2c3d:sl"])
    def test_foreign_refs_are_not_ours(self, junk):
        assert M.parse_order_ref(junk) is None

    def test_broker_order_id_prefers_the_perm_id(self):
        assert M.format_broker_order_id(1234567890, 7) == "1234567890"
        assert M.format_broker_order_id(0, 7) == "o7"
        with pytest.raises(ValueError):
            M.format_broker_order_id(0, None)

    def test_matching_understands_both_encodings(self):
        assert M.broker_id_matches("1234567890", 1234567890, 7)
        assert M.broker_id_matches("o7", 0, 7)
        assert M.broker_id_matches("o7", 1234567890, 7)           # row not yet upgraded
        assert not M.broker_id_matches("o8", 1234567890, 7)
        assert not M.broker_id_matches(None, 1, 1)


class TestTimeInForce:
    def test_market_is_day(self):
        assert M.ib_time_in_force(None, is_market=True) == ("DAY", None)
        assert M.ib_time_in_force("day", is_market=True) == ("DAY", None)
        assert M.ib_time_in_force("ioc", is_market=True) == ("IOC", None)

    def test_a_resting_tif_on_a_market_order_becomes_day_with_a_warning(self):
        tif, warning = M.ib_time_in_force("gtc", is_market=True)
        assert tif == "DAY" and "never rests" in warning
        assert M.ib_time_in_force("GTC", is_market=True)[0] == "DAY"

    def test_resting_orders_default_to_gtc_like_alpaca(self):
        assert M.ib_time_in_force(None, is_market=False) == ("GTC", None)
        assert M.ib_time_in_force("day", is_market=False) == ("DAY", None)
        assert M.ib_time_in_force("GTC", is_market=False) == ("GTC", None)

    def test_unrecognised_is_an_error(self):
        with pytest.raises(ValueError):
            M.ib_time_in_force("gtc_ext", is_market=False)


class TestPriceTicks:
    PENNY = [(0.0, 0.0001), (1.0, 0.01)]
    OPTION = [(0.0, 0.01), (3.0, 0.05)]

    def test_increment_by_band(self):
        assert M.price_increment(self.PENNY, 0.5) == 0.0001
        assert M.price_increment(self.PENNY, 1.0) == 0.01
        assert M.price_increment(self.OPTION, 2.99) == 0.01
        assert M.price_increment(self.OPTION, 3.0) == 0.05

    def test_no_rules_is_an_error_not_a_guess(self):
        with pytest.raises(ValueError):
            M.price_increment([], 1.0)

    @pytest.mark.parametrize("price,mode,expected", [
        (150.126, "nearest", 150.13), (150.124, "nearest", 150.12), (150.125, "nearest", 150.13),
        (150.129, "down", 150.12), (150.121, "up", 150.13)])
    def test_rounding_modes(self, price, mode, expected):
        assert M.round_to_increment(price, [(0.0, 0.01)], mode) == pytest.approx(expected)

    def test_bands_and_crossing(self):
        assert M.round_to_increment(0.12347, self.PENNY) == pytest.approx(0.1235)
        assert M.round_to_increment(1.004, self.PENNY) == pytest.approx(1.0)
        assert M.round_to_increment(3.02, self.OPTION) == pytest.approx(3.0)
        assert M.round_to_increment(3.03, self.OPTION) == pytest.approx(3.05)
        assert M.round_to_increment(2.996, self.OPTION) == pytest.approx(3.0)    # crosses into 0.05 band

    def test_exact_prices_are_unchanged_and_float_noise_is_absent(self):
        assert M.round_to_increment(0.3, [(0.0, 0.01)]) == 0.3
        assert M.round_to_increment(1.15, [(0.0, 0.05)]) == 1.15


class _Row:
    def __init__(self, account, tag, value, currency="USD"):
        self.account, self.tag, self.value, self.currency, self.modelCode = account, tag, value, currency, ""


class TestAccountValues:
    def test_other_accounts_and_non_numeric_tags(self):
        rows = [_Row("DU1", "NetLiquidation", "100"), _Row("DU2", "NetLiquidation", "5"),
                _Row("DU1", "AccountType", "INDIVIDUAL", "")]
        numbers, texts = M.select_account_values(rows, "DU1")
        assert numbers == {"NetLiquidation": 100.0} and texts == {"AccountType": "INDIVIDUAL"}

    def test_currency_preference_usd_then_base_then_blank(self):
        rows = [_Row("A", "X", "1", "EUR"), _Row("A", "X", "2", "BASE"), _Row("A", "X", "3", "USD"),
                _Row("A", "Y", "4", "BASE"), _Row("A", "Z", "5", ""), _Row("A", "NetLiquidation", "9")]
        numbers, _ = M.select_account_values(rows, "A")
        assert (numbers["X"], numbers["Y"], numbers["Z"]) == (3.0, 4.0, 5.0)

    def test_a_non_usd_only_account_is_refused(self):
        with pytest.raises(M.IBKRUnsupportedCurrency):
            M.select_account_values([_Row("A", "NetLiquidation", "9", "EUR")], "A")

    def test_base_currency_net_liquidation_is_accepted(self):
        numbers, _ = M.select_account_values([_Row("A", "NetLiquidation", "9", "BASE")], "A")
        assert numbers["NetLiquidation"] == 9.0


class TestSnapshot:
    BASE = {"NetLiquidation": 100000.0, "TotalCashValue": 50000.0, "SettledCash": 48000.0,
            "AvailableFunds": 80000.0, "BuyingPower": 320000.0, "ExcessLiquidity": 90000.0,
            "InitMarginReq": 20000.0, "MaintMarginReq": 15000.0, "EquityWithLoanValue": 100000.0}

    def test_margin_detection_ratio(self):
        assert M.margin_multiplier_from(self.BASE) == 2.0                       # 4x => Reg-T margin
        assert M.margin_multiplier_from({**self.BASE, "BuyingPower": 80000.0}) == 1.0   # cash
        assert M.margin_multiplier_from({**self.BASE, "BuyingPower": 160000.0}) == 2.0
        assert M.margin_multiplier_from({**self.BASE, "BuyingPower": 100000.0}) == 1.0
        assert M.margin_multiplier_from({**self.BASE, "AvailableFunds": 0.0}) == 1.0
        assert M.margin_multiplier_from({"NetLiquidation": 1.0}) == 1.0

    @pytest.mark.parametrize("sma", [0.0, -2000.0])
    def test_a_zero_or_negative_sma_is_not_a_bound(self, sma):
        nums = {"AvailableFunds": 80000.0, "BuyingPower": 320000.0, "ExcessLiquidity": 90000.0,
                "NetLiquidation": 100000.0, "SMA": sma}
        snap = M.snapshot_from_account_values(nums, {}, None, None)
        assert snap.buying_power == 160000.0 and snap.raw["bp_binding"] == "available_funds_x_mult"

    def test_a_positive_sma_binds_when_smallest_and_the_binder_is_named(self):
        nums = {"AvailableFunds": 80000.0, "BuyingPower": 320000.0, "ExcessLiquidity": 90000.0,
                "NetLiquidation": 100000.0, "SMA": 30000.0}
        snap = M.snapshot_from_account_values(nums, {}, None, None)
        assert snap.buying_power == 60000.0 and snap.raw["bp_binding"] == "sma_x_mult"

    def test_buying_power_is_available_funds_times_regt_never_ibs_own(self):
        snap = M.snapshot_from_account_values(dict(self.BASE), {}, 1000.0, -200.0)
        assert snap.buying_power == 160000.0 and snap.raw["ib_buying_power"] == 320000.0
        assert snap.margin_multiplier == 2.0 and snap.is_margin_account is True
        assert snap.option_buying_power == 80000.0 and snap.cash == 50000.0
        assert snap.non_marginable_buying_power == 48000.0
        assert snap.equity == snap.net_liquidation == 100000.0
        assert snap.long_market_value == 1000.0 and snap.short_market_value == -200.0

    def test_short_market_value_is_forced_negative(self):
        assert M.snapshot_from_account_values(dict(self.BASE), {}, 0.0, 200.0).short_market_value == -200.0

    def test_missing_tags_stay_none_never_zero(self):
        snap = M.snapshot_from_account_values({"NetLiquidation": 5.0}, {}, None, None)
        assert snap.buying_power is None and snap.cash is None and snap.margin_multiplier is None
        assert snap.long_market_value is None and snap.option_buying_power is None


class TestReadings:
    def test_fractionable_tri_state(self):
        assert M.fractionable_from_size(0.0001, 0.0001) is True
        assert M.fractionable_from_size(1.0, 1.0) is False
        assert M.fractionable_from_size(None, None) is None
        assert M.fractionable_from_size(float("nan"), 1.7976931348623157e308) is None
        assert M.fractionable_from_size(1.0, 0.01) is True        # any sub-share step is fractional
        assert M.fractionable_from_size(0.0, 0.0) is None

    def test_shortable_scale(self):
        assert M.is_easy_to_borrow(3.0) is True
        assert M.is_easy_to_borrow(2.5) is False                  # strictly greater
        assert M.is_easy_to_borrow(1.0) is False
        assert M.is_easy_to_borrow(None) is False and M.is_easy_to_borrow(float("nan")) is False

    def test_delayed_data(self):
        assert M.delayed_market_data(3) and M.delayed_market_data(4)
        assert not M.delayed_market_data(1) and not M.delayed_market_data(2)
        assert not M.delayed_market_data(None)
