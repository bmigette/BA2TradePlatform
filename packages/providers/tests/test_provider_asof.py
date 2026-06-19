"""as_of byte-equality + lookahead-fix tests for the corrected providers (Task 4).

The two invariants proven here:
  - as_of=None keeps the live behaviour byte-identical (no live-behaviour change).
  - as_of=<date> enforces the no-lookahead effective-date anchor (the bug fixes:
    insider filingDate; statements fillingDate/acceptedDate).

These use in-test mocks of the raw FMP fetch; the authoritative real-FMP byte-equality
probe is a separate (network) test_files script per Task 11 Step 3.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
    FMPCompanyDetailsProvider,
)
from ba2_providers.cache import cached_get


# ---------------------------------------------------------------------------
# Insider lookahead bug (#1): transactionDate -> filingDate
# ---------------------------------------------------------------------------
FAKE_INSIDER = [
    {"reportingName": "A", "transactionType": "P-Purchase", "transactionDate": "2026-01-05",
     "filingDate": "2026-01-08", "securitiesTransacted": "1000", "price": "10"},
    {"reportingName": "B", "transactionType": "P-Purchase", "transactionDate": "2026-01-06",
     "filingDate": "2026-03-01", "securitiesTransacted": "1000", "price": "10"},  # filed late
]


def _insider_prov():
    p = FMPInsiderProvider.__new__(FMPInsiderProvider)
    p.api_key = "x"
    return p


def test_asof_none_matches_pre_refactor_transactiondate_filter():
    """as_of=None keeps the live transactionDate-range behaviour (byte-equal):
    both A and B count because their transactionDate is in the January window."""
    with patch.object(FMPInsiderProvider, "_fetch_insider_history",
               return_value=FAKE_INSIDER):
        out = _insider_prov().get_insider_transactions(
            "AAPL", end_date=datetime(2026, 1, 31, tzinfo=timezone.utc),
            lookback_days=60, as_of=None, format_type="dict")
    assert out["transaction_count"] == 2  # B counts: its transactionDate is in range


def test_asof_enforces_filingdate_no_lookahead():
    """as_of mid-Feb: B's filingDate (Mar 1) is AFTER as_of => excluded (bug fix)."""
    with patch.object(FMPInsiderProvider, "_fetch_insider_history",
               return_value=FAKE_INSIDER):
        out = _insider_prov().get_insider_transactions(
            "AAPL", end_date=datetime(2026, 2, 15, tzinfo=timezone.utc),
            lookback_days=60, as_of=datetime(2026, 2, 15, tzinfo=timezone.utc),
            format_type="dict")
    names = {t["insider_name"] for t in out["transactions"]}
    assert names == {"A"}, f"filingDate lookahead leak: {names}"


def test_insider_cached_get_alias_threads_asof():
    """cached_get.insider_get maps as_of -> end_date AND as_of, so the corrected
    provider's no-lookahead anchor fires."""
    with patch.object(FMPInsiderProvider, "_fetch_insider_history",
               return_value=FAKE_INSIDER):
        out = cached_get.insider_get(
            _insider_prov(), "AAPL",
            as_of=datetime(2026, 2, 15, tzinfo=timezone.utc), lookback=60)
    names = {t["insider_name"] for t in out["transactions"]}
    assert names == {"A"}, f"alias lookahead leak: {names}"


# ---------------------------------------------------------------------------
# Statement lookahead bug (#2): fiscalDateEnding -> fillingDate/acceptedDate
# ---------------------------------------------------------------------------
# fillingDate is FMP's known double-l typo on statement rows.
FAKE_STATEMENTS = [
    {"date": "2025-12-31", "fillingDate": "2026-02-20", "totalAssets": 200},  # filed late
    {"date": "2025-09-30", "fillingDate": "2025-10-25", "totalAssets": 150},
]


def _details_prov():
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "x"
    return p


def test_statement_asof_none_returns_all_byte_identical():
    """as_of=None: the effective-date pre-pass is skipped; both statements returned
    in the original fiscalDateEnding order (byte-identical to pre-refactor)."""
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_list_call",
               return_value=FAKE_STATEMENTS):
        out = _details_prov().get_balance_sheet(
            "AAPL", "quarterly", end_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
            lookback_periods=10, as_of=None, format_type="dict")
    fiscal_dates = [s["fiscal_date_ending"] for s in out["statements"]]
    assert fiscal_dates == ["2025-12-31", "2025-09-30"]


def test_statement_asof_enforces_fillingdate_no_lookahead():
    """as_of = early Feb 2026: the 2025-12-31 statement was not FILED until
    2026-02-20, so it is excluded; only the Q3 statement (filed Oct 2025) is knowable."""
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_list_call",
               return_value=FAKE_STATEMENTS):
        out = _details_prov().get_balance_sheet(
            "AAPL", "quarterly", end_date=datetime(2026, 2, 5, tzinfo=timezone.utc),
            lookback_periods=10, as_of=datetime(2026, 2, 5, tzinfo=timezone.utc),
            format_type="dict")
    fiscal_dates = [s["fiscal_date_ending"] for s in out["statements"]]
    assert fiscal_dates == ["2025-09-30"], f"fillingDate lookahead leak: {fiscal_dates}"


