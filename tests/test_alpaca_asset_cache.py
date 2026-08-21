"""The shared Alpaca Asset cache: one bulk /assets call, not one call per symbol.

Alpaca DOES have a bulk asset endpoint -- get_all_assets + GetAssetsRequest
(alpaca/trading/client.py:376-397). The book is ~2,500 symbols and the trading API
allows 200 req/min, so the per-symbol path on a cold cache is ~12 minutes of wall
clock inside a page load.

Small baskets deliberately keep the per-symbol endpoint: fetching ~11,000 rows to
answer three questions is the worse trade.

MarginInfo.fractionable is TRI-STATE (contract 1.2): True / False / None, where None
means the broker did not say. Every assertion below uses `is True` / `is False` /
`is None` rather than truthiness, because that distinction is the point.

No live call: client is a MagicMock returning real alpaca-py Asset objects.
"""
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import (
    AccountStatus, AssetClass, AssetExchange, AssetStatus,
)
from alpaca.trading.models import Asset, TradeAccount

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _asset(symbol="AAPL", fractionable=True, marginable=True):
    # `asset_class` is exposed under the pydantic alias "class", so it must be passed
    # via a dict splat -- Asset(asset_class=...) raises "Field required".
    return Asset(
        id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
        symbol=symbol, status=AssetStatus.ACTIVE, tradable=True, marginable=marginable,
        shortable=True, easy_to_borrow=True, fractionable=fractionable,
        min_order_size=0.001, min_trade_increment=0.001,
        maintenance_margin_requirement=30.0)


def _bare_account():
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._authentication_error = None
    acct._asset_cache = {}
    acct._margin_info_cache = {}
    acct.client.get_account.return_value = TradeAccount(
        id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
        cash="1000", equity="25000", buying_power="50000", multiplier="2")
    acct.client.get_asset.side_effect = lambda s: _asset(s)
    return acct


def _basket(n):
    return [f"SYM{i:04d}" for i in range(n)]


# ---------------------------------------------------------------------------
# Bulk vs per-symbol
# ---------------------------------------------------------------------------

def test_a_large_basket_costs_one_bulk_call_not_one_call_per_symbol():
    """The whole point: 60 symbols must not be 60 HTTP round trips."""
    acct = _bare_account()
    symbols = _basket(60)
    acct.client.get_all_assets.return_value = [_asset(s) for s in symbols]

    assets = acct._load_assets(symbols)

    assert acct.client.get_all_assets.call_count == 1
    assert acct.client.get_asset.call_count == 0
    assert sorted(assets) == sorted(symbols)


def test_the_bulk_request_asks_for_active_us_equities():
    acct = _bare_account()
    symbols = _basket(60)
    acct.client.get_all_assets.return_value = [_asset(s) for s in symbols]

    acct._load_assets(symbols)

    request = acct.client.get_all_assets.call_args[0][0]
    assert request.asset_class == AssetClass.US_EQUITY
    assert request.status == AssetStatus.ACTIVE


def test_a_small_basket_still_uses_the_per_symbol_endpoint():
    """Pulling ~11,000 rows to answer three questions is the worse trade."""
    acct = _bare_account()

    acct._load_assets(["AAPL", "MSFT", "NVDA"])

    acct.client.get_all_assets.assert_not_called()
    assert acct.client.get_asset.call_count == 3


def test_the_threshold_is_AT_or_above_not_strictly_above():
    """`_BULK_ASSET_FETCH_THRESHOLD` is documented as "misses AT OR ABOVE which one bulk
    call wins". 3-versus-60 straddles 20 but never lands on it, so `>=` degrading to `>`
    survives both -- only a basket of exactly the threshold can tell them apart."""
    acct = _bare_account()
    symbols = _basket(AlpacaAccount._BULK_ASSET_FETCH_THRESHOLD)
    acct.client.get_all_assets.return_value = [_asset(s) for s in symbols]

    acct._load_assets(symbols)

    assert acct.client.get_all_assets.call_count == 1
    assert acct.client.get_asset.call_count == 0


def test_one_below_the_threshold_still_goes_per_symbol():
    acct = _bare_account()
    symbols = _basket(AlpacaAccount._BULK_ASSET_FETCH_THRESHOLD - 1)

    acct._load_assets(symbols)

    acct.client.get_all_assets.assert_not_called()
    assert acct.client.get_asset.call_count == len(symbols)


