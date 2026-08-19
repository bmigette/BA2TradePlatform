"""Tests for ba2_providers.symbol_snapshot (SYMBOL360 direct-fetch helpers).

All FMP access is monkeypatched — no live key / network required. Follows
this repo's convention (see packages/experts/tests/test_earnings_drift_export.py
and tests/test_fmp_company_details.py) of patching the callee attribute
directly on the module object under test:
``monkeypatch.setattr(S.fmpsdk, "quote", ...)``.
"""
import ba2_providers.symbol_snapshot as S


# ---------------------------------------------------------------------------
# fetch_quote / fetch_profile
# ---------------------------------------------------------------------------

def test_fetch_quote_returns_first_element(monkeypatch):
    monkeypatch.setattr(
        S.fmpsdk, "quote",
        lambda apikey, symbol: [{"symbol": "AAPL", "price": 150.0}],
    )
    result = S.fetch_quote("key", "AAPL")
    assert result == {"symbol": "AAPL", "price": 150.0}


def test_fetch_quote_empty_list_returns_none(monkeypatch):
    monkeypatch.setattr(S.fmpsdk, "quote", lambda apikey, symbol: [])
    assert S.fetch_quote("key", "NOPE") is None


def test_fetch_quote_non_list_returns_none(monkeypatch):
    monkeypatch.setattr(S.fmpsdk, "quote", lambda apikey, symbol: None)
    assert S.fetch_quote("key", "NOPE") is None


def test_fetch_profile_returns_first_element(monkeypatch):
    monkeypatch.setattr(
        S.fmpsdk, "company_profile",
        lambda apikey, symbol: [{"symbol": "AAPL", "companyName": "Apple Inc."}],
    )
    result = S.fetch_profile("key", "AAPL")
    assert result == {"symbol": "AAPL", "companyName": "Apple Inc."}


def test_fetch_profile_empty_list_returns_none(monkeypatch):
    monkeypatch.setattr(S.fmpsdk, "company_profile", lambda apikey, symbol: [])
    assert S.fetch_profile("key", "NOPE") is None


def test_fetch_profile_non_list_returns_none(monkeypatch):
    monkeypatch.setattr(S.fmpsdk, "company_profile", lambda apikey, symbol: {"error": "x"})
    assert S.fetch_profile("key", "NOPE") is None


# ---------------------------------------------------------------------------
# rvol_from_quote
# ---------------------------------------------------------------------------

def test_rvol_from_quote_normal_case():
    quote = {"volume": 2_000_000, "avgVolume": 1_000_000}
    assert S.rvol_from_quote(quote) == 2.0


def test_rvol_from_quote_zero_avg_volume_returns_none():
    quote = {"volume": 500_000, "avgVolume": 0}
    assert S.rvol_from_quote(quote) is None


def test_rvol_from_quote_negative_avg_volume_returns_none():
    quote = {"volume": 500_000, "avgVolume": -1}
    assert S.rvol_from_quote(quote) is None


def test_rvol_from_quote_missing_keys_returns_none():
    # Missing keys fall back to 0 via `.get(...) or 0`, so avgVolume <= 0 -> None.
    assert S.rvol_from_quote({}) is None


def test_rvol_from_quote_missing_volume_only_is_zero():
    # avgVolume present and positive, volume missing -> treated as 0 volume -> RVOL 0.0.
    quote = {"avgVolume": 1_000_000}
    assert S.rvol_from_quote(quote) == 0.0


# ---------------------------------------------------------------------------
# weinstein_from_closes
# ---------------------------------------------------------------------------

def test_weinstein_from_closes_stage2_is_buy():
    """Clean rising series above a rising 150-SMA -> Stage 2 -> buy."""
    closes = [10 + i * 0.5 for i in range(200)]
    result = S.weinstein_from_closes(closes)
    assert result["stage"] == 2
    assert result["signal"] == "buy"


def test_weinstein_from_closes_stage4_is_sell():
    """Clean falling series below a falling SMA -> Stage 4 -> sell."""
    closes = [200 - i * 0.5 for i in range(200)]
    result = S.weinstein_from_closes(closes)
    assert result["stage"] == 4
    assert result["signal"] == "sell"


def test_weinstein_from_closes_stage1_is_neutral():
    """Flat series (price ~= flat SMA, no trend) -> Stage 1 -> neutral."""
    closes = [50.0] * 200
    result = S.weinstein_from_closes(closes)
    assert result["stage"] == 1
    assert result["signal"] == "neutral"


def test_weinstein_from_closes_stage3_is_neutral():
    """Rise then plateau, price nudged just above the flattened SMA -> Stage 3 -> neutral."""
    rise = [10 + i * 0.5 for i in range(60)]
    plateau_val = rise[-1]
    closes = rise + [plateau_val] * 160
    closes[-1] = plateau_val * 1.01
    result = S.weinstein_from_closes(closes)
    assert result["stage"] == 3
    assert result["signal"] == "neutral"


def test_weinstein_from_closes_insufficient_history_is_neutral():
    """Too few bars -> stage None -> neutral (not buy/sell on missing data)."""
    result = S.weinstein_from_closes([1.0, 2.0, 3.0])
    assert result["stage"] is None
    assert result["signal"] == "neutral"
