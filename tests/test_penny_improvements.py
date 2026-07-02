"""
Tests for the June-2026 post-mortem-driven PennyMomentumTrader improvements.

Covers (all offline — every FMP/network call is mocked):
1. Split-transition guard: split-calendar filtering + price-discontinuity flag
   (ALIT 1:20 / RMTI 1:10 reverse-split data traps).
2. Trail-after-max-holding: ratchet math (profit -> trail, never loosens;
   loss -> flat close) at phase 0, and the pure trailing helpers.
3. Entry window: max_entry_age_days default is 3 (not 1).
4. Reversal lane in the quick-filter prompt (CALC +74% was rejected as
   "decliner, no catalyst").
5. Filter post-mortem: stage-stats computation from a fixed fixture with mocked
   quotes, persistence + reprocess marker, fail-soft behavior.
6. eod_flat: opt-in EOD close-all (AZI -15% overnight gap through a 7% stop).
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import AnalysisOutput, MarketAnalysis
from ba2_trade_platform.core.types import MarketAnalysisStatus

from ba2_experts.PennyMomentumTrader import PennyMomentumTrader
from ba2_experts.PennyMomentumTrader.settings import SETTINGS_DEFINITIONS
from ba2_experts.PennyMomentumTrader.postmortem import (
    compute_forward_returns,
    compute_stage_stats,
    find_missed_winners,
)
from ba2_experts.PennyMomentumTrader.prompts import build_quick_filter_prompt
from ba2_experts.PennyMomentumTrader.trailing import (
    apply_trailing_ratchet,
    hard_stop_price_from_conditions,
    update_high_watermark,
)
from tests.factories import (
    create_account_definition,
    create_expert_instance,
    create_market_analysis,
    create_transaction,
)


# ---------------------------------------------------------------------------
# Stub expert: real mixin methods, canned settings/prices, no LLM/broker
# ---------------------------------------------------------------------------

class _PennyStub(PennyMomentumTrader):
    """PennyMomentumTrader with __init__ bypassed and settings/prices canned."""

    def __init__(self, instance=None, settings=None, live_prices=None):
        # Deliberately do NOT call super().__init__ (no DB-backed settings, no logger wiring)
        self.instance = instance
        self.logger = MagicMock()
        self._settings = dict(settings or {})
        self._trade_mgr = MagicMock()
        self._live_prices = dict(live_prices or {})

    def get_setting_with_interface_default(self, key, log_warning=True):
        return self._settings[key]

    def _get_live_prices(self, symbols):
        return {s: self._live_prices.get(s) for s in symbols}


def _make_stub(settings=None, live_prices=None, expert="PennyMomentumTrader"):
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert=expert)
    return _PennyStub(instance=inst, settings=settings, live_prices=live_prices), inst


# ===========================================================================
# 1. Split-transition guard
# ===========================================================================

class TestSplitGuard:
    def _stub(self):
        return _PennyStub(settings={})

    def test_split_transition_symbol_dropped(self):
        """A symbol on the split calendar is dropped with a filtered_stocks entry."""
        stub = self._stub()
        candidates = [
            {"symbol": "ALIT", "price": 0.56, "previous_close": 11.20, "change_percent": 2.0},
            {"symbol": "GOOD", "price": 1.50, "previous_close": 1.40, "change_percent": 7.1},
        ]
        filtered = {}
        survivors = stub._apply_split_guard(candidates, filtered, {"ALIT"})
        assert [c["symbol"] for c in survivors] == ["GOOD"]
        assert filtered["ALIT"]["phase"] == "screen"
        assert filtered["ALIT"]["reason"] == "split_transition"

    def test_price_discontinuity_flagged_and_dropped(self):
        """Price >50% off previousClose with reported chg <20% is a data artifact."""
        stub = self._stub()
        # RMTI signature: quote shows a penny price vs a 10x previousClose while
        # the reported day-change is small.
        candidates = [
            {"symbol": "RMTI", "price": 1.10, "previous_close": 11.00, "change_percent": 3.5},
        ]
        filtered = {}
        survivors = stub._apply_split_guard(candidates, filtered, set())
        assert survivors == []
        assert filtered["RMTI"]["reason"] == "price_discontinuity"
        assert filtered["RMTI"]["phase"] == "screen"

    def test_genuine_big_mover_is_kept(self):
        """A real +120% mover (price and chg% agree) must NOT be dropped."""
        stub = self._stub()
        candidates = [
            {"symbol": "MOON", "price": 2.20, "previous_close": 1.00, "change_percent": 120.0},
        ]
        filtered = {}
        survivors = stub._apply_split_guard(candidates, filtered, set())
        assert [c["symbol"] for c in survivors] == ["MOON"]
        assert "MOON" not in filtered

    def test_candidate_without_prev_close_passes_continuity_check(self):
        stub = self._stub()
        candidates = [{"symbol": "NOPC", "price": 1.0, "change_percent": 5.0}]
        survivors = stub._apply_split_guard(candidates, {}, set())
        assert len(survivors) == 1

    def test_fetch_split_symbols_parses_calendar(self, monkeypatch):
        """Split calendar rows become an uppercase symbol set (mocked HTTP)."""
        import ba2_common.config as cfg
        import ba2_providers.fmp_common as fmp_common

        monkeypatch.setattr(cfg, "get_app_setting", lambda key: "test-key")
        captured = {}

        def fake_get(url, params, **kwargs):
            captured["url"] = url
            captured["params"] = params
            resp = MagicMock()
            resp.json.return_value = [
                {"symbol": "alit", "date": "2026-06-10", "numerator": 1, "denominator": 20},
                {"symbol": "RMTI", "date": "2026-06-10"},
            ]
            return resp

        monkeypatch.setattr(fmp_common, "fmp_http_get", fake_get)
        stub = self._stub()
        result = stub._fetch_split_symbols("2026-06-09", "2026-06-11")
        assert result == {"ALIT", "RMTI"}
        assert "stock_split_calendar" in captured["url"]
        assert captured["params"]["from"] == "2026-06-09"
        assert captured["params"]["to"] == "2026-06-11"

    def test_fetch_split_symbols_fail_soft(self, monkeypatch):
        """Any fetch error returns an empty set — the scan must never break."""
        import ba2_common.config as cfg
        import ba2_providers.fmp_common as fmp_common

        monkeypatch.setattr(cfg, "get_app_setting", lambda key: "test-key")

        def boom(*args, **kwargs):
            raise RuntimeError("FMP down")

        monkeypatch.setattr(fmp_common, "fmp_http_get", boom)
        stub = self._stub()
        assert stub._fetch_split_symbols("2026-06-09", "2026-06-11") == set()

    def test_setting_default_enabled(self):
        assert SETTINGS_DEFINITIONS["split_guard_enabled"]["default"] is True
        assert SETTINGS_DEFINITIONS["split_guard_enabled"]["type"] == "bool"


# ===========================================================================
# 2. Trailing ratchet (pure helpers)
# ===========================================================================

class TestTrailingRatchet:
    def test_high_watermark_never_lowers(self):
        info = {}
        assert update_high_watermark(info, 1.5) == 1.5
        assert update_high_watermark(info, 2.0) == 2.0
        assert update_high_watermark(info, 1.2) == 2.0  # never lowers
        assert update_high_watermark(info, None) == 2.0

    def test_hard_stop_extraction_from_composite(self):
        stop_loss = {
            "all": [
                {"type": "percent_below_entry", "percent": 7.0},
                {"any": [
                    {"type": "price_below_vwap", "timeframe": "5m"},
                    {"type": "price_below", "value": 0.90},
                ]},
            ]
        }
        # entry 1.0, 7% hard stop -> 0.93; price_below 0.90 also found; max = 0.93
        assert hard_stop_price_from_conditions(stop_loss, 1.0) == pytest.approx(0.93)
        # Without entry_price only the price_below contributes
        assert hard_stop_price_from_conditions(stop_loss, None) == pytest.approx(0.90)
        # Signal-only structure -> None
        assert hard_stop_price_from_conditions(
            {"any": [{"type": "price_below_vwap", "timeframe": "5m"}]}, 1.0
        ) is None

    def _profit_info(self):
        return {
            "status": "triggered",
            "exit_conditions": {
                "stop_loss": {"all": [
                    {"type": "percent_below_entry", "percent": 7.0},
                    {"any": [{"type": "price_below_vwap", "timeframe": "5m"}]},
                ]},
                "take_profit": [],
            },
        }

    def test_ratchet_tightens_from_high_watermark(self):
        info = self._profit_info()
        stop = apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.5)
        # hwm 1.5 -> candidate 1.38; hard stop 0.93 -> stop = 1.38
        assert stop == pytest.approx(1.38)
        assert info["trail_stop_price"] == pytest.approx(1.38)
        assert info["exit_conditions"]["stop_loss"] == {
            "any": [{"type": "price_below", "value": 1.38}]
        }

    def test_ratchet_never_loosens_on_price_drop(self):
        info = self._profit_info()
        apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.5)
        # Price drops: candidate would be 1.104 < 1.38 -> stop unchanged
        stop = apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.2)
        assert stop == pytest.approx(1.38)
        assert info["high_watermark"] == pytest.approx(1.5)

    def test_ratchet_moves_up_on_new_high(self):
        info = self._profit_info()
        apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.5)
        stop = apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=2.0)
        assert stop == pytest.approx(1.84)

    def test_hard_stop_is_floor_when_watermark_barely_above_entry(self):
        info = self._profit_info()
        # hwm 1.0 -> candidate 0.92 is LOOSER than the 7% hard stop (0.93)
        stop = apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.0)
        assert stop == pytest.approx(0.93)

    def test_llm_stop_rewrite_cannot_loosen_trailed_stop(self):
        """If the exit-update LLM rewrites stop_loss looser, the recorded
        trail_stop_price is re-applied as a floor on the next tick."""
        info = self._profit_info()
        apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.5)
        # LLM overwrites the stop with a much looser one
        info["exit_conditions"]["stop_loss"] = {
            "all": [{"type": "percent_below_entry", "percent": 10.0}]
        }
        stop = apply_trailing_ratchet(info, 8.0, entry_price=1.0, current_price=1.4)
        assert stop == pytest.approx(1.38)  # floor held

    def test_no_data_returns_none(self):
        assert apply_trailing_ratchet({}, 8.0) is None

    def test_settings_defaults(self):
        assert SETTINGS_DEFINITIONS["trail_after_max_holding"]["default"] is True
        assert SETTINGS_DEFINITIONS["trailing_stop_pct"]["default"] == 8.0


# ===========================================================================
# 2b. Phase 0: trail vs flat-close decision at max_holding_days
# ===========================================================================

def _aged_position(inst_id, symbol="SKYQ", entry_price=1.0, age_days=5):
    txn = create_transaction(
        symbol=symbol, quantity=100.0, expert_id=inst_id, open_price=entry_price,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    return {
        "transaction_id": txn.id, "symbol": symbol,
        "qty": 100, "entry_price": entry_price,
    }


class TestPhase0MaxHolding:
    _SETTINGS = {
        "max_holding_days": 3,
        "trail_after_max_holding": True,
        "trailing_stop_pct": 8.0,
    }

    def test_profitable_aged_position_trails_instead_of_flat_close(self):
        stub, inst = _make_stub(settings=dict(self._SETTINGS), live_prices={"SKYQ": 1.30})
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._trade_mgr.get_open_positions.return_value = [_aged_position(inst.id)]

        stub._phase_0_review(ma)

        stub._trade_mgr.execute_exit.assert_not_called()
        state = get_instance(MarketAnalysis, ma.id).state
        assert "SKYQ" in state.get("trailing", {})
        assert state["trailing"]["SKYQ"]["price_at_activation"] == 1.30

    def test_losing_aged_position_still_flat_closed(self):
        stub, inst = _make_stub(settings=dict(self._SETTINGS), live_prices={"SKYQ": 0.80})
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._trade_mgr.get_open_positions.return_value = [_aged_position(inst.id)]

        stub._phase_0_review(ma)

        stub._trade_mgr.execute_exit.assert_called_once()
        args, kwargs = stub._trade_mgr.execute_exit.call_args
        assert args[0] == "SKYQ"
        assert kwargs["exit_pct"] == 100.0
        assert "max holding" in kwargs["reason"]
        state = get_instance(MarketAnalysis, ma.id).state
        assert "SKYQ" not in state.get("trailing", {})

    def test_trail_disabled_keeps_flat_close_even_at_profit(self):
        settings = dict(self._SETTINGS, trail_after_max_holding=False)
        stub, inst = _make_stub(settings=settings, live_prices={"SKYQ": 1.30})
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._trade_mgr.get_open_positions.return_value = [_aged_position(inst.id)]

        stub._phase_0_review(ma)
        stub._trade_mgr.execute_exit.assert_called_once()

    def test_unverifiable_price_flat_closes(self):
        """No live price -> profit cannot be verified -> safe time stop."""
        stub, inst = _make_stub(settings=dict(self._SETTINGS), live_prices={})
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._trade_mgr.get_open_positions.return_value = [_aged_position(inst.id)]

        stub._phase_0_review(ma)
        stub._trade_mgr.execute_exit.assert_called_once()

    def test_young_position_untouched(self):
        stub, inst = _make_stub(settings=dict(self._SETTINGS), live_prices={"SKYQ": 1.30})
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._trade_mgr.get_open_positions.return_value = [
            _aged_position(inst.id, age_days=1)
        ]

        stub._phase_0_review(ma)
        stub._trade_mgr.execute_exit.assert_not_called()
        state = get_instance(MarketAnalysis, ma.id).state
        assert "SKYQ" not in state.get("trailing", {})

    def test_phase_5_applies_ratchet_each_tick(self):
        """Source check: the monitor loop wires the ratchet for trail_active positions."""
        import inspect
        from ba2_experts.PennyMomentumTrader.monitoring import MonitoringPhasesMixin
        source = inspect.getsource(MonitoringPhasesMixin._phase_5_monitor)
        assert "apply_trailing_ratchet" in source
        assert "update_high_watermark" in source
        assert "trail_active" in source


# ===========================================================================
# 3. Entry window default
# ===========================================================================

class TestEntryWindow:
    def test_max_entry_age_days_default_is_3(self):
        assert SETTINGS_DEFINITIONS["max_entry_age_days"]["default"] == 3

    def test_broker_cancelled_entry_rearms_to_watching(self):
        """Source check: a broker-cancelled day order resets the monitor to
        'watching' (clearing pending_order_id/triggered_at) so the signal can
        re-trigger cleanly within the entry window."""
        import inspect
        from ba2_experts.PennyMomentumTrader.monitoring import MonitoringPhasesMixin
        source = inspect.getsource(MonitoringPhasesMixin._phase_5_monitor)
        assert 'info["status"] = "watching"' in source
        assert 'info.pop("pending_order_id", None)' in source
        assert 'info.pop("triggered_at", None)' in source


# ===========================================================================
# 4. Reversal lane in the quick-filter prompt
# ===========================================================================

class TestReversalLane:
    def test_prompt_contains_reversal_lane(self):
        prompt = build_quick_filter_prompt([{"symbol": "CALC", "price": 1.0}], 15)
        assert '"reversal"' in prompt or "LANE \"reversal\"" in prompt
        assert "52-week low" in prompt
        assert "decliner" in prompt  # explicit anti-pattern callout

    def test_candidate_line_includes_52w_low_distance(self):
        candidates = [{
            "symbol": "CALC", "price": 1.15, "year_low": 1.00,
            "volume": 5_000_000, "market_cap": 50_000_000,
        }]
        prompt = build_quick_filter_prompt(candidates, 15)
        assert "52wLow=+15%" in prompt

    def test_candidate_line_omits_52w_low_when_absent(self):
        prompt = build_quick_filter_prompt([{"symbol": "NOYL", "price": 1.0}], 15)
        assert "52wLow" not in prompt.split("CANDIDATES:")[1].split("RESPOND")[0]

    def test_selected_schema_carries_lane_tag(self):
        prompt = build_quick_filter_prompt([{"symbol": "A", "price": 1.0}], 15)
        assert '"lane": "momentum" or "reversal"' in prompt


# ===========================================================================
# 5. Filter post-mortem
# ===========================================================================

class TestPostmortemStats:
    SCAN_PRICES = {"AAA": 1.00, "BBB": 2.00, "CCC": 4.00, "DDD": 1.00, "SPL": 1.00}
    CURRENT = {"AAA": 1.50, "BBB": 1.00, "CCC": 5.00, "SPL": 20.00}

    def test_forward_returns_exclude_splits_and_missing(self):
        returns = compute_forward_returns(
            ["AAA", "BBB", "DDD", "SPL"], self.SCAN_PRICES, self.CURRENT, {"SPL"}
        )
        assert returns == {"AAA": 50.0, "BBB": -50.0}  # DDD no quote, SPL excluded

    def test_stage_stats_computation(self):
        stages = {
            "scanned": ["AAA", "BBB", "CCC", "SPL"],
            "quick_filter_rejected": ["AAA"],
            "entered": ["BBB"],
            "expired": [],
        }
        stats, stage_returns = compute_stage_stats(
            stages, self.SCAN_PRICES, self.CURRENT, {"SPL"}
        )
        scanned = stats["scanned"]
        assert scanned["count"] == 3  # SPL excluded
        assert scanned["avg_fwd_ret"] == pytest.approx(round((50 - 50 + 25) / 3, 2))
        assert scanned["best"][0] == ("AAA", 50.0)
        assert scanned["worst"][0] == ("BBB", -50.0)
        assert stats["expired"] == {"count": 0, "avg_fwd_ret": None, "best": [], "worst": []}
        assert stage_returns["quick_filter_rejected"] == {"AAA": 50.0}

    def test_missed_winners_threshold(self):
        stage_returns = {
            "quick_filter_rejected": {"CALC": 74.0, "MEH": 5.0},
            "triage_rejected": {"LOW": -10.0},
            "expired": {"NNOX": 55.0, "CALC": 60.0},
            "entered": {"WIN": 99.0},  # entered symbols are never "missed"
        }
        winners = find_missed_winners(stage_returns)
        assert [w["symbol"] for w in winners] == ["CALC", "NNOX"]
        assert winners[0]["fwd_ret_pct"] == 74.0  # best stage return wins for dupes
        assert winners[0]["stage"] == "quick_filter_rejected"


class TestPostmortemPipeline:
    _SETTINGS = {"filter_postmortem_enabled": True, "eod_flat": False}

    def _seed_old_analysis(self, inst_id, days_ago=5):
        old_ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst_id,
            status=MarketAnalysisStatus.COMPLETED,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
            state={
                "filtered_stocks": {
                    "CALC": {"phase": "quick_filter", "reason": "llm_rejected",
                             "details": "decliner, no catalyst"},
                    "LOWC": {"phase": "deep_triage", "reason": "low_confidence",
                             "details": "conf 40"},
                },
                "deep_triage_results": {"NNOX": {"price": 8.00, "confidence": 70}},
                "executed_trades": [
                    {"symbol": "ENTR", "action": "entry", "reason": "catalyst"},
                ],
                "monitored_symbols": {
                    "NNOX": {"status": "expired"},
                    "ENTR": {"status": "triggered"},
                },
            },
        )
        add_instance(AnalysisOutput(
            market_analysis_id=old_ma.id, provider_category="screener",
            provider_name="fmp", name="scan_raw_screener_response", type="json",
            symbol="PENNY_SCAN",
            text=json.dumps([
                {"symbol": "CALC", "price": 1.00},
                {"symbol": "LOWC", "price": 2.00},
                {"symbol": "ENTR", "price": 1.00},
                {"symbol": "SPLT", "price": 1.00},
            ]),
        ))
        add_instance(AnalysisOutput(
            market_analysis_id=old_ma.id, provider_category="llm",
            provider_name="gpt", name="deep_triage_NNOX", type="json",
            symbol="NNOX", text="{}",
        ))
        return old_ma

    def _quotes(self):
        return {
            "CALC": {"symbol": "CALC", "price": 1.74},   # +74%
            "LOWC": {"symbol": "LOWC", "price": 1.80},   # -10%
            "ENTR": {"symbol": "ENTR", "price": 1.10},   # +10%
            "NNOX": {"symbol": "NNOX", "price": 12.40},  # +55%
            "SPLT": {"symbol": "SPLT", "price": 20.00},  # split — excluded
        }

    def test_postmortem_computes_and_persists(self):
        stub, inst = _make_stub(settings=dict(self._SETTINGS))
        old_ma = self._seed_old_analysis(inst.id)
        current_ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._fetch_quotes_chunked = MagicMock(return_value=self._quotes())
        stub._fetch_split_symbols = MagicMock(return_value={"SPLT"})

        payload = stub.run_filter_postmortem(current_ma)

        assert payload is not None
        assert payload["as_of_analysis_id"] == old_ma.id
        # Quotes fetched in batches of 100
        assert stub._fetch_quotes_chunked.call_args.kwargs.get("chunk_size") == 100

        stages = payload["stages"]
        assert stages["scanned"]["count"] == 3  # CALC, LOWC, ENTR (SPLT excluded)
        assert stages["quick_filter_rejected"]["best"][0] == ("CALC", 74.0)
        assert stages["triage_rejected"]["avg_fwd_ret"] == pytest.approx(-10.0)
        assert stages["entered"]["avg_fwd_ret"] == pytest.approx(10.0)
        assert stages["expired"]["best"][0] == ("NNOX", 55.0)
        assert payload["excluded_splits"] == ["SPLT"]

        missed = {w["symbol"]: w for w in payload["missed_winners"]}
        assert set(missed) == {"CALC", "NNOX"}

        # Persisted as an AnalysisOutput on the CURRENT analysis
        from sqlmodel import select
        from ba2_trade_platform.core.db import get_db
        with get_db() as session:
            out = session.exec(
                select(AnalysisOutput)
                .where(AnalysisOutput.market_analysis_id == current_ma.id)
                .where(AnalysisOutput.name == "filter_postmortem")
            ).first()
        assert out is not None
        assert out.type == "json"
        assert json.loads(out.text)["as_of_analysis_id"] == old_ma.id

        # Old analysis marked processed -> a second run finds nothing
        old_state = get_instance(MarketAnalysis, old_ma.id).state
        assert old_state["postmortem_processed"] == current_ma.id
        assert stub.run_filter_postmortem(current_ma) is None

    def test_postmortem_picks_oldest_unprocessed_in_window(self):
        stub, inst = _make_stub(settings=dict(self._SETTINGS))
        older = self._seed_old_analysis(inst.id, days_ago=7)
        newer = self._seed_old_analysis(inst.id, days_ago=5)
        target = stub._find_postmortem_target()
        assert target.id == older.id
        # Out-of-window analyses are ignored
        stub2, inst2 = _make_stub(settings=dict(self._SETTINGS))
        self._seed_old_analysis(inst2.id, days_ago=2)
        self._seed_old_analysis(inst2.id, days_ago=12)
        assert stub2._find_postmortem_target() is None

    def test_postmortem_failure_never_breaks_phase_6(self):
        stub, inst = _make_stub(settings=dict(self._SETTINGS))
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub.run_filter_postmortem = MagicMock(side_effect=RuntimeError("boom"))
        stub._trade_mgr.get_open_positions.return_value = []

        stub._phase_6_eod(ma)  # must not raise

        refreshed = get_instance(MarketAnalysis, ma.id)
        assert refreshed.status == MarketAnalysisStatus.COMPLETED

    def test_setting_default_enabled(self):
        assert SETTINGS_DEFINITIONS["filter_postmortem_enabled"]["default"] is True


# ===========================================================================
# 6. eod_flat
# ===========================================================================

class TestEodFlat:
    def test_eod_flat_closes_all_open_positions(self):
        stub, inst = _make_stub(
            settings={"eod_flat": True, "filter_postmortem_enabled": False},
        )
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING,
            state={"monitored_symbols": {
                "AZI": {"status": "triggered"},
                "BBB": {"status": "triggered"},
                "WCH": {"status": "watching"},
            }},
        )
        stub._trade_mgr.get_open_positions.return_value = [
            {"transaction_id": 1, "symbol": "AZI", "qty": 100, "entry_price": 1.0},
            {"transaction_id": 2, "symbol": "BBB", "qty": 50, "entry_price": 2.0},
        ]
        stub._trade_mgr.execute_exit.return_value = True

        stub._phase_6_eod(ma)

        assert stub._trade_mgr.execute_exit.call_count == 2
        closed = {call.args[0] for call in stub._trade_mgr.execute_exit.call_args_list}
        assert closed == {"AZI", "BBB"}
        for call in stub._trade_mgr.execute_exit.call_args_list:
            assert call.kwargs["exit_pct"] == 100.0
            assert "eod_flat" in call.kwargs["reason"]

        refreshed = get_instance(MarketAnalysis, ma.id)
        assert refreshed.status == MarketAnalysisStatus.COMPLETED
        monitored = refreshed.state["monitored_symbols"]
        assert monitored["AZI"]["status"] == "closed"
        assert monitored["BBB"]["status"] == "closed"
        assert monitored["WCH"]["status"] == "watching"  # watchers untouched

    def test_eod_flat_disabled_by_default_no_closes(self):
        stub, inst = _make_stub(
            settings={"eod_flat": False, "filter_postmortem_enabled": False},
        )
        ma = create_market_analysis(
            symbol="PENNY_SCAN", expert_instance_id=inst.id,
            status=MarketAnalysisStatus.RUNNING, state={},
        )
        stub._trade_mgr.get_open_positions.return_value = [
            {"transaction_id": 1, "symbol": "AZI", "qty": 100, "entry_price": 1.0},
        ]

        stub._phase_6_eod(ma)

        stub._trade_mgr.execute_exit.assert_not_called()
        assert get_instance(MarketAnalysis, ma.id).status == MarketAnalysisStatus.COMPLETED

    def test_setting_default_is_off(self):
        assert SETTINGS_DEFINITIONS["eod_flat"]["default"] is False
