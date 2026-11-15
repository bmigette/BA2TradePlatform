"""Certification of the LIVE iv-v1 reader (Alpha Vantage) against the backtest store.

Same function, same inputs -> identical values, with NO tolerance: the store build and the live
reader both map the vendor rows with ``alphavantage.map_chain``, filter them with
``option_chain_source.chain_quotes_from_frame``, read the same OHLCV window and call
``market_conditions.compute_iv_v1``. The AV store here is written by the REAL downloader code
(``AvOptionStore.record_day`` + ``compact``) from the same raw payload rows the live fetch returns.

Also pinned: every live failure is an unknown with its reason (no key, throttled, HTTP error -- key
redacted, chain not published, no budget slot) and is NOT cached; a fetched chain is cached per
(symbol, session) on disk; capture records the evidence and replay recomputes the identical row,
refusing tampered evidence.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from ba2_common.core import implied_vol as ivm
from ba2_common.core.market_conditions import (
    FIELD_ATM_IV_30, FIELD_IV_RV_RATIO, IV_PROFILE, STATUS_NO_CONTEXT, STATUS_VALID,
)
from ba2_providers.market_conditions import warmup as W
from ba2_providers.market_conditions.av_live_chain import AlphaVantageLiveChainReader
from ba2_providers.options.alphavantage import (
    AlphaVantageOptionsError, AlphaVantageThrottled, AvOptionStore, map_chain,
)

from .test_market_condition_warmup import CERT, TRUTH, FakeSource, _sessions, _write

START, END = date(2025, 3, 3), date(2025, 4, 30)
KEY = "TESTKEY-ABC123"


def _fridays(a, b):
    d = a + timedelta(days=(4 - a.weekday()) % 7)
    while d <= b:
        yield d
        d += timedelta(days=7)


def _av_rows(symbol: str, session: date):
    """Raw AV HISTORICAL_OPTIONS rows (strings, AV field names) priced by Black-76, with a vendor
    implied_volatility column of garbage that must never be read."""
    F = 100.0 + 0.05 * (session - START).days
    rows = []
    for e in _fridays(session + timedelta(days=5), session + timedelta(days=140)):
        dte = (e - session).days
        for K in [80 + 2.5 * i for i in range(18)]:
            for cp in ("C", "P"):
                p = ivm.black76_price(F, K, dte / 365.0, 0.22 + 0.0008 * dte, cp == "C")
                rows.append({"contractID": f"{symbol}{e:%y%m%d}{cp}{int(round(K * 1000)):08d}",
                             "symbol": symbol, "expiration": e.isoformat(), "strike": f"{K:.2f}",
                             "type": "call" if cp == "C" else "put", "date": session.isoformat(),
                             "last": f"{p:.2f}", "mark": f"{p:.2f}", "bid": f"{max(p - 0.02, 0):.2f}",
                             "ask": f"{p + 0.02:.2f}", "volume": "10", "open_interest": "100",
                             "implied_volatility": "9.99", "delta": "0.5"})
    return rows


class _Grant:
    def __init__(self, ok=True):
        self.ok, self.n = ok, 0

    def acquire(self):
        self.n += 1
        return self.ok


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "CLAIM_POLL_S", 0.01)
    root = tmp_path / "cache"
    for sym, df in TRUTH.items():
        _write(root, sym, df)
    for sym, df in CERT.items():
        _write(root, sym, df)
    store = AvOptionStore(str(root / "AlphaVantageOptionsProvider"))
    for s in _sessions(START - timedelta(days=7), END):
        frame, stats = map_chain("AAA", s, _av_rows("AAA", s))
        store.record_day("AAA", s, frame, stats)
    store.compact("AAA")
    plan = W.plan(IV_PROFILE, ["AAA"], START, END, cache_root=str(root), source=FakeSource(str(root)),
                  option_source="alphavantage")
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(plan.to_json())), fetch_missing=False,
                  concurrency=1, source=None)
    assert rep.ok, rep
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader
    mapped = MappedMarketConditionReader(str(root), rep.manifest_digest, IV_PROFILE)
    return root, mapped


def _live_reader(root, *, fetch=None, key=KEY, budget=None):
    """The live reader over the fixture cache: bars from the SAME daily cache the store build read,
    chains from the fake fetch (cached under <root>/market_conditions_live/alphavantage)."""
    calls = []

    def default_fetch(symbol, session, api_key, timeout=None):
        calls.append((symbol, session, api_key))
        return _av_rows(symbol, session)

    reader = AlphaVantageLiveChainReader(IV_PROFILE, cache_root=str(root), api_key=lambda: key,
                                         fetch=fetch or default_fetch, budget=budget or _Grant())
    return reader, calls


SESSIONS = (date(2025, 3, 3), date(2025, 3, 20), date(2025, 4, 8), date(2025, 4, 30))


def test_the_live_rows_equal_the_store_rows_exactly(world):
    root, mapped = world
    live, calls = _live_reader(root)
    for s in SESSIONS:
        want = mapped.observe("AAA", s)
        got = live.observe("AAA", s)
        assert dict(got.by_field()) == dict(want.by_field()), s
        assert got.by_field()[FIELD_ATM_IV_30].status == STATUS_VALID
    assert len(calls) == len(SESSIONS)


def test_a_fetched_chain_is_cached_per_symbol_session(world):
    root, _mapped = world
    live, calls = _live_reader(root)
    live.observe("AAA", SESSIONS[0])
    live.observe("AAA", SESSIONS[0])
    assert len(calls) == 1
    again, calls2 = _live_reader(root)                  # a new reader (restart): the disk cache serves
    assert dict(again.observe("AAA", SESSIONS[0]).by_field()) == dict(live.observe("AAA", SESSIONS[0]).by_field())
    assert calls2 == []


@pytest.mark.parametrize("case,expect", [
    ("no_key", "alpha_vantage_api_key is not set"),
    ("throttled", "rate limit (Alpha Vantage)"),
    ("http", "Alpha Vantage request failed"),
    ("empty", "serves no chain"),
    ("budget", "chain not prefetched; rate budget exhausted"),
])
def test_every_failure_is_an_unknown_with_its_reason_and_is_not_cached(world, case, expect, caplog):
    root, mapped = world
    state = {"fail": True}

    def fetch(symbol, session, api_key, timeout=None):
        if state["fail"]:
            if case == "throttled":
                raise AlphaVantageThrottled("Thank you for using Alpha Vantage! ... premium ... per minute")
            if case == "http":
                raise AlphaVantageOptionsError(f"HTTPError: 503 for url ...?apikey={api_key}")
            if case == "empty":
                return []
        return _av_rows(symbol, session)

    live, _ = _live_reader(root, fetch=fetch, key=None if case == "no_key" else KEY,
                           budget=_Grant(ok=case != "budget"))
    row = live.observe("AAA", SESSIONS[1]).by_field()
    assert {o.status for o in row.values()} == {STATUS_NO_CONTEXT}
    assert all(expect in o.reason for o in row.values()), row[FIELD_IV_RV_RATIO].reason
    assert KEY not in caplog.text and all(KEY not in o.reason for o in row.values())
    assert live.refused == 1
    # repaired: the next read is served (a failure is never memoised or cached)
    state["fail"] = False
    live._api_key = lambda: KEY
    live._budget = _Grant()
    assert dict(live.observe("AAA", SESSIONS[1]).by_field()) == dict(mapped.observe("AAA", SESSIONS[1]).by_field())


def test_the_resolver_serves_alphavantage_and_refuses_other_sources(world):
    from ba2_common.core import market_condition_live as live
    from ba2_common.core.market_condition_iv_live import clear_live_chain_readers, register_live_chain_reader
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
    from ba2_providers.market_conditions.av_live_chain import make_reader

    root, _ = world
    clear_live_chain_readers()
    try:
        r = live.resolver_for_profiles((IV_PROFILE,), option_source="alphavantage", cache_root=str(root))
        assert isinstance(r.reader, LiveUnavailableMarketConditionReader)     # not wired in this process
        assert "no live reader is wired" in r.reader.reason
        register_live_chain_reader("alphavantage", make_reader)
        r = live.resolver_for_profiles((IV_PROFILE,), option_source="alphavantage", cache_root=str(root))
        assert isinstance(r.reader, AlphaVantageLiveChainReader)
        assert r.live_unavailable_profiles() == () and r.research_mode_profiles() == ()
        for src in ("thetadata", None):
            r = live.resolver_for_profiles((IV_PROFILE,), option_source=src, cache_root=str(root))
            assert isinstance(r.reader, LiveUnavailableMarketConditionReader)
    finally:
        clear_live_chain_readers()


class _Capture:
    def __init__(self):
        self.observations = []

    def record_observation(self, **kw):
        self.observations.append(kw)

    def note_failure(self, what, exc):
        raise AssertionError(f"{what}: {exc}")


def test_capture_and_replay_recompute_the_identical_row(world):
    from ba2_common.core.market_condition_iv_live import ReplayChainReader
    from ba2_common.core.market_condition_readers import capturing_reader_for
    from ba2_common.core.replay.context import ReplayMiss

    root, _ = world
    live, _ = _live_reader(root)
    cap = _Capture()
    reader, record = capturing_reader_for(live, cap, source_profile="fmp-daily-split-adjusted-v1",
                                          timing_policy="prior_session_v1")
    got = {s: reader.observe("AAA", s) for s in SESSIONS}
    record("AAA", SESSIONS[0])                                   # a repeat read records nothing new
    payloads = [o["payload"] for o in cap.observations]
    assert len(payloads) == len(SESSIONS)
    assert all(p["chain_b64"] and p["window_f8_b64"] and p["option_source"] == "alphavantage" for p in payloads)
    replay = ReplayChainReader.from_payloads(json.loads(json.dumps(payloads)), profile=IV_PROFILE,
                                             option_source="alphavantage")
    for s in SESSIONS:
        assert dict(replay.observe("AAA", s).by_field()) == dict(got[s].by_field())
    with pytest.raises(ReplayMiss):
        replay.observe("AAA", date(2025, 3, 4))
    tampered = [dict(p) for p in payloads]
    raw = bytearray(__import__("base64").b64decode(tampered[0]["chain_b64"]))
    raw[-3] ^= 0x01
    tampered[0]["chain_b64"] = __import__("base64").b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(ReplayMiss) as miss:
        ReplayChainReader.from_payloads(tampered, profile=IV_PROFILE, option_source="alphavantage") \
            .observe("AAA", SESSIONS[0])
    assert miss.value.kind == "market_condition_chain_digest"


def test_a_recorded_refusal_replays_as_the_same_refusal(world):
    from ba2_common.core.market_condition_iv_live import ReplayChainReader
    from ba2_common.core.market_condition_readers import capturing_reader_for

    root, _ = world
    live, _ = _live_reader(root, key=None)
    cap = _Capture()
    reader, _rec = capturing_reader_for(live, cap, source_profile="x", timing_policy="y")
    refused = reader.observe("AAA", SESSIONS[0])
    replay = ReplayChainReader.from_payloads([o["payload"] for o in cap.observations], profile=IV_PROFILE,
                                             option_source="alphavantage")
    assert dict(replay.observe("AAA", SESSIONS[0]).by_field()) == dict(refused.by_field())
