"""
SQL-backed memory for TradingAgents.

Replaces the prior ChromaDB / embedding-based FinancialSituationMemory with a
direct lookup against BA2's MarketAnalysis / ExpertRecommendation / Transaction
tables. The interface (class name + method signatures) is preserved so existing
agents and the Reflector keep working without changes.

Why this design (vs. embeddings):
- Our DB already contains structured past decisions AND their realized outcomes.
- "Same expert + same symbol, recent first" is strictly more useful to the LLM
  than similarity-matched paragraphs from arbitrary past situations.
- No embedding API costs, no chunking, no Chroma corruption issues, deterministic.

What's stored:
- add_situations(): persists a role-tagged reflection paragraph onto the
  current MarketAnalysis.state under state["reflections"][<role>]. No new tables.
- get_memories(): pulls the last N completed analyses for the same
  expert+symbol, joins with closed Transactions to attach realized P&L, and
  formats a prompt-friendly text block. Also appends a short cross-ticker
  recent-outcomes section (last 30 days, same expert).
"""

from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone

from ba2_trade_platform.logger import logger


class FinancialSituationMemory:
    """SQL-backed drop-in replacement for the prior ChromaDB-based memory.

    Constructor signature is preserved for compatibility with TradingAgentsGraph.
    Only `name`, `symbol`, `market_analysis_id`, and `expert_instance_id` are
    actually used; `config` is accepted but ignored.
    """

    def __init__(
        self,
        name: str,
        config: Optional[Dict[str, Any]] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        expert_instance_id: Optional[int] = None,
    ):
        self.name = name  # e.g. "bull_memory", "trader_memory"
        self.symbol = symbol.upper() if symbol else None
        self.market_analysis_id = market_analysis_id
        self.expert_instance_id = expert_instance_id

    # ------------------------------------------------------------------
    # Reflection persistence
    # ------------------------------------------------------------------
    def add_situations(self, situations_and_advice):
        """Persist reflection text onto the current MarketAnalysis state.

        Compatible signature: `situations_and_advice` is a list of
        (situation, reflection_text) tuples. Only the reflection text is kept —
        the situation itself is already implicit in the MarketAnalysis row.
        """
        if not situations_and_advice or self.market_analysis_id is None:
            return

        from ba2_trade_platform.core.models import MarketAnalysis
        from ba2_trade_platform.core.db import get_instance, update_instance

        try:
            ma = get_instance(MarketAnalysis, self.market_analysis_id)
            if ma is None:
                logger.warning(
                    f"FinancialSituationMemory.add_situations: "
                    f"MarketAnalysis {self.market_analysis_id} not found"
                )
                return

            state = dict(ma.state or {})
            reflections = dict(state.get("reflections") or {})
            # Concatenate all reflection texts for this role
            texts = [text for (_situation, text) in situations_and_advice if text]
            if not texts:
                return
            reflections[self.name] = "\n\n".join(texts)
            state["reflections"] = reflections
            ma.state = state
            update_instance(ma)
        except Exception as e:
            logger.warning(
                f"FinancialSituationMemory.add_situations failed for "
                f"MA={self.market_analysis_id} role={self.name}: {e}"
            )

    # ------------------------------------------------------------------
    # Memory retrieval
    # ------------------------------------------------------------------
    def get_memories(self, current_situation, n_matches=2, aggregate_chunks=False):
        """Return the last N completed analyses for this expert+symbol, with
        realized outcomes and reflection text formatted for prompt injection.

        Args:
            current_situation: Ignored (kept for signature compatibility — was
                used by the prior embedding-based implementation).
            n_matches: Max number of past analyses to return (per same-symbol).
            aggregate_chunks: Ignored (compatibility).

        Returns:
            List of dicts each containing a 'recommendation' key (the
            formatted text block the agents concatenate into their prompts).
            Returns an empty list if no expert context is available.
        """
        if self.expert_instance_id is None or self.symbol is None:
            return []

        try:
            past_blocks = self._fetch_same_symbol_blocks(n_matches)
            cross_block = self._fetch_cross_ticker_summary()
        except Exception as e:
            logger.warning(
                f"FinancialSituationMemory.get_memories failed for "
                f"expert={self.expert_instance_id} symbol={self.symbol}: {e}",
                exc_info=True,
            )
            return []

        if not past_blocks and not cross_block:
            return []

        # Surface results as one entry per past same-symbol analysis, with the
        # cross-ticker summary appended to the first entry so the LLM always
        # sees it. Falls back to a single entry containing only the cross-ticker
        # block when there's no same-symbol history.
        if not past_blocks:
            return [{"recommendation": cross_block, "matched_situation": "", "similarity_score": 1.0}]

        if cross_block:
            past_blocks[0] = past_blocks[0] + "\n\n" + cross_block

        return [
            {"recommendation": block, "matched_situation": "", "similarity_score": 1.0}
            for block in past_blocks
        ]

    # ------------------------------------------------------------------
    # Internal queries
    # ------------------------------------------------------------------
    def _fetch_same_symbol_blocks(self, n_matches: int) -> List[str]:
        """Format the most recent N completed analyses for this expert+symbol."""
        from sqlmodel import select, Session
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import (
            MarketAnalysis,
            ExpertRecommendation,
            Transaction,
        )
        from ba2_trade_platform.core.types import MarketAnalysisStatus

        blocks: List[str] = []
        with Session(get_db().bind) as session:
            # Pull recent completed analyses (excluding the current one)
            stmt = (
                select(MarketAnalysis)
                .where(MarketAnalysis.expert_instance_id == self.expert_instance_id)
                .where(MarketAnalysis.symbol == self.symbol)
                .where(MarketAnalysis.status == MarketAnalysisStatus.COMPLETED)
            )
            if self.market_analysis_id is not None:
                stmt = stmt.where(MarketAnalysis.id != self.market_analysis_id)
            stmt = stmt.order_by(MarketAnalysis.created_at.desc()).limit(n_matches)

            past_mas = list(session.exec(stmt).all())

            for ma in past_mas:
                # Recommendation associated with this analysis (one expected)
                rec = session.exec(
                    select(ExpertRecommendation)
                    .where(ExpertRecommendation.market_analysis_id == ma.id)
                    .order_by(ExpertRecommendation.created_at.desc())
                    .limit(1)
                ).first()

                # Realized outcome from any closed Transaction tied to this rec
                outcome = None
                if rec is not None:
                    outcome = self._lookup_realized_outcome(session, rec.id)

                blocks.append(self._format_past_analysis(ma, rec, outcome))

        return blocks

    def _fetch_cross_ticker_summary(self) -> str:
        """Recent closed trades from the same expert across other tickers."""
        from sqlmodel import select, Session
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import Transaction
        from ba2_trade_platform.core.types import TransactionStatus, OrderDirection

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        with Session(get_db().bind) as session:
            stmt = (
                select(Transaction)
                .where(Transaction.expert_id == self.expert_instance_id)
                .where(Transaction.symbol != self.symbol)
                .where(Transaction.status == TransactionStatus.CLOSED)
                .where(Transaction.close_date >= cutoff)
                .where(Transaction.open_price.is_not(None))
                .where(Transaction.close_price.is_not(None))
                .where(Transaction.quantity > 0)
                .order_by(Transaction.close_date.desc())
                .limit(20)
            )
            txns = list(session.exec(stmt).all())

        if not txns:
            return ""

        wins: List[str] = []
        losses: List[str] = []
        for t in txns:
            try:
                if t.side == OrderDirection.BUY:
                    pnl_pct = (t.close_price - t.open_price) / t.open_price * 100
                else:
                    pnl_pct = (t.open_price - t.close_price) / t.open_price * 100
            except (TypeError, ZeroDivisionError):
                continue
            entry = f"{t.symbol} {pnl_pct:+.1f}% ({t.side.value if hasattr(t.side, 'value') else t.side})"
            (wins if pnl_pct >= 0 else losses).append(entry)

        wins_sorted = sorted(wins, key=lambda s: -float(s.split()[1].rstrip('%')))[:5]
        losses_sorted = sorted(losses, key=lambda s: float(s.split()[1].rstrip('%')))[:5]

        lines = ["=== Recent cross-ticker outcomes (this expert, last 30 days) ==="]
        if wins_sorted:
            lines.append("Wins:   " + ", ".join(wins_sorted))
        if losses_sorted:
            lines.append("Losses: " + ", ".join(losses_sorted))
        return "\n".join(lines) if (wins_sorted or losses_sorted) else ""

    @staticmethod
    def _lookup_realized_outcome(session, recommendation_id: int):
        """Find a closed Transaction tied (via TradingOrder) to this rec."""
        from sqlmodel import select
        from ba2_trade_platform.core.models import Transaction, TradingOrder
        from ba2_trade_platform.core.types import TransactionStatus, OrderDirection

        order = session.exec(
            select(TradingOrder)
            .where(TradingOrder.expert_recommendation_id == recommendation_id)
            .order_by(TradingOrder.id)
            .limit(1)
        ).first()
        if order is None or order.transaction_id is None:
            return None

        txn = session.get(Transaction, order.transaction_id)
        if txn is None or txn.status != TransactionStatus.CLOSED:
            return None
        if not (txn.open_price and txn.close_price and txn.quantity):
            return None

        try:
            if txn.side == OrderDirection.BUY:
                pnl_pct = (txn.close_price - txn.open_price) / txn.open_price * 100
            else:
                pnl_pct = (txn.open_price - txn.close_price) / txn.open_price * 100
        except (TypeError, ZeroDivisionError):
            return None

        days_held = None
        if txn.open_date and txn.close_date:
            days_held = (txn.close_date - txn.open_date).days

        return {
            "pnl_pct": pnl_pct,
            "days_held": days_held,
            "close_reason": txn.close_reason,
        }

    def _format_past_analysis(self, ma, rec, outcome) -> str:
        """Render one past analysis into a prompt-friendly text block."""
        date_str = ma.created_at.strftime("%Y-%m-%d") if ma.created_at else "?"

        # Header line with action + confidence
        if rec is not None:
            action = rec.recommended_action.value if hasattr(rec.recommended_action, "value") else str(rec.recommended_action)
            conf = f"{rec.confidence:.0f}" if rec.confidence is not None else "?"
            expected = (
                f"{rec.expected_profit_percent:+.1f}%"
                if rec.expected_profit_percent is not None
                else "?"
            )
            header = f"[{date_str}] {action} (confidence {conf}, expected {expected})"
        else:
            header = f"[{date_str}] (no recommendation recorded)"

        lines = [header]

        # Compact summary from the trading_agent_graph state
        summary = self._extract_summary(ma)
        if summary:
            lines.append(f"Summary: {summary}")

        # Realized outcome
        if outcome is not None:
            outcome_marker = "✓" if outcome["pnl_pct"] >= 0 else "✗"
            held = (
                f" over {outcome['days_held']}d"
                if outcome["days_held"] is not None
                else ""
            )
            reason = f", closed via {outcome['close_reason']}" if outcome.get("close_reason") else ""
            lines.append(
                f"Result: {outcome['pnl_pct']:+.1f}%{held}{reason} {outcome_marker}"
            )
        elif rec is not None:
            lines.append("Result: no position opened or still open.")

        # Past reflection for this role (if previously saved by add_situations)
        reflection = self._extract_reflection(ma)
        if reflection:
            lines.append(f"Reflection ({self.name}): {reflection}")

        # Rec details if no realized outcome (gives the LLM the reasoning to learn from)
        if outcome is None and rec is not None and rec.details:
            details = rec.details.strip().replace("\n", " ")
            if len(details) > 400:
                details = details[:400] + "…"
            lines.append(f"Rationale: {details}")

        return "\n".join(lines)

    def _extract_summary(self, ma) -> str:
        """Extract the compact summary dict from MarketAnalysis state."""
        state = ma.state or {}
        tag = state.get("trading_agent_graph") or {}
        summary = tag.get("final_analysis_summary")
        if isinstance(summary, dict) and summary:
            return ", ".join(f"{k}={v}" for k, v in summary.items())
        return ""

    def _extract_reflection(self, ma) -> str:
        """Pull the stored reflection paragraph for THIS role from past MA state."""
        state = ma.state or {}
        reflections = state.get("reflections") or {}
        text = reflections.get(self.name)
        if not text:
            return ""
        # Single-line for the prompt
        return text.strip().replace("\n", " ")
