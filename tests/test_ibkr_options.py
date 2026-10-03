"""IBKRAccount options (OptionsAccountInterface) against FakeIB: chains, quotes, ATM IV, positions,
single-leg and BAG-combo orders, combo refresh, closes, refusals. No network."""
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core import ibkr_mapping as M
from ba2_common.core.interfaces.OptionsAccountInterface import COVER_REFUSAL, OptionsAccountInterface
from ba2_common.core.option_types import OptionLeg, OptionPosition
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus)
from ba2_trade_platform.modules.accounts.ibkr_runtime import IBKRContractError
from tests.ibkr_fakes import NAN
from tests.ibkr_helpers import ibkr_logs, make_account  # noqa: F401
from tests.test_ibkr_lifecycle import fresh, rows

TODAY = datetime.now(timezone.utc).date()


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def occ(root, d: date, right, strike):
    return M.build_occ(root, d, OptionRight.CALL if right == "C" else OptionRight.PUT, strike)


EXP_NEAR = TODAY + timedelta(days=30)
EXP_FAR = TODAY + timedelta(days=90)


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    fake.add_option_chain("AAPL", [ymd(EXP_NEAR), ymd(EXP_FAR)], [140.0, 150.0, 160.0], under_con_id=265598)
    yield account, fake, aapl
    account.close()


def seed_chain(fake):
    """AAPL: two expiries, calls+puts at 140/150/160 with quotes/greeks/OI."""
    out = {}
    for exp in (EXP_NEAR, EXP_FAR):
        for strike in (140.0, 150.0, 160.0):
            for right in ("C", "P"):
                d = abs(strike - 150.0) / 100
                out[(exp, strike, right)] = fake.add_option(
                    "AAPL", ymd(exp), strike, right, bid=3.0 - d, ask=3.2 - d, last=3.1 - d,
                    iv=0.30 + d, delta=(0.5 if right == "C" else -0.5), gamma=0.05, theta=-0.04,
                    vega=0.2, oi=1000)
    return out


# ======================================================================= capability
class TestCapability:
    def test_it_is_an_options_account_with_a_declared_greeks_source(self, world):
        account, *_ = world
        assert isinstance(account, OptionsAccountInterface)
        assert account.OPTION_GREEKS_SOURCE == "broker" and account.supports_options is True

    def test_no_assignment_feed_is_claimed(self, world):
        """The TWS API has no assignment/exercise activity feed (design doc 7.3). Claiming the
        hook would make TradeManager call it; absence makes it a documented no-op."""
        account, *_ = world
        assert not hasattr(account, "get_option_activities")
        assert not hasattr(account, "reconcile_option_assignments")