def test_a_bulk_fetched_basket_is_cached_so_the_second_render_costs_nothing():
    """The whole point of the task, on the warm path. The allocation page asks for the
    SAME basket on every refresh; a bulk response that is not cached would re-pull the
    ~11,000-row universe each time, which is worse than the per-symbol path it replaced."""
    acct = _bare_account()
    symbols = _basket(60)
    acct.client.get_all_assets.return_value = [_asset(s) for s in symbols]

    acct._load_assets(symbols)
    assets = acct._load_assets(symbols)

    assert acct.client.get_all_assets.call_count == 1
    assert acct.client.get_asset.call_count == 0
    assert sorted(assets) == sorted(symbols)


def test_a_repeated_symbol_is_only_fetched_once():
    """"Duplicates collapsed", per the docstring. A basket built by unioning several
    labels routinely repeats a name."""
    acct = _bare_account()

    assets = acct._load_assets(["AAPL", "aapl", " AAPL "])

    assert acct.client.get_asset.call_count == 1
    assert sorted(assets) == ["AAPL"]


def test_symbols_absent_from_the_bulk_response_fall_back_to_the_per_symbol_endpoint():
    """The bulk list is ACTIVE US EQUITY only. Anything else -- an OTC name, a
    delisted ticker -- has to be asked for individually or it silently vanishes."""
    acct = _bare_account()
    symbols = _basket(60)
    acct.client.get_all_assets.return_value = [_asset(s) for s in symbols[:-1]]

    assets = acct._load_assets(symbols)

    assert acct.client.get_asset.call_count == 1
    assert acct.client.get_asset.call_args[0][0] == symbols[-1]
    assert sorted(assets) == sorted(symbols)


def test_a_bulk_failure_degrades_to_per_symbol_fetches_rather_than_returning_nothing():
    acct = _bare_account()
    symbols = _basket(60)
    acct.client.get_all_assets.side_effect = RuntimeError("503 unavailable")

    assets = acct._load_assets(symbols)

    assert acct.client.get_asset.call_count == 60
    assert sorted(assets) == sorted(symbols)


# ---------------------------------------------------------------------------
# get_fractionability -- Alpaca-internal, omission means unknown
# ---------------------------------------------------------------------------

def test_get_fractionability_reports_the_brokers_own_flag():
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s, fractionable=(s == "AAPL"))

    flags = acct.get_fractionability(["AAPL", "BRK.A"])

    assert flags == {"AAPL": True, "BRK.A": False}


def test_a_symbol_the_broker_cannot_describe_is_omitted_never_defaulted():
    """Omission means "unknown". Defaulting it to False would make the dry run promise
    a whole-share rounding that will not happen; defaulting it to True would promise a
    fraction the broker then rejects."""
    acct = _bare_account()

    def _get(symbol):
        if symbol == "NOSUCH":
            raise RuntimeError("404 asset not found")
        return _asset(symbol)

    acct.client.get_asset.side_effect = _get

    flags = acct.get_fractionability(["AAPL", "NOSUCH"])

    assert flags == {"AAPL": True}
    assert "NOSUCH" not in flags


def test_symbols_are_normalised_before_the_lookup():
    acct = _bare_account()

    assert acct.get_fractionability(["  aapl "]) == {"AAPL": True}
    assert acct.client.get_asset.call_args[0][0] == "AAPL"


def test_blank_entries_never_become_a_lookup():
    acct = _bare_account()

    acct.get_fractionability(["AAPL", "", None])

    assert acct.client.get_asset.call_count == 1


# ---------------------------------------------------------------------------
# The tri-state carrier: MarginInfo.fractionable
# ---------------------------------------------------------------------------

