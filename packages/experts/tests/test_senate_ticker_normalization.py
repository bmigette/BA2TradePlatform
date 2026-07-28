"""Share-class tickers: the disclosure feed's BRK/B is the price cache's BRK-B (2026-07-28).

REGRESSION. Congressional disclosures write share classes with a SLASH (``BRK/B``). FMP price
history and the platform's own OHLCV cache both use a HYPHEN -- ``historical_price_full__BRK-B.json``
and ``BRK-B_5min.parquet`` are the real files on disk. Nothing normalised between the two, so every
``BRK/B`` disclosure failed its exec-price lookup and was dropped by ``_filter_trades``' silent
``if not exec_price: continue``.

Measured over a 365-day window of the real feed: ``BRK/B`` was the single largest coverage gap at
31 disclosures -- ~4x the next symbol, and Berkshire B is one of the most commonly disclosed
congressional holdings. It was invisible at EVERY window length, so no lookback setting exposed it.

The gap only reaches the trading path in BASKET mode (which discovers symbols from the feed);
``BRK`` is absent from the static senate universe, which is why a per-symbol run never surfaced it.
"""
from datetime import datetime, timezone

from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _expert():
    return FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)


def _t(who, sym, ttype, date, amount="$15,001 - $50,000"):
    return {"representative": who, "symbol": sym, "type": ttype,
            "transactionDate": date, "amount": amount}


# --------------------------------------------------------------------------- #
# the primitive
# --------------------------------------------------------------------------- #
def test_slash_becomes_hyphen():
    assert FMPSenateTraderWeight._normalize_ticker("BRK/B") == "BRK-B"


def test_already_canonical_is_unchanged():
    assert FMPSenateTraderWeight._normalize_ticker("BRK-B") == "BRK-B"


def test_a_dot_is_never_rewritten():
    """Dots are MEANINGFUL to FMP, so mapping them to hyphens would break working tickers. Of the
    27 dotted tickers in the price cache, 8 carry real data: dotted share classes that genuinely
    resolve (CWEN.A, HEI.A, RDS.A, RDS.B) and exchange suffixes (RY.TO, VUL.V). Rewriting ``.``
    would break all 8 and fix nothing, because the disclosure feed writes slashes."""
    for sym in ("BRK.B", "CWEN.A", "HEI.A", "RDS.B", "RY.TO", "VUL.V"):
        assert FMPSenateTraderWeight._normalize_ticker(sym) == sym


def test_a_slashed_ticker_lands_on_the_spelling_that_has_data():
    """BRK.B exists in the cache only as a 2-byte empty sentinel -- FMP has no data under it.
    BRK-B has 7600 rows. So the slash must map to the HYPHEN specifically."""
    assert FMPSenateTraderWeight._normalize_ticker("BRK/B") == "BRK-B" != "BRK.B"


def test_plain_tickers_are_untouched():
    for sym in ("AAPL", "UBER", "NVDA"):
        assert FMPSenateTraderWeight._normalize_ticker(sym) == sym


def test_case_and_whitespace_are_still_handled():
    """It REPLACES the old ``.upper().strip()`` at every call site, so it must keep doing that."""
    assert FMPSenateTraderWeight._normalize_ticker("  brk/b  ") == "BRK-B"


def test_empty_is_empty_not_an_error():
    assert FMPSenateTraderWeight._normalize_ticker("") == ""
    assert FMPSenateTraderWeight._normalize_ticker(None) == ""


# --------------------------------------------------------------------------- #
# the consumers -- a normalized key must not desync from a raw comparison
# --------------------------------------------------------------------------- #
def test_still_held_matches_a_slashed_disclosure_against_a_canonical_symbol():
    """THE DESYNC RISK of this fix: basket mode now groups under the canonical ``BRK-B``, so a
    netting pass that still compared raw would find NO trades and report the position as not
    held -- turning a coverage bug into a wrong-signal bug."""
    e = _expert()
    held = e._still_held_by([_t("Alice", "BRK/B", "purchase", "2024-01-10")], "BRK-B", now=NOW)
    assert held == {"Alice": True}


def test_still_held_nets_a_slashed_sale_against_a_canonical_buy():
    """Both directions of the mismatch, on the same trader."""
    e = _expert()
    trades = [_t("Alice", "BRK-B", "purchase", "2024-01-10"),
              _t("Alice", "BRK/B", "sale", "2024-03-10")]
    assert e._still_held_by(trades, "BRK-B", now=NOW) == {"Alice": False}


def test_trade_key_collapses_the_two_spellings():
    """``_trade_key`` indexes the exec-price map; two spellings of one trade must not produce two
    entries, or the price resolved under one is invisible to the lookup under the other."""
    e = _expert()
    assert (e._trade_key(_t("Alice", "BRK/B", "purchase", "2024-01-10"))
            == e._trade_key(_t("Alice", "BRK-B", "purchase", "2024-01-10")))


def test_hold_days_pairs_a_slashed_buy_with_a_canonical_sale():
    """FIFO round-trip pairing is per symbol; if the spellings don't collapse, the buy and the
    sale land in different buckets and the round-trip is never counted -- which would silently
    change who the scalper filter (min_trader_avg_hold_days) excludes."""
    history = [_t("Alice", "BRK/B", "purchase", "2024-01-01"),
               _t("Alice", "BRK-B", "sale", "2024-02-01")]
    info = FMPSenateTraderWeight._calculate_trader_avg_hold_days(history)
    assert info["roundtrips"] == 1
    assert info["avg_hold_days"] == 31


def test_buy_candidates_are_emitted_canonical():
    """These feed the skill scorer's price lookups; emitting BRK/B here would re-introduce the
    original miss one layer down."""
    candidates, _ = FMPSenateTraderWeight._sorted_buy_candidates(
        [_t("Alice", "BRK/B", "purchase", "2024-01-10")])
    assert [sym for _, sym in candidates] == ["BRK-B"]