# ======================================================================= chain
class TestChain:
    def test_rows_carry_quote_greeks_oi_and_the_source(self, world):
        account, fake, _ = world
        seed_chain(fake)
        chain = account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)
        assert len(chain) == 6
        row = next(c for c in chain if c.strike == 150.0 and c.option_type == OptionRight.CALL)
        assert row.symbol == occ("AAPL", EXP_NEAR, "C", 150.0)
        assert (row.underlying, row.expiry) == ("AAPL", EXP_NEAR)
        assert (row.bid, row.ask, row.last) == (3.0, 3.2, 3.1)
        assert row.implied_volatility == 0.30 and row.delta == 0.5 and row.gamma == 0.05
        assert row.theta == -0.04 and row.vega == 0.2 and row.open_interest == 1000
        assert row.greeks_source == "broker"
        assert row.volume is None and row.rho is None          # unknown, never zero
        put = next(c for c in chain if c.strike == 150.0 and c.option_type == OptionRight.PUT)
        assert put.delta == -0.5 and put.open_interest == 1000   # put OI read from the PUT field

    def test_window_type_and_strike_filters(self, world):
        account, fake, _ = world
        seed_chain(fake)
        both = account.get_option_chain("AAPL", EXP_NEAR, EXP_FAR)
        assert len(both) == 12 and {c.expiry for c in both} == {EXP_NEAR, EXP_FAR}
        puts = account.get_option_chain("AAPL", EXP_NEAR, EXP_FAR, option_type=OptionRight.PUT)
        assert len(puts) == 6 and all(c.option_type == OptionRight.PUT for c in puts)
        band = account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR, strike_min=145.0, strike_max=155.0)
        assert [c.strike for c in band] == [150.0, 150.0]
        assert account.get_option_chain("AAPL", TODAY, TODAY + timedelta(days=5)) == []

    def test_a_contract_with_no_quote_keeps_none_and_a_zero_bid_is_kept(self, world):
        account, fake, _ = world
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C")                       # nothing published
        fake.add_option("AAPL", ymd(EXP_NEAR), 160.0, "C", bid=0.0, ask=0.05)    # real empty bid
        fake.add_option("AAPL", ymd(EXP_NEAR), 140.0, "C", bid=-1.0, ask=-1.0)   # IB "no quote"
        by = {c.strike: c for c in account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)}
        assert (by[150.0].bid, by[150.0].ask, by[150.0].last, by[150.0].implied_volatility) == (None,) * 4
        assert (by[160.0].bid, by[160.0].ask) == (0.0, 0.05)
        assert (by[140.0].bid, by[140.0].ask) == (None, None)

    def test_delayed_contracts_are_excluded_with_an_error(self, world, ibkr_logs):
        account, fake, _ = world
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C", bid=3.0, ask=3.2, market_data_type=3)
        fake.add_option("AAPL", ymd(EXP_NEAR), 160.0, "C", bid=2.0, ask=2.2)
        chain = account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)
        assert [c.strike for c in chain] == [160.0]
        assert "DELAYED" in ibkr_logs.text()

    def test_adjusted_contracts_are_dropped_not_adjusted(self, world):
        account, fake, _ = world
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C", bid=3.0, ask=3.2, multiplier="10")
        fake.add_option("AAPL", ymd(EXP_NEAR), 160.0, "C", bid=2.0, ask=2.2)
        assert [c.strike for c in account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)] == [160.0]

    def test_no_standard_chain_is_an_error(self, monkeypatch):
        account, fake = make_account(monkeypatch)
        fake.add_stock("AAPL", 265598)
        fake.add_option_chain("AAPL", [ymd(EXP_NEAR)], [150.0], trading_class="AAPL1")   # adjusted class
        fake.add_option_chain("AAPL", [ymd(EXP_NEAR)], [150.0], multiplier="10")
        try:
            with pytest.raises(Exception, match="no standard"):
                account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)
        finally:
            account.close()

    def test_a_chain_too_wide_is_refused_not_truncated(self, world, monkeypatch):
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        account, fake, _ = world
        seed_chain(fake)
        monkeypatch.setattr(IBKRAccount, "_MAX_CHAIN_CONTRACTS", 5)
        with pytest.raises(Exception, match="narrow the expiry or strike window"):
            account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)

    def test_market_data_lines_are_batched_and_always_released(self, world, monkeypatch):
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_QUOTE_BATCH", 4)
        monkeypatch.setattr(IBKRAccount, "_QUOTE_WAIT", 0.05)
        seed_chain(fake)
        assert len(account.get_option_chain("AAPL", EXP_NEAR, EXP_FAR)) == 12
        assert fake.max_active_lines <= 4 and fake.active_lines == 0 and fake.cancelled_lines == 12

    def test_the_ladder_costs_one_details_call_per_expiry_not_per_strike(self, world):
        account, fake, _ = world
        seed_chain(fake)
        before = fake.detail_calls
        account.get_option_chain("AAPL", EXP_NEAR, EXP_FAR)
        assert fake.detail_calls - before == 3          # the stock once + one wildcard call per expiry

    def test_failure_is_raised_so_callers_treat_the_chain_as_unknown(self, world):
        account, fake, _ = world
        fake.fail_calls["reqSecDefOptParamsAsync"] = TimeoutError("slow")
        with pytest.raises(TimeoutError):
            account.get_option_chain("AAPL", EXP_NEAR, EXP_NEAR)


