"""Phase 3 / Task 7: survivorship-bias-removal integration test (gate item 3).

This is the **gate item 3** anchor: a symbol that delisted BEFORE today but TRADED
on ``scan_date`` (``ipoDate <= scan_date <= delistedDate``) must APPEAR in that
date's reconstructed universe, and must DISAPPEAR from a universe after its
``delistedDate``. A fixed-current-universe run (the live FMP screener, which returns
today's listings only) would OMIT it — so the delta between the historical path and
"today's listings" is exactly the survivorship bias the historical universe removes.

All FMP access is monkeypatched (lifecycle map + as-of metric helpers) — no live FMP
key / network is required (the test DBs have no FMP key, and this asserts pure
universe-window logic + the Stage-1 threshold pass, both of which are deterministic).

Reconciliations vs. the plan's draft (the draft pinned a speculative helper shape):
  - ``_market_cap_at`` returns a ``(market_cap, source)`` TUPLE in the real provider
    (Task-3 deliverable / audit columns), so the patch returns the tuple — a bare
    float would crash on ``mcap, mcap_source = self._market_cap_at(...)``.
  - The provider class name ``FMPHistoricalScreenerProvider`` is re-exported at the
    ``ba2_providers.screener`` package level and shadows the same-named submodule on
    attribute access, so the MODULE object is obtained via ``importlib`` (so that
    monkeypatching module-level ``fetch_lifecycle_map``/``broad_universe`` works) —
    matching the existing ``test_historical_screener.py`` pattern.
"""
from datetime import datetime, timezone

import importlib

# Real MODULE objects (not the package-level re-exported class), so module-level
# ``fetch_lifecycle_map`` / ``broad_universe`` can be monkeypatched.
H = importlib.import_module("ba2_providers.screener.FMPHistoricalScreenerProvider")
U = importlib.import_module("ba2_providers.screener.universe")


def D(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


# A loser that delisted on 2021-03-15 — absent from today's universe, present in 2020.
# A winner that is still active (delisted_date=None => +inf).
LIFECYCLE = {
    "WINNER": (D("2010-01-01"), None),
    "LOSER": (D("2014-01-01"), D("2021-03-15")),
}

FILTERS = {
    "price_min": 20,
    "market_cap_min": 1_000_000_000,
    "volume_min": 500_000,
    "limit": 100,
}


def _build(monkeypatch, mode: str = "broad"):
    """Build a historical provider whose universe is driven by LIFECYCLE and whose
    as-of metrics all comfortably pass the FILTERS thresholds (so survival is decided
    purely by the lifecycle window, not by a metric edge case)."""
    p = H.FMPHistoricalScreenerProvider(universe_mode=mode)
    p.api_key = "x"
    # Universe: the REAL survivorship-free window logic over the fixed LIFECYCLE.
    monkeypatch.setattr(H, "fetch_lifecycle_map", lambda: LIFECYCLE)
    monkeypatch.setattr(
        H, "broad_universe",
        lambda a, lifecycle=None: U.broad_universe(a, lifecycle=LIFECYCLE),
    )
    # As-of metrics: every surviving symbol clears the thresholds.
    monkeypatch.setattr(p, "_close_at", lambda s, a: 50.0)
    monkeypatch.setattr(
        p, "_market_cap_at", lambda s, a, c: (5e9, "shares_x_close")
    )  # real signature returns (mcap, source)
    monkeypatch.setattr(p, "_avg_volume_at", lambda s, a, window=20: 1_000_000)
    return p


def test_delisted_symbol_present_on_traded_date(monkeypatch):
    """LOSER traded in 2020 (2014 <= 2020-06-30 <= 2021-03-15) -> present."""
    p = _build(monkeypatch)
    on_2020 = {r["symbol"] for r in p.screen_stocks(FILTERS, as_of=D("2020-06-30"))}
    assert "LOSER" in on_2020  # survivorship-free: traded in 2020 -> present
    assert "WINNER" in on_2020


def test_delisted_symbol_absent_after_death(monkeypatch):
    """LOSER delisted 2021-03-15 -> gone from the 2022 universe; WINNER still present."""
    p = _build(monkeypatch)
    on_2022 = {r["symbol"] for r in p.screen_stocks(FILTERS, as_of=D("2022-01-03"))}
    assert "LOSER" not in on_2022  # delisted before 2022 -> absent
    assert "WINNER" in on_2022


def test_delisted_symbol_absent_before_ipo(monkeypatch):
    """LOSER IPO'd 2014-01-01 -> absent from a 2013 universe (not yet public)."""
    p = _build(monkeypatch)
    on_2013 = {r["symbol"] for r in p.screen_stocks(FILTERS, as_of=D("2013-06-30"))}
    assert "LOSER" not in on_2013  # not yet public on 2013-06-30
    assert "WINNER" in on_2013  # WINNER IPO'd 2010 -> public


def test_fixed_current_universe_would_omit_loser(monkeypatch):
    """The delta vs. a fixed-current-universe run IS the survivorship fix.

    The live FMP screener returns today's listings only; LOSER is delisted now, so a
    "replay the live screener over the past" approach would OMIT it on every date.
    The historical path INCLUDES it on 2020 -> the delta is exactly the bias removed.
    """
    p = _build(monkeypatch)
    hist_2020 = {r["symbol"] for r in p.screen_stocks(FILTERS, as_of=D("2020-06-30"))}
    # "today's listings" = symbols with no delistedDate (still active now).
    current_listings = {s for s, (ipo, dl) in LIFECYCLE.items() if dl is None}
    assert "LOSER" in hist_2020  # historical universe includes it on its trading date
    assert "LOSER" not in current_listings  # the bias a current-universe run carries
    # The set difference is non-empty == survivorship bias was removed.
    assert (hist_2020 - current_listings) == {"LOSER"}
