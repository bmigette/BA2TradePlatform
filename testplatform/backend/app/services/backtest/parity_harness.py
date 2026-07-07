"""Golden live<->backtest parity harness (Phase 0 of
docs/plans/2026-07-02-live-backtest-engine-unification.md).

The problem it solves
---------------------
Live ``run_analysis``/``TradeManager`` and the backtest ``daily_engine`` both funnel every
entry decision through the SAME shared code (``TradeActionEvaluator`` + ``TradeConditions`` +
the enter ruleset). The unification plan asserts "same engine in live and backtest" — but
until now there was NO evidence channel proving the backtest reproduces a live decision on
identical inputs. This harness is that channel.

What it does
------------
It REPLAYS a window of RECORDED live ``ExpertRecommendation`` rows (captured hermetically by
``tools/capture_live_parity_fixture.py`` into a committed JSON fixture) through the backtest's
exact enter-decision path — the real ``TradeActionEvaluator.evaluate(...).execute(...)`` against
a flat ``BacktestAccount`` and the live expert's own enter ruleset (seeded faithfully, tiers in
order) — then compares the backtest decision to what the live engine actually did.

What it ASSERTS (logic parity — a failure here is a real engine bug)
    * POSITIVE: every rec the live engine FUNDED (produced a real order for) must, replayed
      through the backtest evaluator against a flat account, fire an order of the SAME SIDE.
      (Live funding implies the enter ruleset passed on that rec; on a flat account the same
      deterministic conditions on the same rec fields MUST pass in the backtest too.)
    * NEGATIVE: every HOLD rec must fire NOTHING (neither bullish nor bearish tier matches).

What it MEASURES (orchestration seam — reported, NOT asserted)
    * BUY recs the live engine did NOT fund: how many still fire in the backtest. A rec can
      pass the ruleset yet be skipped live by dedup / equity / capital-allocation — exactly the
      orchestration gaps the unification plan targets. Reporting (not asserting) this keeps the
      harness honest: it distinguishes shared-decision parity (asserted) from driver-loop
      divergence (measured).

Faithfulness notes
    * The rec is seeded with its RECORDED risk_level + time_horizon (not hardcoded), because the
      live ruleset tiers gate on lowrisk/mediumrisk + long_term/medium_term/short_term triggers —
      dropping them would let a tier spuriously (mis)match.
    * The enter ruleset's EventActions are seeded in link order (first-match-wins tiers).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# A fixed virtual clock date for the replay; the buy/sell fire decision does not depend on the
# calendar date (only on rec fields + flat-account position state), so one date suffices.
_CLOCK = datetime(2024, 1, 3)
_CFG = {"starting_cash": 1_000_000.0, "commission_per_trade": 0.0,
        "slippage_bps": 0.0, "fill_model": "next_bar_open"}

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "tests", "backtest", "fixtures")


@dataclass
class RecParity:
    rec_id: int
    symbol: str
    action: str
    confidence: Optional[float]
    expected_profit: Optional[float]
    funded_live: bool
    live_side: Optional[str]
    bt_fired: bool
    bt_side: Optional[str]
    verdict: str  # 'positive_match' | 'positive_MISMATCH' | 'negative_match' |
    #                'negative_MISMATCH' | 'measured'


@dataclass
class ParityReport:
    expert: str
    rows: List[RecParity] = field(default_factory=list)

    @property
    def positive(self) -> List[RecParity]:
        return [r for r in self.rows if r.funded_live]

    @property
    def negative(self) -> List[RecParity]:
        return [r for r in self.rows if r.action == "HOLD"]

    @property
    def measured(self) -> List[RecParity]:
        return [r for r in self.rows if r.verdict == "measured"]

    @property
    def positive_pass(self) -> int:
        return sum(1 for r in self.positive if r.verdict == "positive_match")

    @property
    def negative_pass(self) -> int:
        return sum(1 for r in self.negative if r.verdict == "negative_match")

    @property
    def mismatches(self) -> List[RecParity]:
        return [r for r in self.rows if r.verdict.endswith("MISMATCH")]

    @property
    def ok(self) -> bool:
        return (not self.mismatches
                and self.positive_pass == len(self.positive)
                and self.negative_pass == len(self.negative))

    def summary(self) -> str:
        seam_fired = sum(1 for r in self.measured if r.bt_fired)
        lines = [
            f"Parity report for expert={self.expert}: {len(self.rows)} recs",
            f"  POSITIVE (live-funded -> BT must fire same side): "
            f"{self.positive_pass}/{len(self.positive)} match",
            f"  NEGATIVE (HOLD -> BT must fire nothing): "
            f"{self.negative_pass}/{len(self.negative)} match",
            f"  MEASURED (BUY not funded live): {seam_fired}/{len(self.measured)} fire in BT "
            f"(attributable to the live orchestration seam: dedup/equity/allocation)",
        ]
        for m in self.mismatches:
            lines.append(f"  !! MISMATCH rec {m.rec_id} {m.symbol} {m.action}: "
                         f"live_side={m.live_side} funded={m.funded_live} -> "
                         f"bt_fired={m.bt_fired} bt_side={m.bt_side}")
        return "\n".join(lines)


# --- enum coercion (fixture stores enum VALUES as strings) ------------------
def _coerce(enum_cls, raw):
    if raw is None:
        return None
    if isinstance(raw, enum_cls):
        return raw
    s = str(raw)
    try:
        return enum_cls(s)
    except Exception:  # noqa: BLE001
        pass
    try:
        return enum_cls[s]
    except Exception:  # noqa: BLE001
        # e.g. "OrderRecommendation.BUY" -> "BUY"
        return enum_cls(s.split(".")[-1])


def _as_dict(v):
    return json.loads(v) if isinstance(v, str) else v


def _seed_enter_ruleset(fixture: Dict[str, Any]) -> int:
    """Recreate the live enter ruleset (tiers in order) in the current backtest DB; return id."""
    from ba2_common.core.db import add_instance, get_db
    from ba2_common.core.models import EventAction, Ruleset, RulesetEventActionLink
    from ba2_common.core.types import AnalysisUseCase, ExpertEventRuleType
    from sqlmodel import Session

    ruleset = Ruleset(
        name="parity-enter", description="live enter ruleset replay",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET)
    ruleset_id = add_instance(ruleset)

    ea_ids: List[int] = []
    for ea in fixture["enter_eventactions"]:
        row = EventAction(
            name=ea.get("name") or "parity-tier",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            subtype=AnalysisUseCase.ENTER_MARKET,
            triggers=_as_dict(ea["triggers"]),
            actions=_as_dict(ea["actions"]),
            extra_parameters=_as_dict(ea.get("extra_parameters")) or {},
            continue_processing=bool(ea.get("continue_processing")),
        )
        ea_ids.append(add_instance(row))

    with Session(get_db().bind) as session:
        for idx, ea_id in enumerate(ea_ids):
            session.add(RulesetEventActionLink(
                ruleset_id=ruleset_id, eventaction_id=ea_id, order_index=idx))
        session.commit()
    return ruleset_id


def _seed_rec(fixture_rec: Dict[str, Any], expert_id: int) -> int:
    """Seed one ExpertRecommendation faithfully (incl. risk_level + time_horizon)."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertRecommendation
    from ba2_common.core.types import OrderRecommendation, RiskLevel, TimeHorizon

    row = ExpertRecommendation(
        instance_id=expert_id,
        market_analysis_id=None,
        symbol=fixture_rec["symbol"],
        recommended_action=_coerce(OrderRecommendation, fixture_rec["recommended_action"]),
        expected_profit_percent=float(fixture_rec.get("expected_profit_percent") or 0.0),
        target_price=(None if fixture_rec.get("target_price") in (None, "None")
                      else float(fixture_rec["target_price"])),
        price_at_date=float(fixture_rec.get("price_at_date") or 0.0),
        details=fixture_rec.get("details") or "",
        confidence=(None if fixture_rec.get("confidence") is None
                    else float(fixture_rec["confidence"])),
        risk_level=_coerce(RiskLevel, fixture_rec.get("risk_level")) or RiskLevel.MEDIUM,
        time_horizon=(_coerce(TimeHorizon, fixture_rec.get("time_horizon"))
                      or TimeHorizon.MEDIUM_TERM),
        data=None,
        created_at=_CLOCK,
    )
    return add_instance(row)