# ======================================================================= quote / ATM IV
class TestQuoteAndIV:
    def test_quote_with_greeks(self, world):
        account, fake, _ = world
        seed_chain(fake)
        q = account.get_option_quote(occ("AAPL", EXP_NEAR, "P", 150.0))
        assert (q.bid, q.ask, q.last) == (3.0, 3.2, 3.1)
        assert (q.delta, q.gamma, q.theta, q.vega, q.implied_volatility) == (-0.5, 0.05, -0.04, 0.2, 0.30)
        assert q.rho is None and q.volume is None and q.mid == 3.1

    def test_nothing_published_is_none(self, world):
        account, fake, _ = world
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C")
        assert account.get_option_quote(occ("AAPL", EXP_NEAR, "C", 150.0)) is None

    def test_delayed_quote_is_refused(self, world):
        account, fake, _ = world
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C", bid=1.0, ask=1.2, market_data_type=3)
        assert account.get_option_quote(occ("AAPL", EXP_NEAR, "C", 150.0)) is None

    def test_an_unknown_contract_raises(self, world):
        account, *_ = world
        with pytest.raises(IBKRContractError):
            account.get_option_quote(occ("AAPL", EXP_NEAR, "C", 999.0))

    def test_an_adjusted_root_is_refused(self, world):
        account, *_ = world
        with pytest.raises(IBKRContractError, match="non-standard"):
            account.get_option_quote("AAPL1" + occ("AAPL", EXP_NEAR, "C", 150.0)[4:])

    def test_atm_iv_is_the_nearest_strike_in_the_20_to_45_dte_window(self, world):
        account, fake, aapl = world
        fake.set_quote(aapl, bid=151.0, ask=151.2)
        for strike, iv in ((140.0, 0.45), (150.0, 0.31), (160.0, 0.40)):
            fake.add_option("AAPL", ymd(EXP_NEAR), strike, "C", bid=2.0, ask=2.2, iv=iv)
        assert account.get_atm_implied_volatility("AAPL") == 0.31

    def test_atm_iv_unknown_without_a_spot_or_without_iv(self, world):
        account, fake, aapl = world
        assert account.get_atm_implied_volatility("AAPL") is None            # no spot
        fake.set_quote(aapl, bid=151.0, ask=151.2)
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C", bid=2.0, ask=2.2)  # quote but no greeks
        assert account.get_atm_implied_volatility("AAPL") is None


# ======================================================================= positions
class TestOptionPositions:
    def test_long_and_short_rows_with_per_share_premium(self, world):
        account, fake, _ = world
        c = fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C")
        p = fake.add_option("AAPL", ymd(EXP_NEAR), 140.0, "P")
        fake.add_position(c, 2, 523.0, mark=6.1, multiplier=100)
        fake.add_position(p, -1, 210.0, mark=1.9, multiplier=100, unrealized=20.0)
        by = {pos.contract_symbol: pos for pos in account.get_option_positions()}
        long_c = by[occ("AAPL", EXP_NEAR, "C", 150.0)]
        assert (long_c.side, long_c.quantity, long_c.avg_entry_price) == (OrderDirection.BUY, 2.0, 5.23)
        assert (long_c.underlying, long_c.option_type, long_c.strike, long_c.expiry, long_c.multiplier) == (
            "AAPL", OptionRight.CALL, 150.0, EXP_NEAR, 100)
        assert long_c.current_price == 6.1 and long_c.market_value == pytest.approx(1220.0)
        short_p = by[occ("AAPL", EXP_NEAR, "P", 140.0)]
        assert (short_p.side, short_p.quantity, short_p.avg_entry_price) == (OrderDirection.SELL, 1.0, 2.1)
        assert short_p.unrealized_pl == 20.0 and short_p.option_type == OptionRight.PUT

    def test_flat_book_is_empty_list_failure_is_none(self, world):
        account, fake, _ = world
        assert account.get_option_positions() == []
        fake.fail_calls["positions"] = ConnectionError("down")
        assert account.get_option_positions() is None

    def test_equity_rows_and_other_accounts_are_excluded(self, world):
        account, fake, aapl = world
        c = fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C")
        fake.add_position(aapl, 10, 150.0, mark=155.0)
        fake.add_position(c, 1, 500.0, mark=5.0, account="DU9999999", multiplier=100)
        assert account.get_option_positions() == []

    def test_a_missing_mark_stays_none(self, world):
        account, fake, _ = world
        c = fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C")
        fake.add_position(c, 1, 500.0, mark=None)
        pos = account.get_option_positions()[0]
        assert pos.current_price is None and pos.market_value is None and pos.avg_entry_price == 5.0

    def test_equity_positions_do_not_include_options(self, world):
        account, fake, _ = world
        c = fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C")
        fake.add_position(c, 1, 500.0, mark=5.0, multiplier=100)
        assert account.get_positions() == []


