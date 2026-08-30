"""TastyTrade/dxfeed historical-options provider — everything except the socket.

Context. The incumbent cache has ``delta``, ``iv`` and ``open_interest`` NULL across all
6,757,055 chain rows, so ``method="delta"`` selects nothing and ``min_open_interest``
rejects the whole chain. A read-only probe (``test_files/probe_tastytrade_option_history.py``)
established that dxfeed DOES serve daily candles for contracts that have already EXPIRED and
that those candles carry ``imp_volatility`` and ``open_interest`` — the two fields that cannot
be recovered from OHLC. This provider turns that into a cache builder.

NOTHING here touches the network. Candles are built with ``Candle.from_stream`` from RAW WIRE
PAYLOADS in dxfeed's field order — not python kwargs — because that is the code path the
streamer actually takes (``model_validator``/``field_validator(mode="before")`` included), and
kwargs would silently skip it.
"""
from datetime import date, datetime, timezone

import pytest

from ba2_common.core.interfaces import OptionContractMeta, OptionsDataProviderInterface
from ba2_providers import OPTIONS_PROVIDERS, get_provider
from ba2_providers.options.tastytrade import (
    StreamInterrupted, TastyTradeOptionsProvider, candle_to_bar, expiry_calendar,
    is_empty_snapshot, occ_symbol, occ_to_streamer, parse_occ, strike_ladder,
    streamer_to_occ, strip_candle_suffix, _run_sync,
)

# dxfeed IndexedEvent flags
SNAPSHOT_BEGIN, SNAPSHOT_END, REMOVE, SNAPSHOT_SNIP = 0x4, 0x8, 0x2, 0x10


def _candle(symbol, *, time_ms, close="7.25", iv="0.2841", oi="12345", flags=0,
            volume="911", open_="7.0", high="7.5", low="6.8"):
    """One dxfeed Candle from a RAW wire payload, in the exact COMPACT field order the
    streamer negotiates (``Candle.model_fields`` order)."""
    from tastytrade.dxfeed import Candle
    payload = [
        symbol,        # eventSymbol
        1700000000000,  # eventTime
        flags,         # eventFlags
        1,             # index
        time_ms,       # time
        0,             # sequence
        1,             # count
        volume, "7.1", "NaN", "NaN",   # volume, vwap, bidVolume, askVolume
        iv, oi,        # impVolatility, openInterest
        open_, high, low, close,
    ]
    return Candle.from_stream(payload)[0]


