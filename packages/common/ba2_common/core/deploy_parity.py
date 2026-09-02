"""THE ONE TABLE of settings a backtest FORCES or DERIVES onto a trial's expert.

The operator principle is that a GA gene may live only in the emitted ruleset or the expert
settings, because those two artefacts are what a deploy carries from a scored ``Backtest`` row to
a live ``ExpertInstance``. The 2026-09-02 final review (§2a, V1-V5) found the *other* direction
open: the backtest handler forces four trading permissions and derives several run-level
behaviours that no deploy payload carried, so a deployed genome ran under different settings from
the one that was scored. The worst of them, ``allow_automated_trade_modification``, defaults
False on ``MarketExpertInterface`` and gates EVERY open-positions action in
``TradeManager`` — a deployed genome evaluated its exits and never submitted them.

**Why a table and not four assignments.** The gap existed because the forcing lived in one
function (``daily_backtest_handler._build_experts``) and the carrying lived in another
(``app.api.backtests._derive_export_payload``), with nothing joining them. Both now read
``BACKTEST_FORCED_SETTINGS``, so a new forced setting cannot be added to one without the other:
adding a row makes the exporter carry it, and dropping a row makes the deploy round-trip pin
(``tests/backtest/test_deploy_round_trip_parity.py``) fail.

**Two kinds of row.**

* ``live_setting`` set -- the value has a live ``ExpertInstance`` setting of the same meaning, so
  the payload carries it and ``tools/import_deploy_payload.py`` applies it through the expert's
  own ``save_settings``. The deployed instance ends up with what the engine had.
* ``live_setting is None`` -- the behaviour has NO live analogue. It is still carried, under
  ``backtest_only`` in the payload, so a deploy is explicitly different rather than silently
  different, and the round-trip pin allowlists it against ``why_no_live_analogue``. Closing one
  of these needs a change to live broker/engine code and is an operator decision, not a
  deploy-tooling one.

Lives in ``ba2_common`` because three different trees read it: the testplatform handler, the
testplatform export API, and ``tools/import_deploy_payload.py`` (which runs against the LIVE
platform checkout and has only the packages on its path).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = [
    "BacktestRunFacts",
    "ForcedSetting",
    "BACKTEST_FORCED_SETTINGS",
    "forced_expert_settings",
    "backtest_only_settings",
    "live_settings_from_universe",
    "SCREENER_UNIVERSE_SETTING",
]


@dataclass(frozen=True)
class BacktestRunFacts:
    """The run-level facts every forced/derived setting is a function of.

    Deliberately explicit rather than a raw config dict: the handler reads these off the trial
    config and the exporter off the persisted ``optimization_config['backtest']`` block, and the
    two dicts spell some of them differently. Naming the facts is what makes those two reads
    provably the same read.
    """

    #: The run's short-side permission. Seeds the symmetric SELL entry rule AND the RM's sell gate.
    enable_short: bool
    #: BacktestAccount setting: keep stock delivered by a short-option assignment instead of
    #: liquidating it at the next bar's open. A per-strategy LAUNCHER decision (the wheel).
    hold_assigned_stock: bool
    #: Pure-option ENTRY template. Truthy means the engine submits option entries directly
    #: instead of staging an RM candidate.
    entry_action: Optional[Any]


@dataclass(frozen=True)
class ForcedSetting:
    """One row of the table."""

    #: The name the BACKTEST knows it by.
    key: str
    #: The live ``ExpertInstance`` setting that carries the same meaning, or ``None`` when the
    #: behaviour has no live analogue.
    live_setting: Optional[str]
    #: Where the backtest sets it, for anyone reading a diff.
    source: str
    #: Why the backtest forces/derives it.
    why: str
    #: For a ``live_setting is None`` row: what closing the gap would take. The deploy round-trip
    #: pin quotes this, so an un-carried behaviour cannot be forgotten, only accepted.
    why_no_live_analogue: str = ""

    def value(self, facts: BacktestRunFacts) -> Any:
        return _VALUES[self.key](facts)


#: key -> how the backtest derives it. Separate from the rows so a row stays a plain description.
_VALUES = {
    "allow_automated_trade_opening": lambda f: True,
    "enable_buy": lambda f: True,
    "allow_automated_trade_modification": lambda f: True,
    "enable_sell": lambda f: bool(f.enable_short),
    "hold_assigned_stock": lambda f: bool(f.hold_assigned_stock),
    "entry_action": lambda f: f.entry_action,
}


BACKTEST_FORCED_SETTINGS = (
    ForcedSetting(
        key="allow_automated_trade_opening",
        live_setting="allow_automated_trade_opening",
        source="daily_backtest_handler._build_experts (gate_settings)",
        why="a backtest simulates AUTOMATED trading; the interface defaults this False and the "
            "RM drops every order without it.",
    ),
    ForcedSetting(
        key="enable_buy",
        live_setting="enable_buy",
        source="daily_backtest_handler._build_experts (gate_settings)",
        why="the RM filters BUY candidates on it; a backtest always allows the long side.",
    ),
    ForcedSetting(
        key="allow_automated_trade_modification",
        live_setting="allow_automated_trade_modification",
        source="daily_backtest_handler._build_experts (gate_settings)",
        why="THE HIGHEST-SEVERITY ROW (review V3). TradeManager gates every open-positions "
            "action -- adjust TP/SL and CLOSE -- on this flag, and the interface defaults it "
            "False. A deploy that does not carry it evaluates its exits and creates them "
            "pending, never submitted: the sleeve runs live with no automated exits at all.",
    ),
    ForcedSetting(
        key="enable_sell",
        live_setting="enable_sell",
        source="daily_backtest_handler._build_experts (gate_settings), from enable_short",
        why="the RM gates SHORT entries on enable_sell; the backtest derives it from the run's "
            "enable_short so the two cannot disagree.",
    ),
    ForcedSetting(
        key="hold_assigned_stock",
        live_setting=None,
        source="ba2test_launcher._hold_assigned_stock -> account_settings; consumed by "
               "BacktestAccount at assignment reconciliation",
        why="review V5. True only for the wheel family; every other option key SELLS stock "
            "delivered by assignment, so backtested P&L assumes a de-risking action.",
        why_no_live_analogue=
            "live never sells assigned stock -- AlpacaAccount.reconcile_option_assignments opens "
            "the equity long and stops. Closing this is a change to a live BROKER class, which "
            "deploy tooling must not make; it is an operator decision.",
    ),
    ForcedSetting(
        key="entry_action",
        live_setting=None,
        source="ba2test_launcher -> backtest_cfg['entry_action'] -> daily_engine._entry_is_option",
        why="review V4. A run-global flag derived from it makes the engine submit option entries "
            "DIRECTLY instead of staging an RM candidate.",
        why_no_live_analogue=
            "live has no such key: TradeManager stages EVERY enter-market recommendation as an RM "
            "candidate. The asymmetry is in the live engine, not in the payload, so carrying it "
            "as an applied setting would be a lie. Recorded so a deploy states it.",
    ),
)

#: The live setting that says WHERE an instance's universe comes from. A screener-universe run
#: exports its screener block; without this the imported instance keeps its static list and the
#: screener settings sit there inert.
SCREENER_UNIVERSE_SETTING = "instrument_selection_method"


def forced_expert_settings(facts: BacktestRunFacts) -> Dict[str, Any]:
    """{live setting: value} for every row that HAS a live analogue.

    Applied last by the handler (so a payload override cannot turn a gate off) and merged last
    into the export payload's ``expert_params`` for the same reason.
    """
    return {row.live_setting: row.value(facts)
            for row in BACKTEST_FORCED_SETTINGS if row.live_setting is not None}


def backtest_only_settings(facts: BacktestRunFacts) -> Dict[str, Any]:
    """{backtest key: value} for every row with NO live analogue -- carried, never applied."""
    return {row.key: row.value(facts)
            for row in BACKTEST_FORCED_SETTINGS if row.live_setting is None}


def live_settings_from_universe(universe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The expert settings a payload's ``universe`` block implies (review V1 + V2).

    ``_derive_export_payload`` has always BUILT this block; ``import_deploy_payload`` consumed
    only ``settings.expert_params`` and dropped it, which is the common root of V1 (the six
    ``screener:*`` genes) and V2 (the $100 underlying-price cap the option grids screen on). The
    live settings already exist under the SAME names on ``MarketExpertInterface``, so the whole
    repair is this mapping plus the selection method that switches them on.

    A static-universe payload maps to nothing: its symbols are the run's candidate list, not a
    setting, and overwriting a live instance's ``enabled_instruments`` from a backtest's universe
    is a different (and much bigger) decision than carrying the screener config.
    """
    if not isinstance(universe, dict) or universe.get("mode") != "screener":
        return {}
    settings = dict(universe.get("screener_settings") or {})
    settings[SCREENER_UNIVERSE_SETTING] = "screener"
    return settings