# ======================================================================= orders
def leg(side, strike, right="C", exp=EXP_NEAR, ratio=1, underlying="AAPL"):
    return OptionLeg(contract_symbol=occ("AAPL", exp, right, strike), side=side, ratio_qty=ratio,
                     position_intent=("buy_to_open" if side == OrderDirection.BUY else "sell_to_open"),
                     option_type=OptionRight.CALL if right == "C" else OptionRight.PUT, strike=strike,
                     expiry=exp, underlying=underlying)


class TestOptionOrders:
    @pytest.fixture
    def seeded(self, world):
        account, fake, aapl = world
        from ib_async import PriceIncrement
        fake.market_rules[27] = [PriceIncrement(0.0, 0.01), PriceIncrement(3.0, 0.05)]
        seed_chain(fake)
        return account, fake, aapl

    def test_single_leg_limit_is_day_ticked_and_correlated(self, seeded):
        account, fake, _ = seeded
        fake.add_option("AAPL", ymd(EXP_NEAR), 155.0, "C", bid=3.0, ask=3.2, rule="27")
        parent = account.submit_option_order(
            [leg(OrderDirection.BUY, 155.0)], 2, "limit", 3.46, option_strategy="long_call")
        p = fake.placed[-1]
        assert (p["sec_type"], p["orderType"], p["action"], p["qty"], p["tif"]) == (
            "OPT", "LMT", "BUY", 2.0, "DAY")
        assert p["lmt"] == pytest.approx(3.45)                        # 0.05 tick above $3
        assert p["ref"] == f"ba2:{account.id}:{parent.id}" and p["account"] == "DU1234567"
        assert p["outsideRth"] is False
        f = fresh(parent)
        assert f.status == OrderStatus.ACCEPTED and f.broker_order_id.isdigit()
        assert f.asset_class == AssetClass.OPTION and f.contract_symbol == occ("AAPL", EXP_NEAR, "C", 155.0)
        assert f.transaction_id is not None and f.legs_broker_ids is None

    def test_single_leg_market(self, seeded):
        account, fake, _ = seeded
        account.submit_option_order([leg(OrderDirection.SELL, 140.0, "P")], 1, "market",
                                    option_strategy="short_put")
        p = fake.placed[-1]
        assert (p["orderType"], p["action"], p["tif"]) == ("MKT", "SELL", "DAY")

    def test_credit_spread_is_one_bag_order_with_a_negative_price(self, seeded):
        account, fake, _ = seeded
        legs = [leg(OrderDirection.SELL, 150.0, "P"), leg(OrderDirection.BUY, 140.0, "P")]
        parent = account.submit_option_order(legs, 3, "limit", -1.234, option_strategy="bull_put_spread")
        assert len([p for p in fake.placed if not p["modification"]]) == 1
        p = fake.placed[-1]
        assert (p["sec_type"], p["orderType"], p["action"], p["qty"], p["tif"]) == ("BAG", "LMT", "BUY", 3.0, "DAY")
        assert p["lmt"] == pytest.approx(-1.23) and p["symbol"] == "AAPL"
        con = {(c.contract.strike, c.contract.right): c.contract.conId
               for c in fake.details["AAPL"]
               if c.contract.secType == "OPT" and c.contract.lastTradeDateOrContractMonth == ymd(EXP_NEAR)}
        assert p["combo_legs"] == [(con[(150.0, "P")], 1, "SELL"), (con[(140.0, "P")], 1, "BUY")]
        f = fresh(parent)
        assert f.status == OrderStatus.ACCEPTED and f.contract_symbol is None
        kids = rows(account, parent_order_id=parent.id)
        assert len(kids) == 2 and {k.status for k in kids} == {OrderStatus.ACCEPTED}
        assert {k.contract_symbol for k in kids} == {occ("AAPL", EXP_NEAR, "P", 150.0),
                                                      occ("AAPL", EXP_NEAR, "P", 140.0)}

    def test_debit_spread_and_leg_ratios(self, seeded):
        account, fake, _ = seeded
        legs = [leg(OrderDirection.BUY, 140.0), leg(OrderDirection.SELL, 150.0, ratio=2),
                leg(OrderDirection.BUY, 160.0)]
        account.submit_option_order(legs, 1, "limit", 0.05, option_strategy="call_butterfly")
        p = fake.placed[-1]
        assert p["lmt"] == pytest.approx(0.05) and [r for _, r, _ in p["combo_legs"]] == [1, 2, 1]
        assert [a for _, _, a in p["combo_legs"]] == ["BUY", "SELL", "BUY"]

    def test_a_broker_rejection_unwinds_every_row_to_error(self, seeded):
        account, fake, _ = seeded
        fake.behaviors.append(("reject", 201, "Order rejected - reason: Insufficient margin"))
        legs = [leg(OrderDirection.SELL, 150.0, "P"), leg(OrderDirection.BUY, 140.0, "P")]
        assert account.submit_option_order(legs, 1, "limit", -1.0, option_strategy="bull_put_spread") is None
        parents = [r for r in rows(account) if r.parent_order_id is None]
        assert len(parents) == 1 and parents[0].status == OrderStatus.ERROR
        assert "Insufficient margin" in parents[0].comment
        kids = rows(account, parent_order_id=parents[0].id)
        assert len(kids) == 2 and {k.status for k in kids} == {OrderStatus.ERROR}

    def test_no_ack_leaves_the_order_open_for_the_refresh_to_adopt(self, seeded, monkeypatch):
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.2)
        account, fake, _ = seeded
        fake.behaviors.append("silent")
        legs = [leg(OrderDirection.SELL, 150.0, "P"), leg(OrderDirection.BUY, 140.0, "P")]
        parent = account.submit_option_order(legs, 1, "limit", -1.0, option_strategy="bull_put_spread")
        f = fresh(parent)
        assert f.status == OrderStatus.PENDING_NEW and f.broker_order_id.startswith("o")
        assert {k.status for k in rows(account, parent_order_id=parent.id)} == {OrderStatus.PENDING_NEW}
        assert len(fake.placed) == 1

    def test_read_only_account_refuses_and_nothing_is_left_pending(self, monkeypatch):
        account, fake = make_account(monkeypatch, read_only=True)
        fake.add_stock("AAPL", 1)
        fake.add_option("AAPL", ymd(EXP_NEAR), 150.0, "C", bid=1.0, ask=1.1)
        try:
            assert account.submit_option_order([leg(OrderDirection.BUY, 150.0)], 1, "market") is None
            assert fake.placed == []
            assert all(r.status == OrderStatus.ERROR for r in rows(account))
        finally:
            account.close()

    def test_fractional_contracts_and_bad_prices_are_refused(self, seeded):
        account, fake, _ = seeded
        assert account.submit_option_order([leg(OrderDirection.BUY, 150.0)], 1.5, "market") is None
        assert account.submit_option_order([leg(OrderDirection.BUY, 150.0)], 1, "limit", None) is None
        assert account.submit_option_order([leg(OrderDirection.BUY, 150.0)], 1, "limit", -2.0) is None
        assert fake.placed == []

    def test_an_adjusted_contract_is_refused_before_anything_is_sent(self, seeded):
        account, fake, _ = seeded
        bad = OptionLeg(contract_symbol="AAPL1" + occ("AAPL", EXP_NEAR, "C", 150.0)[4:],
                        side=OrderDirection.BUY, underlying="AAPL")
        assert account.submit_option_order([bad], 1, "market") is None and fake.placed == []

    def test_an_uncovered_covered_call_is_refused_by_the_shared_guard(self, seeded):
        account, fake, _ = seeded
        with pytest.raises(ValueError, match=COVER_REFUSAL):
            account.submit_option_order([leg(OrderDirection.SELL, 160.0)], 1, "limit", 1.0,
                                        option_strategy="covered_call")
        assert fake.placed == []

    def test_a_covered_call_with_the_shares_goes_out(self, seeded):
        account, fake, aapl = seeded
        fake.add_position(aapl, 100, 150.0, mark=155.0)
        fake.set_quote(aapl, bid=155.0, ask=155.1)
        account.submit_option_order([leg(OrderDirection.SELL, 160.0)], 1, "limit", 1.0,
                                    option_strategy="covered_call")
        assert fake.placed[-1]["action"] == "SELL"

    def test_close_rides_the_open_transaction_with_the_opposite_side(self, seeded):
        account, fake, _ = seeded
        opened = account.submit_option_order([leg(OrderDirection.BUY, 150.0)], 2, "limit", 3.0,
                                             option_strategy="long_call")
        txn_id = fresh(opened).transaction_id
        from ba2_trade_platform.core.db import update_instance
        o = fresh(opened)
        o.status, o.filled_qty = OrderStatus.FILLED, 2.0
        update_instance(o)
        position = OptionPosition(contract_symbol=occ("AAPL", EXP_NEAR, "C", 150.0), underlying="AAPL",
                                  option_type=OptionRight.CALL, strike=150.0, expiry=EXP_NEAR,
                                  side=OrderDirection.BUY, quantity=2.0, avg_entry_price=3.0)
        closing = account.close_option_position(position, "limit", 3.3)
        assert fake.placed[-1]["action"] == "SELL" and fake.placed[-1]["qty"] == 2.0
        assert fresh(closing).transaction_id == txn_id and fresh(closing).option_strategy == "close"


