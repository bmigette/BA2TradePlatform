"""The resumable options warm-up (``tools/warm_options_history.py``).

This script is meant to be left running for HOURS and interrupted at will — Ctrl-C, a
dropped socket, a laptop lid. Everything asserted here is about that: what survives an
interrupt, what a re-run does NOT re-fetch, and the fact that "genuinely empty" is recorded
as a fact rather than retried forever.

No network: the provider is a fake that returns canned batches, and ``sleep`` and the clock
are injected so a multi-hour run's pacing and ETA can be asserted in milliseconds. Time is
frozen to a fixed instant, never ``today``.
"""
import importlib
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Set

import pytest

from ba2_common.core.interfaces import OptionContractMeta, OptionEodBar
from ba2_providers.options.parquet_store import OptionHistoryParquetStore, PartitionState
from ba2_providers.options.tastytrade import CandleBatch, StreamInterrupted, occ_symbol

warm = importlib.import_module("tools.warm_options_history")

FROZEN = datetime(2026, 3, 4, 9, 0, 0, tzinfo=timezone.utc)
START, END = date(2023, 1, 1), date(2026, 3, 1)


class FakeClock:
    """A monotonic clock that only advances when the code under test 'sleeps'."""

    def __init__(self, t0=FROZEN):
        self.now = t0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


@dataclass
class FakeProvider:
    """Canned discovery + canned candle batches, keyed by expiry."""
    contracts: Dict[str, List[OptionContractMeta]] = field(default_factory=dict)
    batches: Dict[date, CandleBatch] = field(default_factory=dict)
    fetched: List[date] = field(default_factory=list)
    discovered: List[str] = field(default_factory=list)
    raise_on: Set[date] = field(default_factory=set)
    #: expiry -> the exact exception to raise, for testing transient-vs-permanent handling.
    #: raise_on always raises StreamInterrupted("socket dropped"), which stays a transient
    #: case; this lets a test inject something that must NOT be retried.
    raise_exc: Dict[date, Exception] = field(default_factory=dict)
    name = "tastytrade"

    def history_floor(self):
        return date(2022, 10, 1)

    def discover_contracts(self, underlying, *, expiry_gte, expiry_lte, **kw):
        self.discovered.append(underlying)
        return [c for c in self.contracts.get(underlying, [])
                if expiry_gte <= c.expiry <= expiry_lte]

    def fetch_bars_detailed(self, contracts, *, start, end):
        contracts = list(contracts)
        exp = contracts[0].expiry
        self.fetched.append(exp)
        if exp in self.raise_exc:
            raise self.raise_exc[exp]
        if exp in self.raise_on:
            raise StreamInterrupted("socket dropped")
        canned = self.batches.get(exp)
        if canned is None:
            return CandleBatch(bars=[], empty={c.occ_symbol for c in contracts})
        # Only return the parts still being asked for (so a retry of the unresolved
        # subset behaves like the real provider).
        wanted = {c.occ_symbol for c in contracts}
        return CandleBatch(
            bars=[b for b in canned.bars if b.occ_symbol in wanted],
            empty={s for s in canned.empty if s in wanted},
            unresolved={s for s in canned.unresolved if s in wanted},
            interrupted=canned.interrupted)


def _contract(underlying, expiry, strike, right="C"):
    return OptionContractMeta(occ_symbol(underlying, expiry, right, strike),
                              underlying, "call" if right == "C" else "put", strike, expiry)


def _bar(occ, d, *, iv=0.2841, oi=12345):
    return OptionEodBar(occ_symbol=occ, bar_date=d, open=7.0, high=7.5, low=6.8,
                        close=7.25, volume=911, open_interest=oi, iv=iv)


EXPIRIES = [date(2023, 1, 20), date(2023, 1, 27), date(2023, 2, 3)]


@pytest.fixture
def store(tmp_path):
    return OptionHistoryParquetStore(root=str(tmp_path / "opt"), clock=lambda: FROZEN)


@pytest.fixture
def provider():
    p = FakeProvider()
    p.contracts["AAPL"] = [_contract("AAPL", e, s) for e in EXPIRIES for s in (150.0, 155.0)]
    for e in EXPIRIES:
        p.batches[e] = CandleBatch(
            bars=[_bar(occ_symbol("AAPL", e, "C", 150.0), date(2023, 1, 3))],
            empty={occ_symbol("AAPL", e, "C", 155.0)})
    return p