def _day_ms(y, m, d, hour=14):
    return int(datetime(y, m, d, hour, 30, tzinfo=timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# the OPTIONS_PROVIDERS contract ThetaDataOptionsProvider already implements
# --------------------------------------------------------------------------- #
def test_registered_in_the_existing_options_seam_alongside_the_incumbents():
    assert set(OPTIONS_PROVIDERS) == {"alpaca", "thetadata", "tastytrade"}
    assert OPTIONS_PROVIDERS["tastytrade"] is TastyTradeOptionsProvider


def test_get_provider_constructs_it_with_no_arguments_and_no_connection():
    """The registry instantiates bare. Opening a session at construction would make a mere
    ``get_provider`` call hit the network."""
    p = get_provider("options", "tastytrade")
    assert isinstance(p, OptionsDataProviderInterface)
    assert isinstance(p, TastyTradeOptionsProvider)
    assert p.name == "tastytrade"


def test_it_implements_every_abstract_method():
    assert not getattr(TastyTradeOptionsProvider, "__abstractmethods__", set())


def test_history_floor_is_the_measured_iv_coverage_floor_not_the_price_floor():
    """Prices go back further, but IV — the reason for the exercise — floors out around
    October 2022. Claiming depth we have no IV for would build a cache whose leading
    months silently cannot do delta selection."""
    p = TastyTradeOptionsProvider()
    assert p.history_floor() == date(2022, 10, 1)
    assert p.history_floor() < date(2024, 1, 18), "must beat Alpaca's measured floor"


def test_history_floor_is_overridable_for_a_probe_that_moves_it():
    assert TastyTradeOptionsProvider(
        history_floor_date=date(2021, 1, 1)).history_floor() == date(2021, 1, 1)


def test_history_floor_honours_the_environment_override(monkeypatch):
    """The floor is MEASURED, so a later probe that pushes it must be able to move it
    without a code change."""
    monkeypatch.setenv("TASTYTRADE_OPTIONS_HISTORY_FLOOR", "2021-06-15")
    assert TastyTradeOptionsProvider().history_floor() == date(2021, 6, 15)


def test_an_unparseable_environment_floor_falls_back_instead_of_crashing(monkeypatch):
    """A typo in an env var must not take down a 40-hour backfill on startup — and must not
    silently become "no floor" either."""
    monkeypatch.setenv("TASTYTRADE_OPTIONS_HISTORY_FLOOR", "not-a-date")
    assert TastyTradeOptionsProvider().history_floor() == date(2022, 10, 1)


def test_an_explicit_floor_beats_the_environment(monkeypatch):
    monkeypatch.setenv("TASTYTRADE_OPTIONS_HISTORY_FLOOR", "2021-06-15")
    assert TastyTradeOptionsProvider(
        history_floor_date=date(2023, 1, 1)).history_floor() == date(2023, 1, 1)


# --------------------------------------------------------------------------- #
# symbol construction — the probe's format, cross-checked against the SDK
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("occ,expected", [
    ("AAPL230120C00150000", ".AAPL230120C150"),
    ("SPY240621P00545500", ".SPY240621P545.5"),
    ("BAC240517C00041000", ".BAC240517C41"),
    ("TSLA230120P00102500", ".TSLA230120P102.5"),
    ("NVDA260116C01200000", ".NVDA260116C1200"),
    ("F230120C00012500", ".F230120C12.5"),
])
def test_occ_to_streamer_matches_the_probe_format(occ, expected):
    assert occ_to_streamer(occ) == expected


@pytest.mark.parametrize("occ", [
    "AAPL230120C00150000", "SPY240621P00545500", "BAC240517C00041000",
    "TSLA230120P00102500", "F230120C00012500", "GOOGL250117C00200000",
])
def test_occ_to_streamer_agrees_with_the_tastytrade_sdk(occ):
    """The SDK's own converter takes the SPACE-PADDED 21-char OCC form; this repo stores the
    unpadded form everywhere (see ThetaData's ``_occ_symbol``). Same answer either way — if
    they ever diverge the streamer silently returns nothing for every contract."""
    from tastytrade.instruments import Option
    root = occ[:-15]
    padded = root.ljust(6) + occ[len(root):]
    assert occ_to_streamer(occ) == Option.occ_to_streamer_symbol(padded)


def test_expired_contract_symbols_are_built_the_same_way_as_live_ones():
    """dxfeed serving DEAD contracts is what makes a historical backfill possible at all, and
    it keys them on the same streamer symbol — there is no separate 'expired' namespace."""
    long_dead = occ_symbol("AAPL", date(2023, 1, 20), "C", 150.0)
    assert long_dead == "AAPL230120C00150000"
    assert occ_to_streamer(long_dead) == ".AAPL230120C150"


@pytest.mark.parametrize("streamer,occ", [
    (".AAPL230120C150", "AAPL230120C00150000"),
    (".SPY240621P545.5", "SPY240621P00545500"),
    (".TSLA230120P102.5", "TSLA230120P00102500"),
])
def test_streamer_to_occ_round_trips(streamer, occ):
    assert streamer_to_occ(streamer) == occ
    assert occ_to_streamer(occ) == streamer


def test_the_candle_interval_suffix_is_stripped_before_matching():
    """dxfeed echoes the subscription back as ``.AAPL230120C150{=1d,tho=true}``. Matching on
    the raw event symbol drops every bar on the floor."""
    assert strip_candle_suffix(".AAPL230120C150{=1d,tho=true}") == ".AAPL230120C150"
    assert strip_candle_suffix(".AAPL230120C150") == ".AAPL230120C150"


def test_occ_symbol_encodes_fractional_strikes_without_float_drift():
    assert occ_symbol("SPY", date(2024, 6, 21), "put", 545.5) == "SPY240621P00545500"
    assert occ_symbol("XYZ", date(2024, 6, 21), "C", 0.5) == "XYZ240621C00000500"
    assert occ_symbol("XYZ", date(2024, 6, 21), "C", 1234.56) == "XYZ240621C01234560"


@pytest.mark.parametrize("strike,expected", [
    # Post-corporate-action strikes land on penny increments, and these are exactly the
    # values where `int(strike * 1000)` truncates one thousandth low (2.01 * 1000 is
    # 2009.9999999999998 in binary float). One wrong digit is a symbol that does not exist,
    # so the contract silently returns no bars and is recorded as permanently empty.
    (2.01, "XYZ240621C00002010"),
    (2.03, "XYZ240621C00002030"),
    (4.02, "XYZ240621C00004020"),
    (8.03, "XYZ240621C00008030"),
])
def test_occ_symbol_survives_the_binary_float_truncation_cases(strike, expected):
    assert int(strike * 1000) != int(round(strike * 1000)), \
        "this strike is only interesting because naive float math drifts on it"
    assert occ_symbol("XYZ", date(2024, 6, 21), "C", strike) == expected


def test_parse_occ_recovers_the_contract_identity():
    m = parse_occ("SPY240621P00545500")
    assert (m.underlying, m.option_type, m.strike, m.expiry) == \
        ("SPY", "put", 545.5, date(2024, 6, 21))


def test_parse_occ_accepts_the_full_one_to_six_character_root_range():
    """OCC roots run 1-6 characters ('F' through 'GOOGL'/'BRKB '); rejecting the long ones
    would silently drop entire underlyings from the build."""
    assert parse_occ("F230120C00012500").underlying == "F"
    assert parse_occ("GOOGL250117C00200000").underlying == "GOOGL"
    assert parse_occ("XXAAPL230120C00150000").underlying == "XXAAPL"


@pytest.mark.parametrize("bad", [
    "", "AAPL", "AAPL230120X00150000", ".AAPL230120C150",
    # Anchored on BOTH ends: an unanchored pattern would happily find a valid OCC symbol
    # embedded in a longer string and return a contract nobody asked for.
    ".AAPL230120C00150000", "AAPL230120C00150000 ADJ",
    "AAPL230120C001500009", "AAPL230120C0015000",
])
def test_parse_occ_rejects_garbage_instead_of_guessing(bad):
    with pytest.raises(ValueError):
        parse_occ(bad)


# --------------------------------------------------------------------------- #
# candle -> bar: iv and open_interest are the payload
# --------------------------------------------------------------------------- #
def test_a_candle_carries_iv_and_open_interest_through_to_the_bar():
    c = _candle(".AAPL230120C150{=1d,tho=true}", time_ms=_day_ms(2023, 1, 3),
                iv="0.2841", oi="12345")
    bar = candle_to_bar(c, "AAPL230120C00150000")
    assert bar.iv == pytest.approx(0.2841)
    assert bar.open_interest == 12345
    assert (bar.open, bar.high, bar.low, bar.close) == (7.0, 7.5, 6.8, 7.25)
    assert bar.volume == 911
    assert bar.bar_date == date(2023, 1, 3)
    assert bar.occ_symbol == "AAPL230120C00150000"


def test_the_bar_carries_no_synthetic_bid_ask():
    """There is no historical bid/ask for dead contracts. The incumbent cache set
    ``bid = ask = close``, which is why ZERO of 4,328,587 quoted rows have ask > bid.
    Absent is honest; a zero-width spread is a lie the arb guard cannot see."""
    c = _candle(".AAPL230120C150{=1d,tho=true}", time_ms=_day_ms(2023, 1, 3))
    bar = candle_to_bar(c, "AAPL230120C00150000")
    assert bar.bid is None and bar.ask is None


def test_a_NaN_iv_becomes_none_not_zero():
    """dxfeed sends the literal string 'NaN'. A 0.0 IV is not 'unknown', it is 'free'."""
    c = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), iv="NaN", oi="NaN")
    bar = candle_to_bar(c, "AAPL230120C00150000")
    assert bar.iv is None
    assert bar.open_interest is None
    assert bar.close == 7.25, "a missing IV must not discard the traded price"