class TestOptionRefresh:
    @pytest.fixture
    def combo(self, world):
        account, fake, aapl = world
        seed_chain(fake)
        legs = [leg(OrderDirection.SELL, 150.0, "P"), leg(OrderDirection.BUY, 140.0, "P")]
        parent = account.submit_option_order(legs, 4, "limit", -1.2, option_strategy="bull_put_spread")
        ref = f"ba2:{account.id}:{parent.id}"
        contracts = {c.contract.strike: c.contract for c in fake.details["AAPL"]
                     if c.contract.secType == "OPT" and c.contract.right == "P"
                     and c.contract.lastTradeDateOrContractMonth == ymd(EXP_NEAR)}
        return account, fake, parent, ref, contracts

    def test_a_filled_combo_fills_its_children_with_per_leg_prices(self, combo):
        account, fake, parent, ref, contracts = combo
        fake.simulate_fill(ref, price=-1.2)
        fake.add_leg_fills(ref, [(contracts[150.0], "SLD", 4, 2.5), (contracts[140.0], "BOT", 4, 1.3)])
        account.refresh_orders()
        f = fresh(parent)
        assert f.status == OrderStatus.FILLED and f.filled_qty == 4
        kids = {k.strike: k for k in rows(account, parent_order_id=parent.id)}
        assert kids[150.0].status == kids[140.0].status == OrderStatus.FILLED
        assert kids[150.0].filled_qty == 4 and kids[140.0].filled_qty == 4
        assert kids[150.0].open_price == 2.5 and kids[140.0].open_price == 1.3

    def test_a_leg_with_no_execution_keeps_a_null_price(self, combo):
        account, fake, parent, ref, contracts = combo
        fake.simulate_fill(ref, price=-1.2)
        account.refresh_orders()
        kids = rows(account, parent_order_id=parent.id)
        assert all(k.status == OrderStatus.FILLED and k.open_price is None for k in kids)

    def test_a_partial_fill_scales_each_legs_quantity(self, combo):
        account, fake, parent, ref, contracts = combo
        fake.simulate_fill(ref, qty=1, price=-1.2)
        account.refresh_orders()
        assert fresh(parent).status == OrderStatus.PARTIALLY_FILLED
        kids = rows(account, parent_order_id=parent.id)
        assert {k.status for k in kids} == {OrderStatus.PARTIALLY_FILLED}
        assert {k.filled_qty for k in kids} == {1.0}

    def test_cancelling_the_combo_cancels_the_children(self, combo):
        account, fake, parent, ref, contracts = combo
        assert account.cancel_order(str(parent.id)) is True
        assert len(fake.cancel_requests) == 1                     # ONE IB order
        assert fresh(parent).status == OrderStatus.PENDING_CANCEL
        account.refresh_orders()
        assert fresh(parent).status == OrderStatus.CANCELED
        assert {k.status for k in rows(account, parent_order_id=parent.id)} == {OrderStatus.CANCELED}

    def test_get_orders_describes_the_combo_as_an_option_parent(self, combo):
        account, fake, parent, ref, contracts = combo
        listed = [o for o in account.get_orders() if o.asset_class == AssetClass.OPTION]
        assert len(listed) == 1 and listed[0].contract_symbol is None and listed[0].symbol == "AAPL"

    def test_a_single_leg_option_order_fills_through_the_normal_path(self, world):
        account, fake, _ = world
        seed_chain(fake)
        parent = account.submit_option_order([leg(OrderDirection.BUY, 150.0)], 2, "limit", 3.0,
                                             option_strategy="long_call")
        fake.simulate_fill(f"ba2:{account.id}:{parent.id}", price=3.0)
        account.refresh_orders()
        f = fresh(parent)
        assert f.status == OrderStatus.FILLED and f.filled_qty == 2 and f.open_price == 3.0
        listed = [o for o in account.get_orders() if o.asset_class == AssetClass.OPTION]
        assert listed[0].contract_symbol == occ("AAPL", EXP_NEAR, "C", 150.0)


