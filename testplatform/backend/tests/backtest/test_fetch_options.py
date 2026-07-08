# backend/tests/backtest/test_fetch_options.py
"""Pure (no-network) unit tests for the historical options-cache builder.

Covers the back-compat snapshot mapper, the metadata→chain-row mapper (iv/greeks None by
default; COMPUTED via Black-Scholes inversion when underlying_close+as_of_date are supplied —
see option_greeks.py), and the INACTIVE+ACTIVE contract merge/dedup logic that makes the cache
include EXPIRED contracts."""
from datetime import date

from app.services.backtest.fetch_options import (
    bar_to_row,
    contract_to_chain_row,
    contract_to_metadata_chain_row,
    is_standard_occ,
    merge_contracts_by_symbol,
    _nearest_on_or_before,
)


class _Snap:
    def __init__(self):
        self.implied_volatility = 0.3
        class G: delta=0.42; gamma=0.02; theta=-0.04; vega=0.12
        self.greeks = G()
        class Q: bid_price=1.0; ask_price=1.2
        self.latest_quote = Q()
        class T: price=1.1; size=7
        self.latest_trade = T()
        self.open_interest = 500


def test_contract_to_chain_row():
    row = contract_to_chain_row("AAPL240315C00180000", "AAPL", "call", 180.0, "2024-03-15", _Snap())
    assert row["occ_symbol"] == "AAPL240315C00180000"
    assert row["iv"] == 0.3 and row["delta"] == 0.42 and row["bid"] == 1.0 and row["last"] == 1.1


def test_contract_to_chain_row_handles_missing_snapshot():
    row = contract_to_chain_row("AAPL240315C00180000", "AAPL", "call", 180.0, "2024-03-15", object())
    assert row["occ_symbol"] == "AAPL240315C00180000" and row["iv"] is None and row["delta"] is None


def test_bar_to_row():
    class B: open=2.0; high=2.5; low=1.9; close=2.3; volume=100
    row = bar_to_row("AAPL240315C00180000", "2024-03-05", B(), "AAPL", "call", 180.0, "2024-03-15")
    assert row["close"] == 2.3 and row["underlying"] == "AAPL"
    assert row["iv"] is None, "no underlying_close supplied -> greeks not computed"


def test_bar_to_row_computes_greeks_when_underlying_close_supplied():
    """With underlying_close supplied, iv/delta/gamma/theta/vega are Black-Scholes-inverted from
    THIS bar's own close — real point-in-time greeks, not vendor data (option_greeks.py)."""
    class B: open=9.5; high=10.5; low=9.0; close=10.45; volume=100
    row = bar_to_row("AAPL250305C00100000", "2024-03-05", B(), "AAPL", "call", 100.0,
                     "2025-03-05", underlying_close=100.0, risk_free_rate=0.05)
    assert row["iv"] is not None and abs(row["iv"] - 0.20) < 0.01, \
        "textbook S=K=100,T=1,r=5%,call=10.45 -> iv should recover ~20%"
    assert row["delta"] is not None and 0.6 < row["delta"] < 0.7


# --------------------------------------------------------------------------- #
# NEW: historical cache builder pure logic
# --------------------------------------------------------------------------- #
class _Contract:
    """Mimics an Alpaca OptionContract enough for the pure mappers (type is enum-like)."""
    def __init__(self, symbol, type_value, strike, expiry):
        self.symbol = symbol
        class _T:  # enum-like .value
            value = type_value
        self.type = _T()
        self.strike_price = strike
        self.expiration_date = date.fromisoformat(expiry)


def test_contract_to_metadata_chain_row_has_no_greeks_and_no_quotes_without_premium():
    """A historical chain row carries occ/type/strike/expiry; open_interest/volume are ALWAYS
    None (Alpaca has no as-of OI/volume for a past date — nothing can derive those from price
    alone). With no as-of premium, greeks can't be Black-Scholes-inverted either (no price to
    invert), and bid/ask/last stay None too (contract not selectable that day)."""
    c = _Contract("AAPL240315C00180000", "call", "180", "2024-03-15")
    row = contract_to_metadata_chain_row(c, "AAPL")
    assert row["occ_symbol"] == "AAPL240315C00180000"
    assert row["option_type"] == "call"
    assert row["strike"] == 180.0 and isinstance(row["strike"], float)
    assert row["expiry"] == "2024-03-15"
    for k in ("iv", "delta", "gamma", "theta", "vega", "open_interest", "volume",
              "bid", "ask", "last"):
        assert row[k] is None, f"expected {k} None in a historical chain row (no premium)"