def test_a_candle_with_no_close_is_not_a_usable_bar():
    """TRAP: the SDK annotates OHLC as ``ZeroFromNone``, so a wire 'NaN' close does NOT
    arrive as None — it arrives as ``Decimal(0)``. Passing that through would mint a
    $0.00 option, the exact artifact the arb guard exists to catch."""
    c = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), close="NaN")
    assert c.close == 0, "the SDK coerced the missing close to zero, as expected"
    assert candle_to_bar(c, "AAPL230120C00150000") is None


def test_a_zero_close_is_never_a_usable_bar():
    c = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), close="0")
    assert candle_to_bar(c, "AAPL230120C00150000") is None


def test_a_zeroed_open_high_low_falls_back_to_the_close_rather_than_to_zero():
    """Same coercion applies to open/high/low. A bar whose low is 0.0 but whose close is
    7.25 would look like a 100% intraday drawdown to anything reading the range."""
    c = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3),
                open_="NaN", high="NaN", low="NaN", close="7.25")
    bar = candle_to_bar(c, "AAPL230120C00150000")
    assert (bar.open, bar.high, bar.low, bar.close) == (7.25, 7.25, 7.25, 7.25)


def test_a_candle_with_no_timestamp_is_not_a_usable_bar():
    c = _candle(".AAPL230120C150{=1d}", time_ms=0)
    assert candle_to_bar(c, "AAPL230120C00150000") is None