def _run(provider, store, extra=(), clock=None, out=None):
    clock = clock or FakeClock()
    lines: List[str] = out if out is not None else []
    # These tests drive the FAKE provider's discover_contracts, so they must ask for the
    # provider-backed listing explicitly. The shipped DEFAULT is 'synthetic', because
    # /instruments/equity-options answers 403 for a personal OAuth app and no available
    # scope can change that -- see test_the_default_discovery_needs_no_listing_endpoint.
    # ``extra`` is appended last, so an individual test can still override this.
    argv = ["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
            "--rate-limit", "0", "--discovery", "rest", "--discovery", "rest", *extra]
    rc = warm.main(argv, provider=provider, store=store, clock=clock,
                   sleep=lambda s: clock.advance(s), log=lines.append)
    return rc, lines


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_writes_absolutely_nothing(provider, store, tmp_path):
    rc, lines = _run(provider, store, ["--dry-run"])
    assert rc == 0
    assert provider.fetched == [], "a dry run must not download a single bar"
    assert not os.path.exists(store.root) or _files_under(store.root) == [], \
        "a dry run must not create a file"


def test_dry_run_prints_the_destination_the_window_and_the_unit_count(provider, store):
    _, lines = _run(provider, store, ["--dry-run"])
    text = "\n".join(lines)
    assert "DRY RUN" in text
    # The destination must be stated OUTRIGHT, not merely implied by a sample file path:
    # "where is this going to write 40 GB" is the first question a dry run has to answer.
    assert any(ln.startswith("store root") and store.root in ln for ln in lines), lines
    assert "2023-01-01" in text and "2026-03-01" in text
    assert "AAPL" in text
    assert "3 units to fetch" in text, text


def test_dry_run_reports_what_is_already_done_separately_from_what_remains(provider, store):
    store.write_partition("AAPL", EXPIRIES[0], [_bar("AAPL230120C00150000", date(2023, 1, 3))],
                          START, END, empty_contracts=[])
    _, lines = _run(provider, store, ["--dry-run"])
    plan = warm.last_plan()
    assert plan.units_pending == 2
    assert plan.units_done == 1
    text = "\n".join(lines)
    assert "2 units to fetch" in text, text
    assert "1 already complete" in text, text


def test_dry_run_names_the_exact_file_it_would_write(provider, store):
    _, lines = _run(provider, store, ["--dry-run"])
    text = "\n".join(lines)
    assert "_manifest.json" in text
    assert "exp=" in text and ".parquet" in text


def test_dry_run_gives_a_wall_clock_estimate(provider, store):
    _, lines = _run(provider, store, ["--dry-run"])
    assert any("estimate" in ln.lower() for ln in lines), lines


# --------------------------------------------------------------------------- #
# the happy path — and the payload
# --------------------------------------------------------------------------- #
def test_a_real_run_writes_one_partition_per_expiry(provider, store):
    rc, _ = _run(provider, store)
    assert rc == 0
    for e in EXPIRIES:
        assert store.partition_state("AAPL", e, START, END) is PartitionState.COMPLETE


def test_iv_and_open_interest_survive_the_whole_pipeline(provider, store):
    """End-to-end: candle batch -> partition -> parquet -> read back. These two fields are
    the entire reason for the pipeline."""
    _run(provider, store)
    df = store.read_underlying("AAPL")
    assert len(df) == 3
    assert [round(float(v), 4) for v in df["iv"]] == [0.2841] * 3
    assert [int(v) for v in df["open_interest"]] == [12345] * 3


def test_contracts_with_no_bars_are_recorded_as_empty_in_the_manifest(provider, store):
    _run(provider, store)
    m = store.read_manifest("AAPL", EXPIRIES[0])
    assert m["empty_contracts"] == [occ_symbol("AAPL", EXPIRIES[0], "C", 155.0)]


def test_an_expiry_whose_whole_chain_is_empty_is_EMPTY_not_missing(provider, store):
    """A dead expiry must be recorded, or every run re-requests it until the end of time."""
    dead = EXPIRIES[1]
    provider.batches[dead] = CandleBatch(
        bars=[], empty={c.occ_symbol for c in provider.contracts["AAPL"] if c.expiry == dead})
    _run(provider, store)
    assert store.partition_state("AAPL", dead, START, END) is PartitionState.EMPTY
    assert store.partition_state("AAPL", dead, START, END) is not PartitionState.MISSING


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #
def test_completed_partitions_are_not_refetched_on_a_restart(provider, store):
    _run(provider, store)
    assert sorted(provider.fetched) == EXPIRIES

    provider.fetched.clear()
    provider.discovered.clear()
    rc, _ = _run(provider, store)
    assert rc == 0
    assert provider.fetched == [], "a completed store must fetch nothing at all"


def test_a_partial_store_refetches_only_the_missing_partitions(provider, store):
    store.write_partition("AAPL", EXPIRIES[0], [_bar("AAPL230120C00150000", date(2023, 1, 3))],
                          START, END, empty_contracts=[])
    _run(provider, store)
    assert provider.fetched == [EXPIRIES[1], EXPIRIES[2]]


def test_an_EMPTY_partition_is_never_retried(provider, store):
    dead = EXPIRIES[1]
    provider.batches[dead] = CandleBatch(
        bars=[], empty={c.occ_symbol for c in provider.contracts["AAPL"] if c.expiry == dead})
    _run(provider, store)
    provider.fetched.clear()
    _run(provider, store)
    assert dead not in provider.fetched