def test_margin_info_reports_a_broker_no_as_a_real_false():
    """Asset.fractionable is a REQUIRED non-Optional bool (models.py:65), so a present
    Asset always carries a real answer. False here means the broker said no -- it is
    NOT the same state as "we could not ask", which is an omitted row."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s, fractionable=False)

    info = acct.get_symbol_margin_info(["BRK.A"])["BRK.A"]

    assert info.fractionable is False


def test_a_symbol_the_broker_cannot_describe_has_no_margin_info_row():
    """The tri-state's third value reaches the engine as an ABSENT key, which
    compute_allocation reads as `m is None` -> REASON_FRACTIONAL_UNKNOWN. Fabricating
    a MarginInfo with fractionable=False here would turn "did not answer" into "said
    no" -- the exact failure this feature exists to kill."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = RuntimeError("404 asset not found")

    assert acct.get_symbol_margin_info(["NOSUCH"]) == {}


# ---------------------------------------------------------------------------
# Caching and invalidation
# ---------------------------------------------------------------------------

def test_a_second_request_for_the_same_symbol_hits_the_cache():
    acct = _bare_account()

    acct.get_fractionability(["AAPL"])
    acct.get_fractionability(["AAPL"])

    assert acct.client.get_asset.call_count == 1


def test_margin_info_and_fractionability_share_one_asset_fetch():
    """Two consumers of the same fact must not cost two round trips -- and cannot
    disagree about a symbol, because there is only one cached Asset behind both."""
    acct = _bare_account()

    acct.get_fractionability(["AAPL"])
    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert acct.client.get_asset.call_count == 1
    assert info.fractionable is True


def test_an_expired_asset_entry_is_refetched():
    acct = _bare_account()
    acct.get_fractionability(["AAPL"])
    stamp, cached = acct._asset_cache["AAPL"]
    acct._asset_cache["AAPL"] = (stamp - AlpacaAccount._ASSET_CACHE_TTL - 1, cached)
    acct.client.get_asset.side_effect = lambda s: _asset(s, fractionable=False)

    assert acct.get_fractionability(["AAPL"]) == {"AAPL": False}
    assert acct.client.get_asset.call_count == 2


def test_clear_asset_cache_forces_the_next_call_to_refetch():
    acct = _bare_account()
    acct.get_fractionability(["AAPL"])

    acct.clear_asset_cache()
    acct.get_fractionability(["AAPL"])

    assert acct.client.get_asset.call_count == 2
    assert acct._asset_cache != {}     # repopulated, not just emptied


def test_clearing_the_margin_cache_also_drops_the_assets_behind_it():
    """Refresh means "re-ask the broker". Keeping the Asset would rebuild the identical
    MarginInfo from the identical stale facts and make the button a no-op."""
    acct = _bare_account()
    acct.get_symbol_margin_info(["AAPL"])

    acct.clear_margin_info_cache()

    assert acct._asset_cache == {}


def test_an_expired_margin_entry_also_drops_the_asset_it_was_derived_from():
    acct = _bare_account()
    acct.get_symbol_margin_info(["AAPL"])
    stamp, cached = acct._margin_info_cache["AAPL"]
    acct._margin_info_cache["AAPL"] = (
        stamp - AlpacaAccount._MARGIN_INFO_CACHE_TTL - 1, cached)
    acct.client.get_asset.side_effect = lambda s: _asset(s, marginable=False)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert acct.client.get_asset.call_count == 2
    assert info.marginable is False


def test_an_unauthenticated_account_loads_no_assets():
    acct = _bare_account()
    acct.client = None
    acct._authentication_error = "missing api_key"

    assert acct._load_assets(["AAPL"]) == {}
    assert acct.get_fractionability(["AAPL"]) == {}


def test_an_unauthenticated_account_never_reaches_the_client_at_all(monkeypatch):
    """The test above is satisfied by a DOUBLE FAULT and so proves nothing on its own.

    Delete the `_check_authentication` guard from `_load_assets` and it still passes:
    the per-symbol loop raises AttributeError on `None.get_asset`, the loop's
    `except Exception` swallows it per symbol, and the method returns `{}` anyway --
    the right answer by way of a crash, one bad ticker's error path silently doing an
    authentication check's job. This one pins the GUARD, by keeping a live client
    around and asserting nothing ever touches it.
    """
    acct = _bare_account()
    monkeypatch.setattr(AlpacaAccount, "_check_authentication", lambda self: False)

    assert acct._load_assets(_basket(60)) == {}
    assert acct.get_fractionability(["AAPL"]) == {}

    acct.client.get_asset.assert_not_called()
    acct.client.get_all_assets.assert_not_called()