def _bt_decision(symbol: str, rec_id: int, ruleset_id: int, account, price_source
                 ) -> tuple[bool, Optional[str]]:
    """Replay ONE rec through the backtest enter path; return (fired, side).

    Mirrors ``daily_engine._run_expert_bar`` exactly: TradeActionEvaluator.evaluate on the
    enter ruleset, then execute(submit_to_broker=False) for the equity entry, then read the
    produced order's side.
    """
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertRecommendation, TradingOrder

    # a bar so get_instrument_current_price(symbol) resolves (needed by sizing/target math)
    rec = get_instance(ExpertRecommendation, rec_id)
    px = float(rec.price_at_date or 100.0) or 100.0
    price_source.load_bars(symbol, [{"Date": _CLOCK, "Open": px, "High": px,
                                     "Low": px, "Close": px, "Volume": 1000}])
    price_source.set_clock(_CLOCK)

    evaluator = TradeActionEvaluator(account=account, instrument_name=symbol,
                                     existing_transactions=None)
    summaries = evaluator.evaluate(instrument_name=symbol, expert_recommendation=rec,
                                   ruleset_id=ruleset_id, existing_order=None)
    if not summaries or any("error" in s for s in summaries):
        return False, None
    results = evaluator.execute(submit_to_broker=False)
    order_ids = [(r.get("data") or {}).get("order_id") for r in results
                 if r.get("success") and (r.get("data") or {}).get("order_id")]
    if not order_ids:
        return False, None
    order = get_instance(TradingOrder, order_ids[0])
    side = getattr(order, "side", None)
    return True, (side.value if hasattr(side, "value") else str(side) if side else None)