def test_a_float_nan_iv_or_open_interest_becomes_none_not_a_number():
    """``candle_to_bar`` is duck-typed, so it must not assume the SDK already sanitised the
    field. The SDK maps the literal wire string 'NaN' to None, but a lower-case 'nan' parses
    straight into ``Decimal('NaN')``, and float(NaN) compares false against every threshold —
    an IV rank built on it is silently garbage rather than obviously missing."""
    from types import SimpleNamespace
    c = SimpleNamespace(event_flags=0, event_symbol=".AAPL230120C150", time=_day_ms(2023, 1, 3),
                        close="7.25", open="7.0", high="7.5", low="6.8", volume="911",
                        imp_volatility=float("nan"), open_interest=float("nan"))
    bar = candle_to_bar(c, "AAPL230120C00150000")
    assert bar.iv is None
    assert bar.open_interest is None
    assert bar.close == 7.25


def test_an_infinite_iv_is_also_rejected():
    from types import SimpleNamespace
    c = SimpleNamespace(event_flags=0, event_symbol=".AAPL230120C150", time=_day_ms(2023, 1, 3),
                        close="7.25", open="7.0", high="7.5", low="6.8", volume="911",
                        imp_volatility=float("inf"), open_interest=5)
    assert candle_to_bar(c, "AAPL230120C00150000").iv is None


def test_a_removal_event_is_not_a_bar():
    c = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), flags=REMOVE)
    assert candle_to_bar(c, "AAPL230120C00150000") is None


def test_an_empty_snapshot_marker_is_recognised():
    """dxfeed signals 'this contract has no history' with a single event carrying
    snapshotBegin + snapshotEnd + removeEvent. Without recognising it, a genuinely empty
    contract is indistinguishable from one that simply never answered."""
    empty = _candle(".AAPL230120C990{=1d}", time_ms=0,
                    flags=SNAPSHOT_BEGIN | SNAPSHOT_END | REMOVE)
    assert is_empty_snapshot(empty)
    real_end = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END)
    assert not is_empty_snapshot(real_end)
    plain = _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3))
    assert not is_empty_snapshot(plain)


def test_a_snipped_snapshot_also_ends_the_contract():
    snipped = _candle(".AAPL230120C990{=1d}", time_ms=0,
                      flags=SNAPSHOT_BEGIN | SNAPSHOT_SNIP | REMOVE)
    assert is_empty_snapshot(snipped)


# --------------------------------------------------------------------------- #
# fetch_eod_bars over a mocked collector
# --------------------------------------------------------------------------- #
CONTRACTS = [
    OptionContractMeta("AAPL230120C00150000", "AAPL", "call", 150.0, date(2023, 1, 20)),
    OptionContractMeta("AAPL230120C00990000", "AAPL", "call", 990.0, date(2023, 1, 20)),
]


def _provider(candles, *, interrupted=False, calls=None, **kw):
    p = TastyTradeOptionsProvider(**kw)

    def fake_collect(symbols, from_time):
        if calls is not None:
            calls.append((list(symbols), from_time))
        if interrupted:
            raise StreamInterrupted("socket dropped", candles)
        return candles

    p._collect = fake_collect  # type: ignore[assignment]
    return p


def test_fetch_eod_bars_yields_bars_for_the_contracts_that_have_data():
    candles = [
        _candle(".AAPL230120C150{=1d,tho=true}", time_ms=_day_ms(2023, 1, 3)),
        _candle(".AAPL230120C150{=1d,tho=true}", time_ms=_day_ms(2023, 1, 4), close="8.0",
                flags=SNAPSHOT_END),
        _candle(".AAPL230120C990{=1d,tho=true}", time_ms=0,
                flags=SNAPSHOT_BEGIN | SNAPSHOT_END | REMOVE),
    ]
    bars = list(_provider(candles).fetch_eod_bars(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31)))
    assert [b.occ_symbol for b in bars] == ["AAPL230120C00150000"] * 2
    assert [b.bar_date for b in bars] == [date(2023, 1, 3), date(2023, 1, 4)]


def test_fetch_eod_bars_honours_the_window():
    candles = [
        _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2022, 12, 30)),
        _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3)),
        _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 2, 1), flags=SNAPSHOT_END),
    ]
    bars = list(_provider(candles).fetch_eod_bars(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31)))
    assert [b.bar_date for b in bars] == [date(2023, 1, 3)]


