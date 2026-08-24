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
    argv = ["--symbols", "AAPL", "--start", START.isoformat(), "--end", END.isoformat(),
            "--rate-limit", "0", *extra]
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
               "--rate-limit", "0"],
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
               "--rate-limit", "0", "--max-retries", "4", "--backoff", "2"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: (slept.append(s), clock.advance(s))[0], log=lambda _l: None)
    backoffs = [s for s in slept if s > 0]
    assert backoffs == [2.0, 4.0, 8.0], f"expected doubling backoff, got {backoffs}"


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
               "--rate-limit", "1.5"],
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
               "--rate-limit", "0", "--progress-every", "1"],
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
               "--end", END.isoformat(), "--rate-limit", "0", "--dry-run"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lambda _l: None)
    assert provider.discovered == ["AAPL", "MSFT"]


def test_symbols_are_upper_cased_and_deduplicated(provider, store):
    clock = FakeClock()
    warm.main(["--symbols", "aapl,AAPL, msft ", "--start", START.isoformat(),
               "--end", END.isoformat(), "--dry-run"],
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
               "--rate-limit", "0", "--dry-run"],
              provider=provider, store=store, clock=clock,
              sleep=lambda s: clock.advance(s), log=lambda _l: None)
    assert provider.discovered == ["AAPL"]


# --------------------------------------------------------------------------- #
def _files_under(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        out.extend(os.path.join(dirpath, f) for f in files)
    return out