class TestSharedOptionGatesOnIBKR:
    """The concrete OptionsAccountInterface gates (assignment capacity, reserve) read the account
    snapshot and the order book, never IBKR directly: pin that they work on this adapter."""

    def test_assignment_capacity_is_measured_against_settled_cash_not_equity(self, world):
        account, fake, _ = world
        assert account.cash_available_for_delivery() == 50000.0
        assert account.assignment_capacity(20000.0).ok is True
        fake.set_account_value("TotalCashValue", "3000")
        verdict = account.assignment_capacity(20000.0)
        assert verdict.ok is False and "ASSIGNMENT CAPACITY" in verdict.reason
        assert verdict.cash == 3000.0

    def test_unreadable_cash_is_unmeasurable_never_refused_on_equity(self, world):
        account, fake, _ = world
        fake.fail_account_reads(ConnectionError("x"))
        assert account.cash_available_for_delivery() is None
        assert account.assignment_capacity(1.0).ok is False

    def test_a_filled_short_put_counts_as_delivery_exposure(self, world):
        account, fake, _ = world
        seed_chain(fake)
        parent = account.submit_option_order([leg(OrderDirection.SELL, 150.0, "P")], 2, "limit", 2.0,
                                             option_strategy="short_put")
        fake.simulate_fill(f"ba2:{account.id}:{parent.id}", price=2.0)
        account.refresh_orders()
        exposure = account.short_put_assignment_exposure()
        assert exposure.is_measurable and exposure.cost == 150.0 * 100 * 2
        assert exposure.contracts == 2.0

    def test_option_buying_power_comes_from_available_funds(self, world):
        account, *_ = world
        assert account.get_option_buying_power() == 80000.0


class TestMarketDataLineBudget:
    def test_concurrent_market_data_groups_never_overlap(self, world, monkeypatch):
        """A chain pull and shortable-tick lookups from other threads share ONE line budget."""
        import threading
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        account, fake, aapl = world
        monkeypatch.setattr(IBKRAccount, "_QUOTE_BATCH", 4)
        monkeypatch.setattr(IBKRAccount, "_QUOTE_WAIT", 0.15)
        seed_chain(fake)
        fake.shortable_shares[aapl.conId] = 5.0
        account.get_positions()                                   # connect first
        stock = account._runtime().state.contracts.get("AAPL")
        errors = []

        def shorts():
            try:
                for _ in range(4):
                    account._call(lambda ib: account._short_check(ib, aapl, "AAPL"), op="short")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=shorts) for _ in range(3)]
        for t in threads:
            t.start()
        rows_ = account.get_option_chain("AAPL", EXP_NEAR, EXP_FAR)
        for t in threads:
            t.join()
        assert not errors and len(rows_) == 12
        assert fake.max_active_lines <= 4 and fake.active_lines == 0
