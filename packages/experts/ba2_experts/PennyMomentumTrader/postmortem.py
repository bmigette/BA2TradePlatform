"""
Automated filter post-mortem for PennyMomentumTrader (phase 6 EOD wrap-up).

Each EOD, the analysis run from ~5 trading days ago is re-examined: forward
returns vs the scan price are computed for every symbol per funnel stage
(scanned, quick-filter rejected, triage rejected, triaged, entered, expired),
split-affected symbols are excluded, and the result is persisted as a
``filter_postmortem`` AnalysisOutput on the CURRENT analysis. Rejected/expired
symbols that ran >= +25% are surfaced as ``missed_winners`` — the feedback loop
the June-2026 post-mortem lacked (CALC +74% was quick-filter rejected; NNOX
+55% / RTB +44% / GALT +39% expired unfilled).

Everything here is fail-soft: the caller wraps the entry point in try/except
and any partial data simply shrinks the report — the scan pipeline is never
broken by this feature.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.models import AnalysisOutput, MarketAnalysis
from ba2_common.core.db import get_db
from ba2_common.core.types import AnalysisUseCase, MarketAnalysisStatus


# Rejected/expired symbols above this forward return are reported as missed winners.
MISSED_WINNER_THRESHOLD_PCT = 25.0

# Funnel stages whose symbols count as "rejected/expired" for missed_winners.
_REJECTED_STAGES = ("quick_filter_rejected", "triage_rejected", "expired")


# ---------------------------------------------------------------------------
# Pure computation helpers (unit-testable, no platform dependencies)
# ---------------------------------------------------------------------------

def compute_forward_returns(
    symbols: List[str],
    scan_prices: Dict[str, float],
    current_prices: Dict[str, float],
    excluded: set,
) -> Dict[str, float]:
    """Per-symbol forward return (%) vs scan price.

    Symbols in ``excluded`` (split-affected — their price basis changed) or
    without both a positive scan price and a positive current price are
    omitted. No fallback prices, ever.
    """
    returns: Dict[str, float] = {}
    for sym in symbols:
        if sym in excluded:
            continue
        scan_p = scan_prices.get(sym)
        cur_p = current_prices.get(sym)
        if not scan_p or scan_p <= 0 or not cur_p or cur_p <= 0:
            continue
        returns[sym] = round((cur_p / scan_p - 1) * 100.0, 2)
    return returns


def compute_stage_stats(
    stage_symbols: Dict[str, List[str]],
    scan_prices: Dict[str, float],
    current_prices: Dict[str, float],
    excluded: set,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, float]]]:
    """Per-stage forward-return stats.

    Returns (stats, stage_returns) where stats maps
    ``stage -> {count, avg_fwd_ret, best: [(sym, ret)...top5], worst: [...bottom5]}``
    (count = symbols with a computable return) and stage_returns keeps the raw
    per-symbol returns for downstream use (missed winners).
    """
    stats: Dict[str, Any] = {}
    stage_returns: Dict[str, Dict[str, float]] = {}
    for stage, symbols in stage_symbols.items():
        returns = compute_forward_returns(
            symbols, scan_prices, current_prices, excluded
        )
        stage_returns[stage] = returns
        if returns:
            ordered = sorted(returns.items(), key=lambda kv: kv[1], reverse=True)
            stats[stage] = {
                "count": len(returns),
                "avg_fwd_ret": round(sum(returns.values()) / len(returns), 2),
                "best": ordered[:5],
                "worst": ordered[-5:][::-1],
            }
        else:
            stats[stage] = {"count": 0, "avg_fwd_ret": None, "best": [], "worst": []}
    return stats, stage_returns


def find_missed_winners(
    stage_returns: Dict[str, Dict[str, float]],
    threshold_pct: float = MISSED_WINNER_THRESHOLD_PCT,
) -> List[Dict[str, Any]]:
    """Rejected/expired symbols whose forward return reached ``threshold_pct``."""
    winners: Dict[str, Dict[str, Any]] = {}
    for stage in _REJECTED_STAGES:
        for sym, ret in (stage_returns.get(stage) or {}).items():
            if ret >= threshold_pct:
                prev = winners.get(sym)
                if prev is None or ret > prev["fwd_ret_pct"]:
                    winners[sym] = {"symbol": sym, "fwd_ret_pct": ret, "stage": stage}
    return sorted(winners.values(), key=lambda w: w["fwd_ret_pct"], reverse=True)


# ---------------------------------------------------------------------------
# Mixin (pipeline integration)
# ---------------------------------------------------------------------------

class PostmortemMixin:
    def _find_postmortem_target(self) -> Optional[MarketAnalysis]:
        """Oldest unprocessed COMPLETED ENTER_MARKET analysis from 4-8 days ago."""
        # Naive UTC to match how created_at is stored in SQLite
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with get_db() as session:
            from sqlmodel import select as sql_select
            statement = (
                sql_select(MarketAnalysis)
                .where(MarketAnalysis.expert_instance_id == self.instance.id)
                .where(MarketAnalysis.status == MarketAnalysisStatus.COMPLETED)
                .where(MarketAnalysis.subtype == AnalysisUseCase.ENTER_MARKET)
                .where(MarketAnalysis.created_at >= now - timedelta(days=8))
                .where(MarketAnalysis.created_at <= now - timedelta(days=4))
                .order_by(MarketAnalysis.created_at.asc())
            )
            for ma in session.exec(statement).all():
                if ma.state and ma.state.get("postmortem_processed"):
                    continue
                session.expunge(ma)
                return ma
        return None

    def _collect_postmortem_stages(
        self, target: MarketAnalysis
    ) -> Tuple[Dict[str, List[str]], Dict[str, float]]:
        """Build {stage: [symbols]} and {symbol: scan_price} from a past analysis."""
        state = target.state or {}

        # Scan candidates (post-filter screener rows, with scan-time prices)
        scan_candidates: List[Dict[str, Any]] = []
        with get_db() as session:
            from sqlmodel import select as sql_select
            outputs = session.exec(
                sql_select(AnalysisOutput)
                .where(AnalysisOutput.market_analysis_id == target.id)
            ).all()
        triaged_from_outputs: set = set()
        for out in outputs:
            if out.name == "scan_raw_screener_response" and out.text:
                try:
                    parsed = json.loads(out.text)
                    if isinstance(parsed, list):
                        scan_candidates = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            elif out.name and out.name.startswith("deep_triage_"):
                triaged_from_outputs.add(out.name[len("deep_triage_"):].upper())

        scan_prices: Dict[str, float] = {}
        scanned_symbols: List[str] = []
        for c in scan_candidates:
            sym = (c.get("symbol") or "").upper() if isinstance(c, dict) else ""
            if not sym:
                continue
            scanned_symbols.append(sym)
            try:
                price = float(c.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                scan_prices[sym] = price

        # Deep-triage results carry prices for discovered symbols not in the scan list
        deep_triage_results = state.get("deep_triage_results") or {}
        for sym, result in deep_triage_results.items():
            sym_u = sym.upper()
            if sym_u not in scan_prices and isinstance(result, dict):
                try:
                    price = float(result.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    scan_prices[sym_u] = price

        filtered_stocks = state.get("filtered_stocks") or {}
        quick_filter_rejected = [
            s.upper() for s, e in filtered_stocks.items()
            if isinstance(e, dict) and e.get("phase") == "quick_filter"
        ]
        triage_rejected = [
            s.upper() for s, e in filtered_stocks.items()
            if isinstance(e, dict) and e.get("phase") == "deep_triage"
        ]
        triaged = sorted(
            {s.upper() for s in deep_triage_results} | triaged_from_outputs
        )
        entered = sorted({
            (t.get("symbol") or "").upper()
            for t in state.get("executed_trades") or []
            if isinstance(t, dict) and t.get("action") == "entry" and t.get("symbol")
        })
        expired = [
            s.upper() for s, i in (state.get("monitored_symbols") or {}).items()
            if isinstance(i, dict) and i.get("status") == "expired"
        ]

        stages = {
            "scanned": scanned_symbols,
            "quick_filter_rejected": quick_filter_rejected,
            "triage_rejected": triage_rejected,
            "triaged": triaged,
            "entered": entered,
            "expired": expired,
        }
        return stages, scan_prices

    def run_filter_postmortem(self, market_analysis: MarketAnalysis) -> Optional[Dict[str, Any]]:
        """Compute + persist the filter post-mortem for the oldest eligible run.

        Called from phase 6. The caller wraps this in try/except (fail-soft);
        this method additionally returns None quietly when there is nothing to
        process or no usable data.
        """
        target = self._find_postmortem_target()
        if target is None:
            self.logger.debug("Filter post-mortem: no unprocessed analysis 4-8 days old")
            return None

        stages, scan_prices = self._collect_postmortem_stages(target)
        all_symbols = sorted({s for syms in stages.values() for s in syms})
        if not all_symbols:
            self.logger.info(
                f"Filter post-mortem: analysis #{target.id} has no symbols — marking processed"
            )
            self._mark_postmortem_processed(target.id, market_analysis.id)
            return None

        # Current quotes (FMP /v3/quote/, 100 symbols per call)
        quotes = self._fetch_quotes_chunked(all_symbols, chunk_size=100)
        current_prices: Dict[str, float] = {}
        for sym, q in quotes.items():
            price = q.get("price")
            if price and price > 0:
                current_prices[sym.upper()] = float(price)

        # Exclude symbols with a split in the window (price basis changed)
        target_date = (
            target.created_at or datetime.now(timezone.utc).replace(tzinfo=None)
        ).date()
        split_symbols = self._fetch_split_symbols(
            target_date.isoformat(),
            datetime.now(timezone.utc).date().isoformat(),
        )

        stats, stage_returns = compute_stage_stats(
            stages, scan_prices, current_prices, split_symbols
        )
        missed_winners = find_missed_winners(stage_returns)

        payload = {
            "as_of_analysis_id": target.id,
            "as_of_created_at": target.created_at.isoformat() if target.created_at else None,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "stages": stats,
            "missed_winners": missed_winners,
            "excluded_splits": sorted(split_symbols & set(all_symbols)),
        }

        self._save_analysis_output(
            market_analysis,
            provider_category="analytics",
            provider_name="filter_postmortem",
            name="filter_postmortem",
            output_type="json",
            text=json.dumps(payload, default=str),
            symbol="PENNY_SCAN",
        )
        self._mark_postmortem_processed(target.id, market_analysis.id)

        summary_parts = []
        for stage in ("scanned", "quick_filter_rejected", "triage_rejected", "triaged", "entered", "expired"):
            st = stats.get(stage) or {}
            if st.get("count"):
                summary_parts.append(f"{stage} n={st['count']} avg {st['avg_fwd_ret']:+.1f}%")
        winners_str = (
            ", ".join(f"{w['symbol']} {w['fwd_ret_pct']:+.0f}%" for w in missed_winners[:5])
            or "none"
        )
        description = (
            f"PennyMomentumTrader filter post-mortem vs analysis #{target.id} "
            f"({target_date}): {'; '.join(summary_parts) or 'no computable returns'} "
            f"| missed winners (>= +{MISSED_WINNER_THRESHOLD_PCT:.0f}%): {winners_str}"
        )
        self.logger.info(description)
        try:
            from ba2_common.core.db import log_activity
            from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
            log_activity(
                severity=ActivityLogSeverity.INFO,
                activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                description=description,
                data={
                    "as_of_analysis_id": target.id,
                    "missed_winners": missed_winners,
                },
                source_expert_id=self.instance.id,
            )
        except Exception:
            pass

        return payload

    def _mark_postmortem_processed(self, target_id: int, current_analysis_id: int):
        """Stamp the processed marker on the past analysis (idempotency)."""
        from sqlalchemy.orm import attributes
        with get_db() as session:
            ma = session.get(MarketAnalysis, target_id)
            if ma:
                state = ma.state or {}
                state["postmortem_processed"] = current_analysis_id
                ma.state = state
                attributes.flag_modified(ma, "state")
                session.add(ma)
                session.commit()
