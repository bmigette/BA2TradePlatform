"""A deployed screener setting must land under the name the live screener READS.

THE DEFECT (parity review 2026-09-07, P1 #1). ``live_settings_from_universe`` copied a
payload's screener block verbatim on the premise that "the live settings already exist under
the SAME names". That is only half true. The block mixes two vocabularies:

  * the GA's ``screener:*`` genes decode to live names already -- ``screener_market_cap_min``
  * the run-level ``screener_opt.base_settings`` use the metric store's UNPREFIXED names --
    ``market_cap_max``

Copied verbatim, an unprefixed key is written to expertsetting under a name ``StockScreener``
never reads, so the live screener quietly keeps its own default. For ``market_cap_max`` that
default is ``0``, which means NO CEILING -- so prod instances 8-12 were deployed with the upper
bound of their capitalisation band missing outright, and "small"/"mid" stopped describing a
bounded live universe. Nothing errored; the key was present in the DB, just inert.

The values were always correct. Canonicalising the prefix is the entire repair.
"""
import pytest

from ba2_common.core.deploy_parity import (
    LIVE_SCREENER_SETTINGS,
    SCREENER_UNIVERSE_SETTING,
    live_settings_from_universe,
    unmapped_screener_keys,
)


def _universe(**screener_settings):
    return {"mode": "screener", "screener_store": "/store",
            "screener_settings": dict(screener_settings)}


class TestUnprefixedKeysAreCanonicalised:
    def test_market_cap_max_reaches_the_key_the_screener_reads(self):
        """THE DEFECT, in one line: this landed as `market_cap_max` and was ignored."""
        out = live_settings_from_universe(_universe(market_cap_max=10_000_000_000.0))
        assert out["screener_market_cap_max"] == 10_000_000_000.0
        assert "market_cap_max" not in out

    @pytest.mark.parametrize("bare", [
        "market_cap_min", "market_cap_max", "volume_min", "volume_max",
        "float_min", "float_max", "price_min", "price_max",
        "relative_volume_min", "price_drop_pct", "price_drop_days",
        "max_stocks", "sort_metric", "weinstein_stage2_only",
    ])
    def test_every_bare_screener_name_maps(self, bare):
        """market_cap_max is the one that bit; the others are the same shape and would bite
        the same way the first time a grid pins one."""
        out = live_settings_from_universe(_universe(**{bare: 7}))
        assert out[f"screener_{bare}"] == 7
        assert bare not in out

    def test_the_value_is_carried_unchanged(self):
        """Renaming a key must not coerce, round or re-type the value it carries."""
        out = live_settings_from_universe(_universe(market_cap_max=2_000_000_000.0))
        assert out["screener_market_cap_max"] == 2_000_000_000.0
        assert isinstance(out["screener_market_cap_max"], float)


class TestAlreadyCorrectKeysAreUntouched:
    """The inverse: the gene-derived half of the block was never broken."""

    def test_a_prefixed_key_passes_straight_through(self):
        out = live_settings_from_universe(_universe(screener_market_cap_min=5_000_000_000.0))
        assert out["screener_market_cap_min"] == 5_000_000_000.0

    def test_a_mixed_block_ends_up_entirely_prefixed(self):
        """The real shape: run-level base settings + decoded genes in one dict."""
        out = live_settings_from_universe(_universe(
            market_cap_max=10_000_000_000.0,
            screener_market_cap_min=5_000_000_000.0,
            screener_max_stocks=40,
        ))
        assert out["screener_market_cap_max"] == 10_000_000_000.0
        assert out["screener_market_cap_min"] == 5_000_000_000.0
        assert out["screener_max_stocks"] == 40
        assert all(k.startswith("screener_") or k == SCREENER_UNIVERSE_SETTING for k in out)

    def test_the_selection_method_is_still_switched_on(self):
        out = live_settings_from_universe(_universe(market_cap_max=1))
        assert out[SCREENER_UNIVERSE_SETTING] == "screener"

    def test_a_static_universe_still_maps_to_nothing(self):
        assert live_settings_from_universe({"mode": "static", "symbols": ["AAPL"]}) == {}
        assert live_settings_from_universe(None) == {}


class TestAnUnmappableKeyIsReported:
    """A key that is neither live nor one prefix from live is inert -- say so out loud."""

    def test_an_unknown_key_is_listed(self):
        assert unmapped_screener_keys(_universe(bogus_key=1)) == ["bogus_key"]

    def test_it_is_still_carried_rather_than_dropped(self):
        """Reporting is not deleting: a key this contract does not know about may be
        meaningful to an expert's own settings, so it travels and gets flagged."""
        assert live_settings_from_universe(_universe(bogus_key=1))["bogus_key"] == 1

    def test_mapped_and_prefixed_keys_are_not_reported(self):
        uni = _universe(market_cap_max=1, screener_max_stocks=2)
        assert unmapped_screener_keys(uni) == []

    def test_a_static_universe_reports_nothing(self):
        assert unmapped_screener_keys({"mode": "static", "symbols": []}) == []


def test_the_name_table_matches_what_the_interface_declares():
    """LIVE_SCREENER_SETTINGS is a hand-kept mirror of MarketExpertInterface's screener
    settings -- deliberately, so an upstream rename surfaces HERE as a parity failure rather
    than silently changing what a deploy writes. This test is what makes that safe."""
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    MarketExpertInterface._ensure_builtin_settings()
    declared = {k for k in MarketExpertInterface._builtin_settings
                if k.startswith("screener_")}
    assert declared, "the interface must declare screener settings for this test to mean anything"
    assert declared == set(LIVE_SCREENER_SETTINGS)