def run_parity(fixture_path: str) -> ParityReport:
    """Replay a captured live window through the backtest enter path; return a parity report."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import activity_logging_disabled

    with open(fixture_path) as fh:
        fixture = json.load(fh)

    expert = str((fixture.get("instance") or {}).get("expert") or "unknown")
    recs = fixture["recommendations"]
    funded_rec_ids = {o["expert_recommendation_id"] for o in fixture["orders"]
                      if (o.get("quantity") or 0) > 0}
    live_side_by_rec: Dict[int, str] = {}
    for o in fixture["orders"]:
        if (o.get("quantity") or 0) > 0 and o["expert_recommendation_id"] not in live_side_by_rec:
            s = o.get("side")
            live_side_by_rec[o["expert_recommendation_id"]] = (
                s.split(".")[-1] if isinstance(s, str) else str(s))

    report = ParityReport(expert=expert)
    account_id = 900001
    expert_id = 900002

    wire = wire_backtest_seams()
    ctx = backtest_trading_db("parity-harness")
    ctx.__enter__()
    activity_ctx = activity_logging_disabled()  # silence per-action ActivityLog writes (no such
    activity_ctx.__enter__()                     # table in the backtest DB — matches the real engine)
    try:
        seed_account_definition(account_id, _CFG)
        price_source = AsOfPriceSource(ohlcv_provider=None)
        account = BacktestAccount(account_id, price_source, _CFG)
        wire.register_account(account_id, account)
        ruleset_id = _seed_enter_ruleset(fixture)

        for fr in recs:
            rec_id = _seed_rec(fr, expert_id)
            symbol = fr["symbol"]
            fired, bt_side = _bt_decision(symbol, rec_id, ruleset_id, account, price_source)

            action = (fr["recommended_action"].split(".")[-1]
                      if isinstance(fr["recommended_action"], str) else str(fr["recommended_action"]))
            funded = fr["id"] in funded_rec_ids
            live_side = live_side_by_rec.get(fr["id"])

            if funded:
                verdict = ("positive_match" if fired and bt_side == live_side
                           else "positive_MISMATCH")
            elif action == "HOLD":
                verdict = "negative_match" if not fired else "negative_MISMATCH"
            else:
                verdict = "measured"

            report.rows.append(RecParity(
                rec_id=fr["id"], symbol=symbol, action=action,
                confidence=fr.get("confidence"),
                expected_profit=fr.get("expected_profit_percent"),
                funded_live=funded, live_side=live_side,
                bt_fired=fired, bt_side=bt_side, verdict=verdict))
    finally:
        activity_ctx.__exit__(None, None, None)
        ctx.__exit__(None, None, None)

    return report


def default_fixture(instance: int = 13) -> str:
    return os.path.abspath(os.path.join(FIXTURE_DIR, f"live_parity_inst{instance}.json"))


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else default_fixture()
    rep = run_parity(path)
    print(rep.summary())
    print("PARITY OK" if rep.ok else "PARITY FAILED")
    sys.exit(0 if rep.ok else 1)