def test_an_unrequested_symbol_in_the_stream_is_ignored():
    candles = [_candle(".MSFT230120C250{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END)]
    bars = list(_provider(candles).fetch_eod_bars(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31)))
    assert bars == []


def test_no_contracts_means_no_subscription_at_all():
    calls = []
    p = _provider([], calls=calls)
    assert list(p.fetch_eod_bars([], start=date(2023, 1, 1), end=date(2023, 1, 31))) == []
    assert calls == [], "an empty contract list must not open a stream"


def test_the_subscription_uses_streamer_symbols_and_starts_at_the_window_start():
    calls = []
    p = _provider([], calls=calls)
    list(p.fetch_eod_bars(CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31)))
    (symbols, from_time), = calls
    assert symbols == [".AAPL230120C150", ".AAPL230120C990"]
    assert from_time.date() == date(2023, 1, 1)
    assert from_time.tzinfo is not None, "dxfeed fromTime must be unambiguous"


def test_large_contract_sets_are_split_into_batches():
    calls = []
    many = [OptionContractMeta(occ_symbol("AAPL", date(2023, 1, 20), "C", float(s)),
                               "AAPL", "call", float(s), date(2023, 1, 20))
            for s in range(100, 130)]
    p = _provider([], calls=calls, batch_size=12)
    list(p.fetch_eod_bars(many, start=date(2023, 1, 1), end=date(2023, 1, 31)))
    assert [len(c[0]) for c in calls] == [12, 12, 6]


# --------------------------------------------------------------------------- #
# empty vs unresolved — what makes resume trustworthy
# --------------------------------------------------------------------------- #
def test_an_explicitly_empty_contract_is_reported_as_empty_not_unresolved():
    candles = [
        _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END),
        _candle(".AAPL230120C990{=1d}", time_ms=0,
                flags=SNAPSHOT_BEGIN | SNAPSHOT_END | REMOVE),
    ]
    batch = _provider(candles).fetch_bars_detailed(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31))
    assert batch.empty == {"AAPL230120C00990000"}
    assert batch.unresolved == set()
    assert batch.interrupted is False


def test_a_silent_contract_on_a_CLEAN_drain_is_empty():
    """A contract that never existed simply produces nothing. When the stream drained
    normally, silence IS the answer — recording it stops an eternal retry."""
    candles = [_candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END)]
    batch = _provider(candles).fetch_bars_detailed(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31))
    assert batch.empty == {"AAPL230120C00990000"}
    assert batch.unresolved == set()


def test_a_silent_contract_after_a_DROPPED_SOCKET_is_unresolved_never_empty():
    """THE resume-safety invariant. If a socket dies mid-symbol, the contracts that had not
    answered yet are UNKNOWN. Recording them as 'empty' would permanently bake a hole into
    the cache that no re-run ever revisits."""
    candles = [_candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3),
                       flags=SNAPSHOT_END)]
    batch = _provider(candles, interrupted=True).fetch_bars_detailed(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31))
    assert batch.interrupted is True
    assert batch.unresolved == {"AAPL230120C00990000"}
    assert batch.empty == set()
    assert [b.occ_symbol for b in batch.bars] == ["AAPL230120C00150000"], \
        "a contract that DID finish its snapshot before the drop is still kept"


def test_a_contract_interrupted_MID_SNAPSHOT_is_unresolved_even_though_it_sent_bars():
    """It sent SOME bars but never its end-of-snapshot marker, so its history is truncated.
    Keeping the partial rows AND calling it done would silently cache a short series."""
    candles = [
        _candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3)),
        _candle(".AAPL230120C990{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END),
    ]
    batch = _provider(candles, interrupted=True).fetch_bars_detailed(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31))
    assert batch.unresolved == {"AAPL230120C00150000"}
    assert batch.empty == set()
    assert "AAPL230120C00150000" not in {b.occ_symbol for b in batch.bars}, \
        "its rows are a TRUNCATED series and must not be merged into the partition"
    assert [b.occ_symbol for b in batch.bars] == ["AAPL230120C00990000"], \
        "the contract that DID finish is unaffected"