def test_statement_cached_get_alias_threads_asof():
    """cached_get.statement_get routes through get_balance_sheet with as_of enforced."""
    with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_list_call",
               return_value=FAKE_STATEMENTS):
        out = cached_get.statement_get(
            _details_prov(), "AAPL", "balance_sheet",
            as_of=datetime(2026, 2, 5, tzinfo=timezone.utc),
            frequency="quarterly", lookback_periods=10)
    fiscal_dates = [s["fiscal_date_ending"] for s in out["statements"]]
    assert fiscal_dates == ["2025-09-30"], f"alias lookahead leak: {fiscal_dates}"


# ---------------------------------------------------------------------------
# No-lookahead determinism (Task 11 Step 2)
# ---------------------------------------------------------------------------
# A fixed (symbol, as_of) must replay deterministically (same rows every call) AND
# every returned row's effective_date must be <= as_of. We prove the invariant at
# both layers it is enforced: the provider client-side filter and the native cache.
from ba2_providers.cache import native_cache as nc  # noqa: E402
from ba2_common.core.provider_utils import (  # noqa: E402
    insider_effective_date,
    statement_effective_date,
    parse_provider_date,
)


def test_insider_asof_is_deterministic_and_no_lookahead():
    """A fixed (symbol, as_of) returns the SAME transactions across repeated calls,
    and EVERY returned transaction has effective_date (filingDate) <= as_of."""
    as_of = datetime(2026, 2, 15, tzinfo=timezone.utc)
    results = []
    for _ in range(3):
        with patch.object(FMPInsiderProvider, "_fetch_insider_history",
                   return_value=FAKE_INSIDER):
            out = _insider_prov().get_insider_transactions(
                "AAPL", end_date=as_of, lookback_days=60, as_of=as_of,
                format_type="dict")
        results.append(tuple(sorted(t["insider_name"] for t in out["transactions"])))

    # Deterministic: identical set of transactions on every replay.
    assert len(set(results)) == 1, f"non-deterministic as_of replay: {results}"
    assert results[0] == ("A",)  # B's filingDate (Mar 1) is after as_of

    # Cross-check the effective-date invariant directly against the RAW rows: every
    # row that survived the filter must have filingDate <= as_of.
    surviving = {n for n in results[0]}
    for raw in FAKE_INSIDER:
        eff = insider_effective_date(raw)
        if raw["reportingName"] in surviving:
            assert eff is not None and eff <= as_of, (
                f"lookahead: {raw['reportingName']} effective {eff} > as_of {as_of}")
        else:
            # Excluded rows are excluded BECAUSE their effective_date is after as_of.
            assert eff is None or eff > as_of


def test_statement_asof_is_deterministic_and_no_lookahead():
    """Fixed (symbol, as_of) statement read replays identically and every returned
    statement was FILED (fillingDate) on or before as_of."""
    as_of = datetime(2026, 2, 5, tzinfo=timezone.utc)
    results = []
    for _ in range(3):
        with patch("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_list_call",
                   return_value=FAKE_STATEMENTS):
            out = _details_prov().get_balance_sheet(
                "AAPL", "quarterly", end_date=as_of, lookback_periods=10,
                as_of=as_of, format_type="dict")
        results.append(tuple(s["fiscal_date_ending"] for s in out["statements"]))

    assert len(set(results)) == 1, f"non-deterministic statement replay: {results}"
    assert results[0] == ("2025-09-30",)

    surviving_fiscal = set(results[0])
    for raw in FAKE_STATEMENTS:
        eff = statement_effective_date(raw)
        if raw["date"] in surviving_fiscal:
            assert eff is not None and eff <= as_of, (
                f"statement lookahead: {raw['date']} effective {eff} > as_of {as_of}")
        else:
            assert eff is None or eff > as_of


def test_native_cache_read_is_deterministic_and_no_lookahead():
    """The cache layer's read_event_rows replays a fixed (symbol, as_of) identically
    and guarantees effective_date <= as_of for every returned row."""
    rows = [
        {"insider_name": "DET-A", "transactionDate": "2026-01-05",
         "filingDate": "2026-01-08", "v": 1},
        {"insider_name": "DET-B", "transactionDate": "2026-02-01",
         "filingDate": "2026-02-20", "v": 2},
        {"insider_name": "DET-C", "transactionDate": "2026-02-10",
         "filingDate": "2026-04-01", "v": 3},  # filed far in the future
    ]
    nc.upsert_event_rows(
        "FMPInsiderProvider", "insider_txn", "DETSYM", rows,
        value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
        effective_date_fn=insider_effective_date)

    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    reads = [
        tuple(sorted(r["insider_name"]
                     for r in nc.read_event_rows(
                         "FMPInsiderProvider", "insider_txn", "DETSYM", as_of)))
        for _ in range(3)
    ]
    assert len(set(reads)) == 1, f"non-deterministic cache replay: {reads}"
    # DET-C (filed Apr 1) must be excluded; A and B are knowable by Mar 1.
    assert reads[0] == ("DET-A", "DET-B")

    # Invariant: every returned row's effective_date <= as_of.
    got = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "DETSYM", as_of)
    for r in got:
        eff = insider_effective_date(r)
        assert eff is not None and eff <= as_of, (
            f"cache lookahead: {r['insider_name']} effective {eff} > as_of {as_of}")