def test_contract_to_metadata_chain_row_fills_quotes_from_as_of_premium():
    """When the as-of premium (the start-date bar close) is supplied, bid/ask/last are set to it
    (a zero-spread historical-premium proxy) so the option ENTRY action — which requires a
    non-None ask to size+price — can trade. Greeks/iv/oi STAY None here because as_of_date/
    underlying_close are NOT supplied (see the next test for the computed path) and
    open_interest/volume are always None regardless (no as-of OI/volume exists to derive)."""
    c = _Contract("AAPL240315C00180000", "call", "180", "2024-03-15")
    row = contract_to_metadata_chain_row(c, "AAPL", as_of_premium=4.6)
    assert row["bid"] == 4.6 and row["ask"] == 4.6 and row["last"] == 4.6
    for k in ("iv", "delta", "gamma", "theta", "vega", "open_interest", "volume"):
        assert row[k] is None, f"expected {k} None without as_of_date+underlying_close"


def test_contract_to_metadata_chain_row_computes_greeks_when_asked():
    """With as_of_date + underlying_close supplied, the chain row's iv/greeks are computed from
    the as-of premium via Black-Scholes inversion (option_greeks.py) — real point-in-time
    greeks, not vendor-supplied and not None-by-default."""
    c = _Contract("AAPL250305C00100000", "call", "100", "2025-03-05")
    row = contract_to_metadata_chain_row(
        c, "AAPL", as_of_premium=10.4506, as_of_date=date(2024, 3, 5),
        underlying_close=100.0, risk_free_rate=0.05)
    assert row["iv"] is not None and abs(row["iv"] - 0.20) < 0.01
    assert row["delta"] is not None and 0.6 < row["delta"] < 0.7
    # open_interest/volume are STILL always None — no as-of OI/volume to derive from price.
    assert row["open_interest"] is None and row["volume"] is None


def test_nearest_on_or_before_finds_prior_date_within_window():
    series = {"2024-03-01": 100.0, "2024-03-04": 103.0}
    # Exact hit.
    assert _nearest_on_or_before(series, date(2024, 3, 4)) == 103.0
    # Weekend/holiday gap -> falls back to the nearest PRIOR date.
    assert _nearest_on_or_before(series, date(2024, 3, 3)) == 100.0
    # Outside the lookback window -> None, not a stale value from further back.
    assert _nearest_on_or_before(series, date(2024, 3, 20), max_back_days=7) is None


def test_contract_to_metadata_chain_row_accepts_string_type_and_expiry():
    """The mapper also serves already-normalised string type/expiry (so a test stub or a
    pre-normalised contract maps identically — no enum/date required)."""
    class _Plain:
        symbol = "MSFT240419P00400000"
        type = "put"                       # plain string, no .value
        strike_price = 400.0
        expiration_date = "2024-04-19"     # plain ISO string, no .isoformat
    row = contract_to_metadata_chain_row(_Plain(), "MSFT")
    assert row["option_type"] == "put" and row["expiry"] == "2024-04-19"
    assert row["strike"] == 400.0


def test_merge_contracts_keeps_expired_and_dedups():
    """INACTIVE (expired) + ACTIVE merged on OCC symbol; first-seen wins (INACTIVE passed
    first → the historical contract is kept), and overlapping symbols are deduped."""
    expired = [
        _Contract("AAPL240315C00180000", "call", "180", "2024-03-15"),
        _Contract("AAPL240315C00190000", "call", "190", "2024-03-15"),
    ]
    active = [
        _Contract("AAPL240315C00190000", "call", "190", "2024-03-15"),  # dup of an expired one
        _Contract("AAPL260116C00200000", "call", "200", "2026-01-16"),  # still-listed, unique
    ]
    merged = merge_contracts_by_symbol(expired, active)
    syms = [c.symbol for c in merged]
    assert syms == [
        "AAPL240315C00180000",
        "AAPL240315C00190000",
        "AAPL260116C00200000",
    ], "expected expired contracts kept, dup removed, active-unique appended (order stable)"


def test_merge_contracts_handles_empty_lists():
    assert merge_contracts_by_symbol([], None) == []
    one = [_Contract("AAPL240315C00180000", "call", "180", "2024-03-15")]
    assert [c.symbol for c in merge_contracts_by_symbol(one, [])] == ["AAPL240315C00180000"]


def test_is_standard_occ_filters_adjusted_contracts():
    """Standard OCC symbols pass; corporate-action ADJUSTED roots (numeric/extra-char prefix,
    which the bars endpoint 400s) are rejected so the build doesn't crash on one symbol."""
    assert is_standard_occ("AAPL240315C00180000")
    assert is_standard_occ("MSFT240419P00400000")
    assert is_standard_occ("F240315C00012000")          # 1-char root
    assert not is_standard_occ("1AAPL240429P00170000")  # adjusted: leading digit (the real one we hit)
    assert not is_standard_occ("AAPL240315X00180000")   # bad: neither C nor P
    assert not is_standard_occ("AAPL240315C0018000")    # bad: only 7 strike digits
    assert not is_standard_occ("")
    assert not is_standard_occ("NOTANOPTION")