def test_strict_snapshot_mode_refuses_to_infer_emptiness_from_silence():
    candles = [_candle(".AAPL230120C150{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END)]
    batch = _provider(candles, strict_snapshot=True).fetch_bars_detailed(
        CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31))
    assert batch.unresolved == {"AAPL230120C00990000"}
    assert batch.empty == set()


def test_an_interrupted_batch_does_not_abort_the_remaining_batches():
    """One bad socket must not lose the work of the other batches in the same call."""
    calls = []
    p = TastyTradeOptionsProvider(batch_size=1)
    good = _candle(".AAPL230120C990{=1d}", time_ms=_day_ms(2023, 1, 3), flags=SNAPSHOT_END)

    def collect(symbols, from_time):
        calls.append(list(symbols))
        if symbols == [".AAPL230120C150"]:
            raise StreamInterrupted("boom", [])
        return [good]

    p._collect = collect  # type: ignore[assignment]
    batch = p.fetch_bars_detailed(CONTRACTS, start=date(2023, 1, 1), end=date(2023, 1, 31))
    assert len(calls) == 2
    assert batch.unresolved == {"AAPL230120C00150000"}
    assert [b.occ_symbol for b in batch.bars] == ["AAPL230120C00990000"]
    assert batch.interrupted is True


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
_INSTRUMENT_PAYLOAD = [
    # Raw /instruments/equity-options items, as the REST API returns them.
    {"symbol": "AAPL  230120C00150000", "underlying-symbol": "AAPL",
     "expiration-date": "2023-01-20", "strike-price": "150.0", "option-type": "C",
     "active": False, "shares-per-contract": 100},
    {"symbol": "AAPL  230120P00150000", "underlying-symbol": "AAPL",
     "expiration-date": "2023-01-20", "strike-price": "150.0", "option-type": "P",
     "active": False, "shares-per-contract": 100},
    {"symbol": "AAPL  230120C00200000", "underlying-symbol": "AAPL",
     "expiration-date": "2023-01-20", "strike-price": "200.0", "option-type": "C",
     "active": False, "shares-per-contract": 100},
    # A NON-STANDARD deliverable (post corporate action): 105 shares, ORDINARY root — so
    # only the shares-per-contract guard can catch it.
    {"symbol": "AAPL  230120C00160000", "underlying-symbol": "AAPL",
     "expiration-date": "2023-01-20", "strike-price": "160.0", "option-type": "C",
     "active": False, "shares-per-contract": 105},
    # An ADJUSTED root with a perfectly normal 100-share deliverable — so only the root
    # guard can catch it. Kept separate from the row above: a payload that trips BOTH
    # guards at once cannot tell you that either one works.
    {"symbol": "AAPL1 230120C00170000", "underlying-symbol": "AAPL",
     "expiration-date": "2023-01-20", "strike-price": "170.0", "option-type": "C",
     "active": False, "shares-per-contract": 100},
    # Outside the requested expiry window.
    {"symbol": "AAPL  240119C00150000", "underlying-symbol": "AAPL",
     "expiration-date": "2024-01-19", "strike-price": "150.0", "option-type": "C",
     "active": True, "shares-per-contract": 100},
]


def _discovery_provider(payload, calls=None):
    p = TastyTradeOptionsProvider()

    def fake_list(underlying, expiry_gte, expiry_lte):
        if calls is not None:
            calls.append((underlying, expiry_gte, expiry_lte))
        return payload

    p._list_instruments = fake_list  # type: ignore[assignment]
    return p


def test_discovery_parses_the_raw_instrument_payload():
    got = _discovery_provider(_INSTRUMENT_PAYLOAD).discover_contracts(
        "AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1))
    assert {c.occ_symbol for c in got} == {
        "AAPL230120C00150000", "AAPL230120P00150000", "AAPL230120C00200000"}
    put = next(c for c in got if c.option_type == "put")
    assert (put.underlying, put.strike, put.expiry) == ("AAPL", 150.0, date(2023, 1, 20))


def test_discovery_must_include_expired_contracts():
    """Every contract in a 2023 window is dead today. A listing call that defaults to
    'currently tradable' returns an empty chain and builds a silently empty cache."""
    calls = []
    p = TastyTradeOptionsProvider()
    seen = {}

    def fake_get(path, params):
        seen.update(params)
        return {"data": {"items": _INSTRUMENT_PAYLOAD}, "pagination": {"page-offset": 0,
                                                                      "total-pages": 1}}

    p._rest_get = fake_get  # type: ignore[assignment]
    p.discover_contracts("AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1))
    assert seen.get("with-expired") is True
    assert calls == []


def test_discovery_drops_a_non_standard_deliverable_even_under_an_ordinary_root():
    """A 105-share contract does not price like the 100-share one and is not what any
    strategy selects. Its root looks completely normal, so only the deliverable check
    can reject it."""
    got = _discovery_provider(_INSTRUMENT_PAYLOAD).discover_contracts(
        "AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1))
    assert "AAPL230120C00160000" not in {c.occ_symbol for c in got}


def test_discovery_drops_an_adjusted_root_even_with_a_standard_deliverable():
    """Alpaca's builder rejects the same class ('1SPY...'); here it is a trailing digit
    ('AAPL1'). The deliverable is a normal 100 shares, so only the root check catches it."""
    got = _discovery_provider(_INSTRUMENT_PAYLOAD).discover_contracts(
        "AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1))
    assert "AAPL1230120C00170000" not in {c.occ_symbol for c in got}
    assert all(c.underlying == "AAPL" for c in got)


def test_discovery_applies_strike_and_expiry_filters():
    p = _discovery_provider(_INSTRUMENT_PAYLOAD)
    got = p.discover_contracts("AAPL", expiry_gte=date(2023, 1, 1),
                               expiry_lte=date(2023, 2, 1), strike_min=175.0)
    assert [c.occ_symbol for c in got] == ["AAPL230120C00200000"]
    assert p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1),
                                expiry_lte=date(2025, 2, 1)) == []


def test_max_contracts_keeps_the_strikes_nearest_the_band_centre():
    """Near-the-money is what strategies select; an arbitrary slice would cap the build with
    the wings and throw away everything usable. Same rule ThetaData's provider follows.

    The payload deliberately lists the WINGS FIRST, so a plain ``contracts[:n]`` slice would
    return exactly the wrong two and still look like it worked on a payload that happened to
    be sorted helpfully."""
    wings_first = [
        {"symbol": "AAPL  230120C00100000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "100.0", "option-type": "C",
         "shares-per-contract": 100},
        {"symbol": "AAPL  230120C00200000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "200.0", "option-type": "C",
         "shares-per-contract": 100},
        {"symbol": "AAPL  230120C00150000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "150.0", "option-type": "C",
         "shares-per-contract": 100},
        {"symbol": "AAPL  230120P00150000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "150.0", "option-type": "P",
         "shares-per-contract": 100},
    ]
    p = _discovery_provider(wings_first)
    got = p.discover_contracts("AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1),
                               strike_min=100.0, strike_max=200.0, max_contracts=2)
    assert {c.strike for c in got} == {150.0}, [c.occ_symbol for c in got]


def test_max_contracts_falls_back_to_the_median_strike_without_an_explicit_band():
    wings_first = [
        {"symbol": "AAPL  230120C00100000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "100.0", "option-type": "C",
         "shares-per-contract": 100},
        {"symbol": "AAPL  230120C00150000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "150.0", "option-type": "C",
         "shares-per-contract": 100},
        {"symbol": "AAPL  230120C00155000", "underlying-symbol": "AAPL",
         "expiration-date": "2023-01-20", "strike-price": "155.0", "option-type": "C",
         "shares-per-contract": 100},
    ]
    got = _discovery_provider(wings_first).discover_contracts(
        "AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1), max_contracts=2)
    assert sorted(c.strike for c in got) == [150.0, 155.0]


def test_a_duplicated_instrument_row_yields_one_contract():
    """Paginated listings can repeat a row across page boundaries; two identical contracts
    would be subscribed twice and written twice."""
    dupes = [_INSTRUMENT_PAYLOAD[0], dict(_INSTRUMENT_PAYLOAD[0])]
    got = _discovery_provider(dupes).discover_contracts(
        "AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1))
    assert [c.occ_symbol for c in got] == ["AAPL230120C00150000"]


def test_discovery_paginates_until_the_last_page():
    pages = [
        {"data": {"items": _INSTRUMENT_PAYLOAD[:2]},
         "pagination": {"page-offset": 0, "total-pages": 2}},
        {"data": {"items": _INSTRUMENT_PAYLOAD[2:]},
         "pagination": {"page-offset": 1, "total-pages": 2}},
    ]
    seen = []
    p = TastyTradeOptionsProvider()

    def fake_get(path, params):
        seen.append(params["page-offset"])
        return pages[params["page-offset"]]

    p._rest_get = fake_get  # type: ignore[assignment]
    got = p.discover_contracts("AAPL", expiry_gte=date(2023, 1, 1), expiry_lte=date(2023, 2, 1))
    assert seen == [0, 1]
    assert len(got) == 3


# --------------------------------------------------------------------------- #
# synthetic (offline) discovery — the fallback that needs no listing endpoint
# --------------------------------------------------------------------------- #
def test_expiry_calendar_lists_every_friday_in_the_window():
    got = expiry_calendar(date(2023, 1, 1), date(2023, 1, 31))
    assert got == [date(2023, 1, 6), date(2023, 1, 13), date(2023, 1, 20), date(2023, 1, 27)]


def test_expiry_calendar_is_inclusive_of_a_friday_endpoint():
    assert expiry_calendar(date(2023, 1, 6), date(2023, 1, 6)) == [date(2023, 1, 6)]


def test_expiry_calendar_rejects_a_reversed_window():
    with pytest.raises(ValueError):
        expiry_calendar(date(2023, 2, 1), date(2023, 1, 1))


def test_strike_ladder_brackets_the_price_range_on_a_round_increment():
    got = strike_ladder(140.0, 160.0, band_pct=10.0, increment=5.0)
    assert got[0] <= 126.0 and got[-1] >= 176.0
    assert all(abs(round(s / 5.0) * 5.0 - s) < 1e-9 for s in got)
    assert got == sorted(set(got))


def test_strike_ladder_increment_scales_with_price_level():
    penny = strike_ladder(4.0, 6.0, band_pct=20.0)
    mega = strike_ladder(900.0, 1100.0, band_pct=20.0)
    assert (penny[1] - penny[0]) < (mega[1] - mega[0])


def test_strike_ladder_rejects_a_non_positive_price():
    with pytest.raises(ValueError):
        strike_ladder(0.0, 10.0, band_pct=10.0)


# --------------------------------------------------------------------------- #
# ssl — the failure the probe had to diagnose the hard way
# --------------------------------------------------------------------------- #
def test_the_streamer_ssl_context_is_pinned_to_certifi():
    """The system trust store picks up a corporate root for the WebSocket while httpx uses
    certifi, so REST succeeds and the stream silently does not. Pin certifi for both."""
    import certifi
    p = TastyTradeOptionsProvider()
    ctx = p.ssl_context()
    assert ctx is not None
    loaded = {c.get("subject") for c in ctx.get_ca_certs()}
    reference = __import__("ssl").create_default_context(cafile=certifi.where())
    assert loaded == {c.get("subject") for c in reference.get_ca_certs()}


# --------------------------------------------------------------------------- #
# _run_sync — the warm-up's "Event loop is closed" incident (2026-08-30)
# --------------------------------------------------------------------------- #
# _require_session() caches self._session (and, inside the tastytrade SDK's Session, a
# long-lived httpx.AsyncClient) across every call for the life of a provider instance --
# warm_options_history.py builds ONE provider and calls fetch_bars_detailed on it for
# thousands of units. httpx.AsyncClient is not safe to reuse across different event loops:
# whatever loop first touches its internal connection pool is the only loop it can ever be
# used from again. A fresh `asyncio.run()` per _run_sync call creates AND CLOSES a new loop
# every time, so the cached session's client is bound to a loop that no longer exists by the
# second call -- observed live as "RuntimeError: Event loop is closed" on every batch after
# the first, across all 8 warm-up workers. _run_sync must reuse ONE persistent loop per
# process instead.
def test_run_sync_reuses_one_persistent_loop_across_calls():
    """The literal failure mode: a real socket transport (what the TastyTrade SDK's cached
    session/DXLink websocket ultimately is) is bound to the event loop that was running when
    it was opened. A fresh loop per _run_sync call (asyncio.run() each time) closes that loop
    the moment the call returns, so reusing the transport from a LATER call breaks -- exactly
    as observed live ("RuntimeError: Event loop is closed" on every warm-up batch after the
    first). One persistent loop across calls keeps it usable."""
    import asyncio

    shared: dict = {}

    async def handle(r, w):
        while True:
            data = await r.read(100)
            if not data:
                break
            w.write(data)
            await w.drain()

    async def first():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        shared["server"], shared["reader"], shared["writer"] = server, reader, writer
        writer.write(b"hello")
        await writer.drain()
        return (await reader.read(100)).decode()

    async def second():
        shared["writer"].write(b"again")
        await shared["writer"].drain()
        return (await shared["reader"].read(100)).decode()

    try:
        assert _run_sync(first()) == "hello"
        assert _run_sync(second()) == "again"
    finally:
        try:
            shared["server"].close()
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never mask the assertion above
            pass


def test_run_sync_returns_the_coroutines_result():
    async def coro():
        return 42

    assert _run_sync(coro()) == 42


def test_run_sync_propagates_the_coroutines_exception():
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        _run_sync(boom())


def test_run_sync_still_works_when_called_from_inside_a_running_loop():
    """The rare nested case (sync code called from within already-async code) must keep
    working via the one-off thread-pool fallback -- only the no-running-loop path moves to
    the persistent background loop."""
    import asyncio

    async def inner():
        return "inner-ok"

    async def outer():
        return _run_sync(inner())

    assert asyncio.run(outer()) == "inner-ok"