def test_a_ctrl_c_mid_run_leaves_finished_partitions_intact_and_resumes(provider, store):
    """Interrupting must lose at most the CURRENT unit of work."""
    boom = EXPIRIES[1]

    real_fetch = provider.fetch_bars_detailed

    def fetch(contracts, *, start, end):
        contracts = list(contracts)
        if contracts[0].expiry == boom:
            raise KeyboardInterrupt("user hit ctrl-c")
        return real_fetch(contracts, start=start, end=end)

    provider.fetch_bars_detailed = fetch
    with pytest.raises(KeyboardInterrupt):
        _run(provider, store)

    assert store.partition_state("AAPL", EXPIRIES[0], START, END) is PartitionState.COMPLETE
    assert store.partition_state("AAPL", boom, START, END) is PartitionState.MISSING
    assert store.partition_state("AAPL", EXPIRIES[2], START, END) is PartitionState.MISSING

    provider.fetch_bars_detailed = real_fetch
    provider.fetched.clear()
    _run(provider, store)
    assert provider.fetched == [boom, EXPIRIES[2]], "resumes exactly where it stopped"


def test_widening_the_window_refetches_rather_than_serving_the_narrower_cache(provider, store):
    _run(provider, store)
    provider.fetched.clear()
    clock = FakeClock()
    warm.main(["--symbols", "AAPL", "--start", "2022-11-01", "--end", END.isoformat(),
               "--rate-limit", "0", "--discovery", "rest"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lambda _l: None)
    assert sorted(provider.fetched) == EXPIRIES, \
        "a wider window must not be served from a narrower fetch"


# --------------------------------------------------------------------------- #
# dropped socket mid-symbol
# --------------------------------------------------------------------------- #
def test_an_unresolved_contract_blocks_the_manifest_so_the_unit_is_redone(provider, store):
    """UNRESOLVED means "the socket died before this contract answered". Writing a manifest
    over that would bake a permanent hole into the cache."""
    bad = EXPIRIES[1]
    provider.batches[bad] = CandleBatch(
        bars=[_bar(occ_symbol("AAPL", bad, "C", 150.0), date(2023, 1, 3))],
        unresolved={occ_symbol("AAPL", bad, "C", 155.0)}, interrupted=True)
    rc, lines = _run(provider, store, ["--max-retries", "2"])

    assert store.partition_state("AAPL", bad, START, END) is PartitionState.MISSING
    assert store.partition_state("AAPL", EXPIRIES[0], START, END) is PartitionState.COMPLETE
    assert store.partition_state("AAPL", EXPIRIES[2], START, END) is PartitionState.COMPLETE
    assert rc != 0, "a unit that never completed must be reported as a failure"


def test_a_retry_re_requests_only_the_contracts_that_did_not_answer(provider, store):
    """The bars already received are kept; re-subscribing the whole chain would throw away
    good work every time a socket blips."""
    bad = EXPIRIES[1]
    asked: List[List[str]] = []
    good = occ_symbol("AAPL", bad, "C", 150.0)
    missing = occ_symbol("AAPL", bad, "C", 155.0)

    def fetch(contracts, *, start, end):
        contracts = list(contracts)
        asked.append(sorted(c.occ_symbol for c in contracts))
        if contracts[0].expiry != bad:
            return CandleBatch(bars=[], empty={c.occ_symbol for c in contracts})
        if len(asked) == 2:  # first attempt at the bad expiry
            return CandleBatch(bars=[_bar(good, date(2023, 1, 3))],
                               unresolved={missing}, interrupted=True)
        return CandleBatch(bars=[_bar(missing, date(2023, 1, 4))])

    provider.fetch_bars_detailed = fetch
    rc, _ = _run(provider, store, ["--max-retries", "3"])
    assert rc == 0
    assert asked[2] == [missing], f"retry must ask only for the unresolved contract: {asked}"
    df = store.read_partition("AAPL", bad)
    assert sorted(df["occ_symbol"]) == sorted([good, missing]), \
        "the bars from the first attempt are kept and merged with the retry"


def test_empties_found_before_a_drop_are_still_recorded_after_the_retry(provider, store):
    """Attempt 1 proves three strikes are dead and then the socket dies. If those facts are
    thrown away with the failed attempt, every future run re-requests them — the exact
    "retried forever" failure the empty state exists to prevent."""
    bad = EXPIRIES[1]
    dead_early = occ_symbol("AAPL", bad, "C", 155.0)
    late = occ_symbol("AAPL", bad, "C", 150.0)
    calls = []

    def fetch(contracts, *, start, end):
        contracts = list(contracts)
        calls.append(sorted(c.occ_symbol for c in contracts))
        if contracts[0].expiry != bad:
            return CandleBatch(bars=[], empty={c.occ_symbol for c in contracts})
        if len(calls) == 2:
            return CandleBatch(bars=[], empty={dead_early}, unresolved={late},
                               interrupted=True)
        return CandleBatch(bars=[], empty={late})

    provider.fetch_bars_detailed = fetch
    rc, _ = _run(provider, store, ["--max-retries", "3"])
    assert rc == 0
    m = store.read_manifest("AAPL", bad)
    assert m["empty_contracts"] == sorted([dead_early, late]), m["empty_contracts"]


def test_an_exception_from_the_stream_is_retried_with_exponential_backoff(provider, store):
    slept: List[float] = []
    clock = FakeClock()
    provider.raise_on = {EXPIRIES[1]}
    warm.main(["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
               "--rate-limit", "0", "--discovery", "rest", "--max-retries", "4", "--backoff", "2"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: (slept.append(s), clock.advance(s))[0], log=lambda _l: None)
    backoffs = [s for s in slept if s > 0]
    assert backoffs == [2.0, 4.0, 8.0], f"expected doubling backoff, got {backoffs}"


def test_a_429_is_recognised_as_transient_and_retried_with_backoff(provider, store):
    """Mirrors fetch_options.py's _is_transient, which explicitly names "429"/"TooManyRequests"
    -- a rate-limit response must back off and retry, not be treated as a dead end. raise_exc
    never self-clears (matching raise_on's existing pattern), so this asserts the retry/backoff
    SEQUENCE happens, the same shape as test_an_exception_from_the_stream_is_retried_with_..."""
    slept: List[float] = []
    clock = FakeClock()
    provider.raise_exc = {EXPIRIES[1]: RuntimeError("429 Too Many Requests")}
    warm.main(["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
              "--rate-limit", "0", "--discovery", "rest", "--max-retries", "4", "--backoff", "2"],
             provider=provider, store=store, clock=clock,
             sleep=lambda s: (slept.append(s), clock.advance(s))[0], log=lambda _l: None)
    backoffs = [s for s in slept if s > 0]
    assert backoffs == [2.0, 4.0, 8.0], f"a 429 must retry with the same doubling backoff, got {backoffs}"


def test_a_permanent_error_is_not_retried_and_the_unit_fails_immediately(provider, store):
    """THE GAP THIS CLOSES. Every exception used to be treated as transient (a blanket
    'except Exception: retry'), so an auth/scope failure -- e.g. TastyTrade's own
    "Token has insufficient scopes for this request", the exact 403 this cache hit on
    2026-08-25 -- burned through every retry's backoff on EVERY unit before giving up, instead
    of failing that unit at once. Isolation is preserved: it is still just THIS unit that
    fails, not the whole run (matches test_a_failing_unit_does_not_abort_the_rest_of_the_run)."""
    bad = EXPIRIES[1]
    slept: List[float] = []
    clock = FakeClock()
    provider.raise_exc = {bad: RuntimeError("403: Token has insufficient scopes for this request")}
    warm.main(["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
              "--rate-limit", "0", "--discovery", "rest", "--max-retries", "4", "--backoff", "2"],
             provider=provider, store=store, clock=clock,
             sleep=lambda s: (slept.append(s), clock.advance(s))[0], log=lambda _l: None)
    assert not any(s > 0 for s in slept), "a permanent error must not sleep/back off at all"
    assert store.partition_state("AAPL", bad, START, END) is PartitionState.MISSING
    assert store.partition_state("AAPL", EXPIRIES[0], START, END) is PartitionState.COMPLETE
    assert store.partition_state("AAPL", EXPIRIES[2], START, END) is PartitionState.COMPLETE


def test_a_permanent_error_is_logged_as_non_transient(provider, store):
    bad = EXPIRIES[1]
    provider.raise_exc = {bad: RuntimeError("401 Unauthorized")}
    lines: List[str] = []
    warm.main(["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
              "--rate-limit", "0", "--discovery", "rest", "--max-retries", "4"],
             provider=provider, store=store, clock=FakeClock(),
             sleep=lambda s: None, log=lines.append)
    attempt_lines = [ln for ln in lines if "] attempt" in ln]
    assert len(attempt_lines) == 1, \
        f"a permanent error must be tried exactly once, not retried: {attempt_lines}"
    assert "permanent" in attempt_lines[0].lower(), \
        f"the single attempt must say WHY it is not retrying: {attempt_lines[0]!r}"


def test_a_failing_unit_does_not_abort_the_rest_of_the_run(provider, store):
    provider.raise_on = {EXPIRIES[0]}
    rc, _ = _run(provider, store, ["--max-retries", "1"])
    assert rc != 0
    assert store.partition_state("AAPL", EXPIRIES[1], START, END) is PartitionState.COMPLETE
    assert store.partition_state("AAPL", EXPIRIES[2], START, END) is PartitionState.COMPLETE


# --------------------------------------------------------------------------- #
# pacing / progress
# --------------------------------------------------------------------------- #
def test_it_rate_limits_between_units(provider, store):
    slept: List[float] = []
    clock = FakeClock()
    warm.main(["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
               "--rate-limit", "1.5", "--discovery", "rest"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: (slept.append(s), clock.advance(s))[0], log=lambda _l: None)
    assert slept.count(1.5) == 3, f"one pause per unit, got {slept}"


def test_progress_lines_carry_done_remaining_and_an_eta(provider, store):
    clock = FakeClock()
    real_fetch = provider.fetch_bars_detailed

    def slow(contracts, *, start, end):
        clock.advance(60)
        return real_fetch(contracts, start=start, end=end)

    provider.fetch_bars_detailed = slow
    lines: List[str] = []
    warm.main(["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
               "--rate-limit", "0", "--discovery", "rest", "--progress-every", "1"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lines.append)
    progress = [ln for ln in lines if "ETA" in ln]
    assert len(progress) >= 3, lines
    assert "1/3" in progress[0], progress[0]
    assert "3/3" in progress[-1], progress[-1]


def test_the_final_summary_reports_rows_partitions_and_bytes(provider, store):
    _, lines = _run(provider, store)
    text = "\n".join(lines)
    assert "3 rows" in text or "rows=3" in text, text
    assert "byte" in text.lower() or "MB" in text or "KB" in text, text


def test_the_summary_counts_empty_partitions_apart_from_written_ones(provider, store):
    """"3 partitions written" when one of them holds nothing is how a run looks successful
    while the cache quietly has a hole in it."""
    dead = EXPIRIES[1]
    provider.batches[dead] = CandleBatch(
        bars=[], empty={c.occ_symbol for c in provider.contracts["AAPL"] if c.expiry == dead})
    _, lines = _run(provider, store)
    text = "\n".join(lines)
    assert "2 partitions written" in text, text
    assert "1 empty" in text, text


def test_units_are_processed_in_ascending_expiry_order(provider, store):
    """A resumable run needs a DETERMINISTIC order: interrupt, restart, and the same units
    come next. Iterating a dict built from whatever order discovery happened to return
    makes "resume" mean something different on every run."""
    shuffled = list(reversed(EXPIRIES)) + [date(2023, 1, 24)]
    provider.contracts["AAPL"] = [_contract("AAPL", e, 150.0) for e in shuffled]
    for e in shuffled:
        provider.batches.setdefault(e, CandleBatch(
            bars=[_bar(occ_symbol("AAPL", e, "C", 150.0), date(2023, 1, 3))]))
    _run(provider, store)
    assert provider.fetched == sorted(shuffled), provider.fetched


# --------------------------------------------------------------------------- #
# --limit / --symbols
# --------------------------------------------------------------------------- #
def test_limit_caps_the_number_of_units_so_one_name_can_be_proven_first(provider, store):
    _run(provider, store, ["--limit", "1"])
    assert len(provider.fetched) == 1


def test_limit_counts_only_units_that_would_actually_be_fetched(provider, store):
    store.write_partition("AAPL", EXPIRIES[0], [_bar("AAPL230120C00150000", date(2023, 1, 3))],
                          START, END, empty_contracts=[])
    _run(provider, store, ["--limit", "1"])
    assert provider.fetched == [EXPIRIES[1]], "the limit must not be spent on skipped work"


def test_symbols_file_is_read_when_no_inline_symbols_are_given(provider, store, tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("AAPL\n# a comment\n\nMSFT\n")
    clock = FakeClock()
    warm.main(["--symbols-file", str(f), "--start", START.isoformat(),
               "--end", END.isoformat(), "--rate-limit", "0", "--discovery", "rest", "--dry-run"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lambda _l: None)
    assert provider.discovered == ["AAPL", "MSFT"]


def test_symbols_are_upper_cased_and_deduplicated(provider, store):
    clock = FakeClock()
    warm.main(["--symbols", "aapl,AAPL, msft ", "--start", START.isoformat(),
               "--end", END.isoformat(), "--dry-run", "--discovery", "rest"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lambda _l: None)
    assert provider.discovered == ["AAPL", "MSFT"]


def test_no_symbols_at_all_is_an_error_not_a_silent_no_op(provider, store):
    with pytest.raises(SystemExit):
        warm.main(["--start", START.isoformat(), "--end", END.isoformat()],
                  provider=provider, store=store, clock=FakeClock(),
                  sleep=lambda s: None, log=lambda _l: None)


# --------------------------------------------------------------------------- #
# guardrails
# --------------------------------------------------------------------------- #
def test_a_start_before_the_providers_history_floor_is_refused(provider, store):
    """Silently accepting it would build a cache with empty leading months, which surfaces
    much later as an expert that never trades."""
    with pytest.raises(SystemExit):
        warm.main(["--symbols", "AAPL", "--start", "2019-01-01", "--end", END.isoformat()],
                  provider=provider, store=store, clock=FakeClock(),
                  sleep=lambda s: None, log=lambda _l: None)


def test_a_reversed_window_is_refused(provider, store):
    with pytest.raises(SystemExit):
        warm.main(["--symbols", "AAPL", "--start", "2026-01-01", "--end", "2023-01-01"],
                  provider=provider, store=store, clock=FakeClock(),
                  sleep=lambda s: None, log=lambda _l: None)


def test_the_default_store_root_is_never_the_incumbent_sqlite_cache():
    """The existing ~10 GB options cache must never be touched or migrated."""
    from ba2_common.config import OPTIONS_CACHE_DB
    root = OptionHistoryParquetStore().root
    assert not OPTIONS_CACHE_DB.startswith(root + os.sep)
    assert os.path.dirname(OPTIONS_CACHE_DB) != root


def test_the_default_window_start_is_the_documented_2023_01_01(provider, store):
    ns = warm.parse_args(["--symbols", "AAPL"])
    assert ns.start == "2023-01-01"


# --------------------------------------------------------------------------- #
# discovery caching — a re-run must not re-enumerate 3.5 years of chain
# --------------------------------------------------------------------------- #
def test_discovery_is_cached_so_a_second_run_does_not_relist_the_chain(provider, store):
    _run(provider, store)
    assert provider.discovered == ["AAPL"]
    provider.discovered.clear()
    _run(provider, store)
    assert provider.discovered == [], "the contract list must be served from disk"


def test_a_dry_run_does_not_persist_the_discovery_cache(provider, store):
    _run(provider, store, ["--dry-run"])
    provider.discovered.clear()
    _run(provider, store, ["--dry-run"])
    assert provider.discovered == ["AAPL"], "a dry run must not write even the contract cache"


def test_the_discovery_cache_is_invalidated_by_a_wider_window(provider, store):
    _run(provider, store)
    provider.discovered.clear()
    clock = FakeClock()
    warm.main(["--symbols", "AAPL", "--start", "2022-11-01", "--end", END.isoformat(),
               "--rate-limit", "0", "--discovery", "rest", "--dry-run"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lambda _l: None)
    assert provider.discovered == ["AAPL"]


# --------------------------------------------------------------------------- #
def _files_under(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        out.extend(os.path.join(dirpath, f) for f in files)
    return out


# --------------------------------------------------------------------------- #
# discovery mode — the shipped default must work with a personal OAuth app
# --------------------------------------------------------------------------- #
def test_the_default_discovery_needs_no_listing_endpoint():
    """/instruments/equity-options answers 403 'insufficient scopes' for a personal OAuth
    app, with or without with-expired, and TastyTrade offers only read/trade/openid --
    openid being OpenID Connect identity, not data. So the default must be the mode that
    never calls it. The probe that proved dxfeed serves EXPIRED contracts synthesised its
    streamer symbols exactly this way."""
    assert warm.parse_args(["--symbols", "AAPL"]).discovery == "synthetic"


def test_a_403_on_rest_discovery_names_the_mode_that_works(provider, store, monkeypatch):
    """A scope refusal must never read as 'this symbol has no contracts'."""
    def _boom(*a, **k):
        raise RuntimeError("403 Client Error: Token has insufficient scopes for this request")
    monkeypatch.setattr(provider, "discover_contracts", _boom)
    with pytest.raises(SystemExit) as ei:
        _run(provider, store, ["--dry-run"])
    msg = str(ei.value)
    assert "--discovery synthetic" in msg, msg
    assert "403" in msg and "AAPL" in msg, msg


def test_a_bad_symbols_discovery_failure_does_not_abort_the_rest_of_the_run(
        provider, store, monkeypatch):
    """Regression for the 2026-08-25 incident: 5 of 8 parallel warm-up workers died within
    seconds because their symbol chunk happened to contain a hyphenated ticker (BF-B, CIG-C,
    FITB-PA, MKC-V, PBR-A) that occ_symbol/parse_occ can't round-trip -- an unhandled
    ValueError from discover() used to propagate straight out of build_plan's `for symbol in
    symbols:` loop, killing the whole worker process and abandoning every other symbol queued
    behind the bad one, not just the one that actually failed.

    BADCO is queued FIRST (worst case: everything after it in the loop) with a real, working
    AAPL right behind it -- proving the failure costs exactly BADCO, not the batch."""
    real_discover = provider.discover_contracts

    def _flaky(underlying, **kw):
        if underlying == "BADCO":
            raise ValueError("not an OCC option symbol: 'BADCO230120C00150000'")
        return real_discover(underlying, **kw)

    monkeypatch.setattr(provider, "discover_contracts", _flaky)
    clock = FakeClock()
    rc = warm.main(
        ["--symbols", "BADCO,AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
         "--rate-limit", "0", "--discovery", "rest"],
        provider=provider, store=store, clock=clock,
        sleep=lambda s: clock.advance(s), log=lambda _l: None)

    assert rc == 0
    plan = warm.last_plan()
    assert plan.discovery_failed == {"BADCO": "not an OCC option symbol: 'BADCO230120C00150000'"}
    assert "BADCO" not in plan.per_symbol
    assert "AAPL" in plan.per_symbol
    for e in EXPIRIES:
        assert store.partition_state("AAPL", e, START, END) is PartitionState.COMPLETE


# --------------------------------------------------------------------------- #
# --log-file (2026-08-26: worker logs hit 1-1.7 GB each within a day -- almost entirely the
# tastytrade SDK's own DEBUG wire-trace, not this tool's own progress lines)
# --------------------------------------------------------------------------- #
def test_log_file_receives_this_tools_own_progress_lines(provider, store, tmp_path):
    import logging

    log_path = str(tmp_path / "worker_0.log")
    clock = FakeClock()
    rc = warm.main(
        ["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
         "--rate-limit", "0", "--discovery", "rest", "--log-file", log_path],
        provider=provider, store=store, clock=clock, sleep=lambda s: clock.advance(s))
    assert rc == 0
    assert os.path.exists(log_path)
    with open(log_path, encoding="utf-8") as f:
        content = f.read()
    assert "done:" in content  # the final summary line, via the injected log()

    # The handler created for this run must not linger and keep writing into a closed test
    # temp dir on the NEXT test that reuses the same logger name / file path.
    logging.getLogger(f"warm_options_history.{log_path}").handlers.clear()


def test_no_log_file_falls_back_to_the_injected_log_or_print(provider, store):
    """Explicit ``log=`` (what every other test in this file relies on) must still win over
    stdout even when --log-file is not passed -- CLI-only behaviour is opt-in, not forced."""
    rc, lines = _run(provider, store)
    assert rc == 0
    assert any("done:" in l for l in lines)


def test_the_vendor_debug_logger_is_capped_regardless_of_log_file(provider, store):
    """tastytrade/__init__.py sets its OWN logger to DEBUG at import time; streamer.py then logs
    every raw websocket frame at that level. Left alone, a single busy expiry emits thousands of
    records -- the actual source of the multi-GB/day worker logs, not this tool's own (sparse)
    progress/retry lines. This must be capped on every run, not just when --log-file is used."""
    import logging

    logging.getLogger("tastytrade").setLevel(logging.DEBUG)  # simulate the SDK's import-time set
    rc, _lines = _run(provider, store)
    assert rc == 0
    assert logging.getLogger("tastytrade").level == logging.WARNING


# --------------------------------------------------------------------------- #
# --provider — ThetaData alongside TastyTrade (2026-09-02)
# --------------------------------------------------------------------------- #
def test_the_store_root_defaults_to_a_separate_tree_per_provider(monkeypatch, tmp_path):
    """--provider thetadata must never write into the existing TastyTrade tree (or vice
    versa) unless --out is given explicitly -- the whole point of running it alongside the
    existing cache instead of replacing it."""
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))

    def _root_for(provider_flag):
        lines: List[str] = []
        fake = FakeProvider()
        argv = ["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
               "--rate-limit", "0", "--discovery", "rest"]
        if provider_flag:
            argv += ["--provider", provider_flag]
        warm.main(argv, provider=fake, store=None, clock=FakeClock(),
                 sleep=lambda s: None, log=lines.append)
        return next(l for l in lines if l.startswith("store root")).split(":", 1)[1].strip()

    root_tt = _root_for(None)  # default
    root_td = _root_for("thetadata")

    assert os.path.basename(root_tt) == "TastyTradeOptionsProvider"
    assert os.path.basename(root_td) == "ThetaDataOptionsProvider"
    assert root_tt != root_td
    assert os.path.dirname(root_tt) == os.path.dirname(root_td) == str(tmp_path)


def test_load_thetadata_api_key_prefers_the_explicit_arg_over_everything():
    os_environ_backup = os.environ.get("THETADATA_API_KEY")
    os.environ["THETADATA_API_KEY"] = "env-key"
    try:
        assert warm.load_thetadata_api_key(db_path=None, api_key_arg="arg-key") == "arg-key"
    finally:
        if os_environ_backup is None:
            os.environ.pop("THETADATA_API_KEY", None)
        else:
            os.environ["THETADATA_API_KEY"] = os_environ_backup


def test_load_thetadata_api_key_falls_back_to_the_env_var(monkeypatch):
    monkeypatch.setenv("THETADATA_API_KEY", "env-key")
    assert warm.load_thetadata_api_key(db_path=None, api_key_arg=None) == "env-key"


def test_load_thetadata_api_key_reads_the_appsetting_via_db(tmp_path, monkeypatch):
    monkeypatch.delenv("THETADATA_API_KEY", raising=False)
    import sqlite3
    db_path = str(tmp_path / "platform.sqlite")
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE appsetting (key TEXT, value_str TEXT)")
    con.execute("INSERT INTO appsetting (key, value_str) VALUES ('thetadata_api_key', ?)",
               ("db-key",))
    con.commit()
    con.close()

    assert warm.load_thetadata_api_key(db_path=db_path, api_key_arg=None) == "db-key"


def test_load_thetadata_api_key_sees_a_row_still_in_an_open_WAL_writers_journal(
        tmp_path, monkeypatch):
    """Regression, found live 2026-09-02: the read connection used to add ``immutable=1`` to
    ``mode=ro`` ("mode=ro is belt; immutable=1 is braces"). immutable=1 tells SQLite the file
    will NEVER change, which lets it skip re-checking the WAL -- so a row committed by a still-
    OPEN WAL-mode writer (exactly what a LIVE prod app's own long-running DB connection is)
    read as silently ABSENT, even though ``SELECT`` against the same file with a normal
    connection found it immediately. A real prod DB is checkpointed lazily, so this bit for
    real the first time a key was saved and read back within the same session."""
    monkeypatch.delenv("THETADATA_API_KEY", raising=False)
    import sqlite3
    db_path = str(tmp_path / "platform.sqlite")

    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE appsetting (key TEXT, value_str TEXT)")
    writer.execute("INSERT INTO appsetting (key, value_str) VALUES ('thetadata_api_key', ?)",
                   ("wal-only-key",))
    writer.commit()
    try:
        # The writer connection stays OPEN and uncheckpointed -- the row lives only in the
        # -wal sidecar, not yet folded into the main db file. This is the live-prod shape.
        assert warm.load_thetadata_api_key(db_path=db_path, api_key_arg=None) == "wal-only-key"
    finally:
        writer.close()


def test_load_thetadata_api_key_raises_a_named_error_with_nothing_available(monkeypatch):
    monkeypatch.delenv("THETADATA_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="No ThetaData API key"):
        warm.load_thetadata_api_key(db_path=None, api_key_arg=None)


def test_build_provider_dispatches_to_thetadata_on_the_flag(monkeypatch):
    monkeypatch.setenv("THETADATA_API_KEY", "some-key")
    from ba2_providers.options.thetadata import ThetaDataOptionsProvider
    ns = warm.parse_args(["--symbols", "AAPL", "--provider", "thetadata"])
    p = warm.build_provider(ns)
    assert isinstance(p, ThetaDataOptionsProvider)
    assert p.api_key == "some-key"


def test_a_grpc_unavailable_error_is_recognised_as_transient():
    """ThetaData's cloud client raises grpc.RpcError; its repr() carries the status name
    directly. Without this, a rate-limited/temporarily-down ThetaData call would be treated
    as permanent and fail the unit on the first attempt."""
    class _FakeRpcError(Exception):
        def __repr__(self):
            return ("<_MultiThreadedRendezvous of RPC that terminated with:\n\tstatus = "
                   "StatusCode.UNAVAILABLE\n\tdetails = \"upstream connect error\">")
    assert warm._is_transient(_FakeRpcError())


def test_a_grpc_invalid_argument_error_is_NOT_transient():
    """A malformed request (e.g. bad symbol) fails identically on every retry."""
    class _FakeRpcError(Exception):
        def __repr__(self):
            return ("<_MultiThreadedRendezvous of RPC that terminated with:\n\tstatus = "
                   "StatusCode.INVALID_ARGUMENT\n\tdetails = \"bad request\">")
    assert not warm._is_transient(_FakeRpcError())


def test_the_vendor_debug_cap_survives_the_sdks_own_lazy_import(provider, store):
    """Regression for the 2026-08-26 fix-that-didn't-fix-it: a live relaunch with the FIRST
    version of this cap kept emitting DEBUG spam, because tastytrade/__init__.py's
    logger.setLevel(DEBUG) runs the first time ANYTHING imports the package -- and our own
    provider wrapper does that LAZILY, deep inside the actual streaming call (only reached once
    a unit opens a socket), i.e. AFTER a setLevel(WARNING) placed early in main() had already
    run. That later, SDK-owned setLevel(DEBUG) silently overwrote the early cap.

    First prove the mechanism is real (cap-then-import loses); then prove main()'s own ordering
    (import-then-cap) survives a later re-import exactly the way the real streaming call would
    trigger it."""
    import logging
    import sys

    sys.modules.pop("tastytrade", None)  # simulate: never yet imported in this process

    # The FIRST (broken) fix: cap before the SDK's own (lazy, later) import ever runs.
    logging.getLogger("tastytrade").setLevel(logging.WARNING)
    import tastytrade  # noqa: F401 -- exactly what ba2_providers' lazy `from tastytrade import
    # DXLinkStreamer` does the first time a unit actually streams
    assert logging.getLogger("tastytrade").level == logging.DEBUG, (
        "if this fails, the SDK no longer sets DEBUG at import time and the ordering bug this "
        "test guards no longer applies -- the fix in main() could be simplified")

    # main()'s actual ordering: import the SDK itself FIRST (forcing its one-time init to run
    # now), cap SECOND. A later re-import (this test's own `import tastytrade` above, or the
    # real streaming call) is then a sys.modules cache hit -- __init__.py does not re-run, so
    # the cap sticks for the rest of the process.
    rc, _lines = _run(provider, store)
    assert rc == 0
    assert logging.getLogger("tastytrade").level == logging.WARNING
