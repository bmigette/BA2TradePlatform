"""
Tests for PennyMomentumTrader improvements from missed-gainers analysis (2026-03-26).

Covers:
- Gainers merge: unknown market cap (mcap=0) should not silently drop valid stocks
- Gainers merge: quote enrichment should update market_cap from live quote
- Settings defaults: max_scan_candidates raised to 100
- Settings defaults: min_confidence_threshold lowered to 45
- Quick filter prompt: biotech catalyst guidance is non-exhaustive and includes business events
- Deep triage prompt: guidance to check after-hours / prior-evening news
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Direct module loading helpers (mirrors test_penny_fixes.py pattern)
# ---------------------------------------------------------------------------

_BASE = os.path.join(
    os.path.dirname(__file__),
    "..",
    "ba2_trade_platform",
    "modules",
    "experts",
    "PennyMomentumTrader",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _read_source(filename: str) -> str:
    return open(os.path.join(_BASE, filename)).read()


def _load_prompts_module():
    """Load prompts.py directly without triggering full package init."""
    mock_logger = MagicMock()
    for mod_name in ["ba2_trade_platform", "ba2_trade_platform.logger"]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()
    sys.modules["ba2_trade_platform.logger"] = MagicMock(logger=mock_logger)

    spec = importlib.util.spec_from_file_location(
        "penny_prompts", os.path.join(_BASE, "prompts.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["penny_prompts"] = mod
    spec.loader.exec_module(mod)
    return mod


_prompts = _load_prompts_module()


# ===========================================================================
# 1. Gainers merge: unknown market cap must not silently discard valid stocks
# ===========================================================================

class TestGainersMarketCapFix:
    """
    The FMP /stock_market/gainers endpoint returns no marketCap field.
    Stocks with mcap=0 were being silently rejected by the mcap_min guard,
    meaning the entire include_gainers feature was ineffective for stocks
    only visible via the gainers list.
    """

    def test_gainers_mcap_guard_uses_zero_check(self):
        """mcap_min filter must guard against zero before comparing — 0 < mcap_min is not valid rejection."""
        source = _read_source("__init__.py")
        # Find the gainers merge block
        idx = source.index("_fetch_gainers")
        block = source[idx: idx + 2000]
        # The old broken pattern was: `g_mcap < float(mcap_min)` which would reject mcap=0
        # The fix requires a zero-guard: only apply the lower-bound when mcap is actually known
        assert (
            "g_mcap > 0 and g_mcap < float(mcap_min)" in block
            or "g_mcap and g_mcap < float(mcap_min)" in block
        ), (
            "Gainers mcap_min check must guard against unknown (zero) mcap. "
            "Use `g_mcap > 0 and g_mcap < float(mcap_min)` so stocks with no "
            "market cap data are not silently discarded."
        )

    def test_gainers_mcap_upper_guard_uses_zero_check(self):
        """mcap_max filter must also guard against zero for the same reason."""
        source = _read_source("__init__.py")
        idx = source.index("_fetch_gainers")
        block = source[idx: idx + 2000]
        assert (
            "g_mcap > 0 and g_mcap > float(mcap_max)" in block
            or "g_mcap and g_mcap > float(mcap_max)" in block
        ), (
            "Gainers mcap_max check must also guard against zero mcap so "
            "stocks with unknown market cap can still enter the pipeline."
        )

    def test_gainers_rejections_are_logged_to_filtered_stocks(self):
        """Gainers rejected for price/mcap mismatch must be logged so they are visible in the UI."""
        source = _read_source("__init__.py")
        idx = source.index("_fetch_gainers")
        block = source[idx: idx + 2500]
        assert "filtered_stocks" in block, (
            "Gainers merge must log rejected stocks to filtered_stocks so operators "
            "can see why a high-gaining stock was not included."
        )

    def test_quote_enrichment_updates_market_cap(self):
        """After fetching live quotes, market_cap should be updated so gainers get their real mcap."""
        source = _read_source("__init__.py")
        # Find the quote enrichment loop (after _fetch_quotes_chunked call)
        idx = source.index("_fetch_quotes_chunked")
        enrich_block = source[idx: idx + 1500]
        assert "market_cap" in enrich_block, (
            "The quote enrichment loop must write market_cap from the live quote "
            "so that gainers (which have mcap=0 from the FMP gainers API) get "
            "their real market cap before filtering or LLM evaluation."
        )


# ===========================================================================
# 2. Settings defaults
# ===========================================================================

class TestUpdatedSettingsDefaults:
    """Verify updated defaults that improve candidate coverage and recall."""

    def _src(self):
        return _read_source("__init__.py")

    def test_max_scan_candidates_default_is_100(self):
        """
        Raised from 50 → 100 so that stocks with moderate RVOL (e.g. 2.4x) are not
        cut by the capacity cap when there are many active movers.
        """
        source = self._src()
        idx = source.index('"max_scan_candidates"')
        block = source[idx: idx + 300]
        assert '"default": 100,' in block, (
            f"max_scan_candidates default must be 100. Got block: {block[:200]}"
        )

    def test_min_confidence_threshold_default_is_45(self):
        """
        Lowered from 55 → 45 to capture more speculative-but-valid setups.
        ICU was rejected at confidence=40 but went on to gain +28.5%.
        """
        source = self._src()
        idx = source.index('"min_confidence_threshold"')
        block = source[idx: idx + 300]
        assert '"default": 45,' in block, (
            f"min_confidence_threshold default must be 45. Got block: {block[:200]}"
        )


# ===========================================================================
# 3. Quick filter prompt: biotech catalyst guidance
# ===========================================================================

class TestQuickFilterPromptBiotechGuidance:
    """
    The old prompt categorically dropped Healthcare/Biotech unless the catalyst was
    "earnings, not FDA trial". This missed LNAI (+52%) which had a clear $20M strategic
    transaction catalyst — a business event, not an FDA trial.

    The new guidance should be non-exhaustive: give examples of valid catalysts but
    explicitly leave room for the LLM to use its own judgment.
    """

    def _prompt(self) -> str:
        return _prompts.build_quick_filter_prompt(
            [
                {
                    "symbol": "LNAI",
                    "price": 0.6,
                    "volume": 50_000_000,
                    "market_cap": 15_000_000,
                    "sector": "Healthcare",
                    "industry": "Biotechnology",
                    "exchange": "NASDAQ",
                }
            ]
        )

    def test_prompt_mentions_business_deal_catalysts(self):
        """Valid catalyst examples must include business events beyond just earnings."""
        p = self._prompt().lower()
        has_business_event = (
            "acqui" in p
            or "merger" in p
            or "transaction" in p
            or "partnership" in p
            or "contract" in p
            or "deal" in p
        )
        assert has_business_event, (
            "Quick filter prompt must mention business deal / partnership / acquisition "
            "as example valid catalysts for Healthcare/Biotech stocks so the LLM does "
            "not reflexively drop stocks with non-FDA catalysts."
        )

    def test_prompt_invites_llm_judgment_not_exhaustive_rules(self):
        """The catalyst list must be framed as examples, not an exhaustive rule set."""
        p = self._prompt().lower()
        invites_judgment = (
            "judgment" in p
            or "not limited to" in p
            or "such as" in p
            or "e.g." in p
            or "example" in p
            or "including but" in p
            or "use your" in p
        )
        assert invites_judgment, (
            "Quick filter prompt must indicate that the provided catalyst examples are "
            "non-exhaustive so the LLM applies judgment rather than a rigid checklist."
        )

    def test_prompt_retains_fda_binary_risk_guidance(self):
        """FDA binary-event risk guidance should still be present (it is valid risk management)."""
        p = self._prompt().lower()
        assert "fda" in p or "binary" in p or "trial" in p, (
            "FDA binary-event risk guidance must still appear in the prompt — "
            "the fix broadens the catalyst list, it does not remove the FDA risk warning."
        )


# ===========================================================================
# 4. Deep triage prompt: after-hours / prior-evening news guidance
# ===========================================================================

class TestDeepTriagePromptAfterHoursNews:
    """
    SLND was repeatedly rejected for 'no catalyst' but gained +27% after a
    $118M contract award published the prior evening. The LLM needs explicit
    guidance to look for after-hours and prior-evening releases.
    """

    def _prompt(self) -> str:
        return _prompts.build_deep_triage_prompt(
            symbol="SLND",
            news="No news available.",
            insider="No insider data.",
            fundamentals="Revenue: $10M",
            social="Neutral sentiment.",
        )

    def test_prompt_mentions_after_hours_news(self):
        """Deep triage prompt must instruct LLM to check after-hours or prior-evening news."""
        p = self._prompt().lower()
        has_after_hours = (
            ("after" in p and ("hour" in p or "close" in p or "market" in p))
            or "evening" in p
            or "overnight" in p
            or "prior day" in p
            or "pre-market" in p
        )
        assert has_after_hours, (
            "Deep triage prompt must mention after-hours, prior-evening, or overnight "
            "news so the LLM does not dismiss pre-market movers that had a late catalyst."
        )

    def test_prompt_guidance_is_illustrative_not_exhaustive(self):
        """Deep triage catalyst guidance must still invite the LLM to exercise judgment."""
        p = self._prompt().lower()
        invites_judgment = (
            "not limited to" in p
            or "such as" in p
            or "e.g." in p
            or "example" in p
            or "including" in p
            or "use your" in p
            or "judgment" in p
        )
        assert invites_judgment, (
            "Deep triage catalyst framework must be phrased as guidance with examples, "
            "not an exhaustive checklist, so the LLM can recognise novel catalysts."
        )
