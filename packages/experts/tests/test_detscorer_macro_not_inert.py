"""The macro regime must stay wired.

Until 2026-08-11 ``fetch_macro_series`` read ``providers.macro()`` behind a
``hasattr`` guard. No provider bundle has ever defined ``macro()``, so the guard was
always False, every FRED input resolved to None, and ``regime_composite``
renormalized onto its single surviving input -- "macro regime" was really just
``+1 if SPY > SMA200 else -1``, in live and backtest alike. Nothing failed; the
numbers merely stopped meaning what they claimed.

These tests fail if that regression returns: if the inputs stop arriving, if the
weight table drifts back onto inputs nothing feeds, or if a stand-in series gets
substituted into a scorer calibrated for different units.
"""
import json
import os

import pytest

from ba2_providers.macro import fred_series as fs
from ba2_experts.DeterministicScorer import data
from ba2_experts.DeterministicScorer.macro import DEF_MW


@pytest.fixture(autouse=True)
def _fred_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "CACHE_FOLDER", str(tmp_path))
    fs.reset_cache()
    data.reset_caches()
    os.makedirs(os.path.join(str(tmp_path), "fred"), exist_ok=True)

    def write(series_id, vintage, rows):
        """The first-release format ``refresh_series`` writes; a daily row with no
        ``realtime_start`` is stamped as published on its own date."""
        from datetime import datetime, timezone

        rows = [dict(r, realtime_start=r.get("realtime_start", r["date"])) for r in rows]
        with open(fs.cache_path(series_id), "w", encoding="utf-8") as fh:
            json.dump({"series_id": series_id,
                       "fetched_at": datetime.now(timezone.utc).isoformat(),
                       "availability": fs.AVAIL_FIRST_RELEASE,
                       "format": fs.CACHE_FORMAT_FIRST_RELEASE,
                       "first_vintage": "2000-01-03", "observations": rows}, fh)

    # 80 daily points is enough for credit_score (needs >= 60) and the yc average.
    daily = [{"date": f"2024-01-{d:02d}" if d <= 31 else f"2024-02-{d - 31:02d}",
              "value": str(3.0 + (d % 7) * 0.1)} for d in range(1, 81)]
    write("VIXCLS", False, [{"date": o["date"], "value": "18.5"} for o in daily])
    write("BAA10Y", False, daily)
    write("T10Y3M", False, [{"date": o["date"], "value": "0.6"} for o in daily])
    # Sahm needs >= 13 monthly points, cut on first-publication date.
    write("UNRATE", True, [
        {"date": f"2023-{m:02d}-01", "value": "3.7",
         "realtime_start": f"2023-{m + 1:02d}-05"} for m in range(1, 12)
    ] + [
        {"date": "2023-12-01", "value": "3.8", "realtime_start": "2024-01-05"},
        {"date": "2024-01-01", "value": "3.9", "realtime_start": "2024-02-02"},
    ])
    yield
    fs.reset_cache()
    data.reset_caches()


def test_macro_inputs_are_actually_populated():
    """The regression: every one of these was None for months."""
    out = data.fetch_macro_series(None, "2024-03-20")
    assert out["vix"] is not None, "VIX input is not arriving"
    assert out["unrate_series"] is not None and len(out["unrate_series"])
    assert out["oas_series"] is not None and len(out["oas_series"])
    assert out["spread_10y3m_series"] is not None and len(out["spread_10y3m_series"])


def test_fetch_does_not_depend_on_a_bundle_macro_method():
    """Passing providers=None must still work -- macro reads the disk cache directly.

    If this ever needs a bundle again, the hasattr guard must NOT come back with it.
    """
    out = data.fetch_macro_series(None, "2024-03-20")
    assert out["vix"] is not None


def test_weight_table_only_covers_inputs_that_are_fed():
    """A weight on an input nothing supplies is GA search width spent on nothing."""
    assert set(DEF_MW) == {"trend_index", "vix", "credit", "yield_curve", "sahm"}
    assert "breadth" not in DEF_MW, "breadth is still None (needs screener integration)"
    assert "pmi" not in DEF_MW, "NAPM no longer exists on FRED"
    assert sum(DEF_MW.values()) == pytest.approx(1.0)


def test_unrate_respects_publication_date_through_the_expert_path():
    """January's rate is dated 2024-01-01 but was not public until 2024-02-02."""
    before = data.fetch_macro_series(None, "2024-02-01")["unrate_series"]
    data.reset_caches()
    after = data.fetch_macro_series(None, "2024-02-02")["unrate_series"]
    assert len(after) == len(before) + 1
    assert str(after.index[-1].date()) == "2024-01-01"


def test_pmi_key_is_gone_entirely():
    """pmi_score is hard-wired to ISM's 50 boundary; a rescaled stand-in would pin it."""
    assert "pmi" not in data.fetch_macro_series(None, "2024-03-20")


@pytest.mark.parametrize("mode", ["enforce", "observe", "legacy"])
def test_an_unestablishable_macro_read_is_never_absorbed(monkeypatch, mode):
    """A cache in the old observation-date format cannot say what a decision could see. The
    expert must not quietly fall back to the index trend alone -- in ANY error mode."""
    monkeypatch.setenv("BA2_ERROR_MODE", mode)
    with open(fs.cache_path("VIXCLS"), "w", encoding="utf-8") as fh:
        json.dump({"series_id": "VIXCLS", "vintage": False,
                   "observations": [{"date": "2024-01-02", "value": "18.5",
                                     "realtime_start": "2026-09-23"}]}, fh)
    fs.reset_cache()
    data.reset_caches()
    with pytest.raises(fs.MacroAvailabilityUnknown, match="first-release format"):
        data.fetch_macro_series(None, "2024-03-20")
