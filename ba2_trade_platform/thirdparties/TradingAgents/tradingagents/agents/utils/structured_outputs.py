"""
Pydantic schemas for structured agent outputs.

This module is the foundation for migrating the TradingAgents pipeline away
from brittle free-form-text parsing toward LLM tool-calling / JSON-mode
structured outputs (per upstream v0.2.4).

Adoption is incremental: an agent opts in by binding one of these schemas via
`llm.with_structured_output(schema)` and returning the parsed pydantic instance
into the state. Agents that have not migrated continue to return text — the
state types remain unchanged, so the rest of the graph is unaffected.

Currently used by:
- trader (TraderDecision)

Add new schemas here when migrating additional agents (bull/bear researcher,
research manager, risk manager, final decision).
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, conint, confloat


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------

class TraderDecision(BaseModel):
    """Structured decision returned by the Trader node.

    The Trader synthesizes the analysts' reports and the bull/bear judge's
    investment plan into a single actionable decision. The free-form
    `trader_investment_plan` text is preserved (for downstream agents that
    expect prose), but the structured fields below give the Risk team and
    the Final Summarization agent reliable handles.
    """

    action: Literal["BUY", "SELL", "HOLD", "UNDERWEIGHT", "OVERWEIGHT"] = Field(
        description="The trader's directional decision."
    )
    confidence: conint(ge=1, le=100) = Field(
        description="1-100 confidence in this decision. 50 = no edge, 70+ = strong edge."
    )
    expected_profit_pct: confloat(ge=-100.0, le=500.0) = Field(
        description="Realistic expected return %, signed (positive for upside, negative for downside)."
    )
    time_horizon_days: conint(ge=1, le=365) = Field(
        default=5,
        description="Expected holding period in days before the thesis plays out.",
    )
    entry_zone: Optional[str] = Field(
        default=None,
        description="Suggested entry price range as a short string, e.g. '$172-$175'. Optional.",
    )
    stop_loss_pct: Optional[confloat(ge=0.5, le=50.0)] = Field(
        default=None,
        description="Recommended stop-loss as % below entry (for longs) or above (for shorts).",
    )
    take_profit_pct: Optional[confloat(ge=0.5, le=500.0)] = Field(
        default=None,
        description="Recommended primary take-profit as % from entry.",
    )
    key_catalysts: List[str] = Field(
        default_factory=list,
        description="2-5 bullet catalysts justifying the direction (earnings, news, technicals).",
    )
    key_risks: List[str] = Field(
        default_factory=list,
        description="2-5 bullet risks that would invalidate the thesis.",
    )
    reasoning: str = Field(
        description="2-4 sentence synthesis explaining the decision."
    )


# ---------------------------------------------------------------------------
# Helper: render structured output back into the free-form prose the rest of
# the graph still expects. Lets us flip individual agents to structured output
# without breaking downstream nodes that read the text field.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Researchers (bull / bear)
# ---------------------------------------------------------------------------

class ResearcherArgument(BaseModel):
    """Structured output for a bull or bear researcher in the investment debate.

    The researchers are debaters; their prose argument is the primary output and
    is what the opponent and the judge will read. Structured fields add a short
    headline that the judge can consume directly.
    """

    stance: Literal["BULL", "BEAR"] = Field(
        description="This researcher's stance — BULL for upside thesis, BEAR for downside."
    )
    conviction: conint(ge=1, le=100) = Field(
        description="1-100 conviction in the stance. 50 = balanced/no edge, 80+ = strongly held."
    )
    headline: str = Field(
        description="One-sentence thesis summary (e.g. 'Strong earnings + AI tailwind outweighs valuation risk')."
    )
    key_points: List[str] = Field(
        description="3-5 bullet points making the case.",
    )
    rebuttals: List[str] = Field(
        default_factory=list,
        description="Direct rebuttals to the opponent's last argument (empty for the opening round).",
    )
    full_argument: str = Field(
        description=(
            "Full conversational argument in prose, as the analyst would speak it. "
            "This is what gets appended to debate history and shown to the opponent and judge. "
            "Several paragraphs of natural language — do NOT compress into bullet points here."
        )
    )


# ---------------------------------------------------------------------------
# Investment judge (research_manager) — ends the bull/bear debate
# ---------------------------------------------------------------------------

class InvestmentJudgeVerdict(BaseModel):
    """The Investment Judge's ruling on the bull vs. bear debate.

    Produces the `investment_plan` that the Trader then acts on, plus structured
    fields the downstream Risk team and Final Summarization can consume directly.
    """

    chosen_side: Literal["BULL", "BEAR", "MIXED"] = Field(
        description="Which side carried the debate. MIXED only when the evidence is genuinely balanced."
    )
    action: Literal["BUY", "SELL", "HOLD", "UNDERWEIGHT", "OVERWEIGHT"] = Field(
        description="Directional verdict the trader should pursue."
    )
    confidence: conint(ge=1, le=100) = Field(
        description="1-100 confidence in the verdict."
    )
    target_horizon_days: conint(ge=1, le=365) = Field(
        default=5,
        description="Expected holding period in days before the thesis resolves.",
    )
    bull_strongest_point: Optional[str] = Field(
        default=None,
        description="The single strongest bull argument considered.",
    )
    bear_strongest_point: Optional[str] = Field(
        default=None,
        description="The single strongest bear argument considered.",
    )
    why_chosen_side_won: str = Field(
        description="2-4 sentence explanation of why the chosen side's case is stronger."
    )
    full_investment_plan: str = Field(
        description=(
            "Full investment plan in prose, including thesis, suggested entry/exit framing, "
            "and what could invalidate the thesis. This text is what the Trader reads — "
            "it must be self-contained natural language."
        )
    )


# ---------------------------------------------------------------------------
# Risk judge (risk_manager) — final ruling after the risk-debate phase
# ---------------------------------------------------------------------------

class RiskJudgeVerdict(BaseModel):
    """The Risk Judge's final trade decision, integrating the 3 risk debaters."""

    final_action: Literal["BUY", "SELL", "HOLD", "UNDERWEIGHT", "OVERWEIGHT"] = Field(
        description="Final directional verdict for execution."
    )
    confidence: conint(ge=1, le=100) = Field(
        description="1-100 confidence in the final action."
    )
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        description="Overall risk classification for the suggested trade."
    )
    position_sizing_pct: Optional[confloat(ge=0.0, le=100.0)] = Field(
        default=None,
        description="Suggested position sizing as % of available risk budget (optional).",
    )
    stop_loss_pct: Optional[confloat(ge=0.5, le=50.0)] = Field(
        default=None,
        description="Recommended stop loss % from entry.",
    )
    take_profit_pct: Optional[confloat(ge=0.5, le=500.0)] = Field(
        default=None,
        description="Recommended take profit % from entry.",
    )
    accepted_arguments: List[str] = Field(
        default_factory=list,
        description="Specific arguments from the debaters that the judge accepted.",
    )
    rejected_arguments: List[str] = Field(
        default_factory=list,
        description="Specific arguments the judge rejected (and why, briefly).",
    )
    rationale: str = Field(
        description="2-4 sentence executive summary explaining the verdict."
    )
    full_text: str = Field(
        description=(
            "Full final-decision text the Final Summarization agent will read. "
            "Should include the executive summary, action, sizing/SL/TP guidance, "
            "and the trade thesis in prose."
        )
    )


# ---------------------------------------------------------------------------
# Renderers — translate structured output back into the prose fields the rest
# of the graph still reads. This lets each migrated agent populate BOTH the
# structured key and the existing text key, so unmigrated downstream nodes
# (and the DB-persistence layer) keep working unchanged.
# ---------------------------------------------------------------------------

def render_trader_decision(decision: TraderDecision) -> str:
    """Render a TraderDecision into a markdown summary for state['trader_investment_plan']."""
    lines = [
        f"**Action:** {decision.action}",
        f"**Confidence:** {decision.confidence}/100",
        f"**Expected return:** {decision.expected_profit_pct:+.1f}% over ~{decision.time_horizon_days} days",
    ]
    if decision.entry_zone:
        lines.append(f"**Entry zone:** {decision.entry_zone}")
    if decision.stop_loss_pct is not None:
        lines.append(f"**Stop loss:** {decision.stop_loss_pct:.1f}%")
    if decision.take_profit_pct is not None:
        lines.append(f"**Take profit:** {decision.take_profit_pct:.1f}%")
    if decision.key_catalysts:
        lines.append("\n**Key catalysts:**")
        lines.extend(f"- {c}" for c in decision.key_catalysts)
    if decision.key_risks:
        lines.append("\n**Key risks:**")
        lines.extend(f"- {r}" for r in decision.key_risks)
    lines.append("")
    lines.append(decision.reasoning)
    return "\n".join(lines)


def render_researcher_argument(arg: ResearcherArgument) -> str:
    """Render a ResearcherArgument back into the conversational form expected
    by the opponent and the judge. The `full_argument` field carries the prose;
    the structured headline/points are appended only when the prose is short."""
    if arg.full_argument and len(arg.full_argument) > 200:
        return arg.full_argument
    # Short prose → augment with structured points so the judge has substance to chew on
    parts = [arg.full_argument or ""]
    if arg.headline:
        parts.append(f"\n\n**Thesis:** {arg.headline}")
    if arg.key_points:
        parts.append("**Key points:**")
        parts.extend(f"- {p}" for p in arg.key_points)
    if arg.rebuttals:
        parts.append("**Rebuttals:**")
        parts.extend(f"- {r}" for r in arg.rebuttals)
    return "\n".join(parts)


def render_investment_judge_verdict(v: InvestmentJudgeVerdict) -> str:
    """Render the investment-judge verdict into the prose investment_plan text."""
    lines = [
        f"**Verdict:** {v.action} (chose {v.chosen_side})",
        f"**Confidence:** {v.confidence}/100",
        f"**Horizon:** ~{v.target_horizon_days} days",
        "",
    ]
    if v.bull_strongest_point:
        lines.append(f"**Strongest bull point:** {v.bull_strongest_point}")
    if v.bear_strongest_point:
        lines.append(f"**Strongest bear point:** {v.bear_strongest_point}")
    lines.append("")
    lines.append(f"**Why {v.chosen_side} wins:** {v.why_chosen_side_won}")
    lines.append("")
    lines.append(v.full_investment_plan)
    return "\n".join(lines)


def render_risk_judge_verdict(v: RiskJudgeVerdict) -> str:
    """Render the risk-judge verdict into the final_trade_decision text."""
    lines = [
        f"**Final Action:** {v.final_action}",
        f"**Confidence:** {v.confidence}/100",
        f"**Risk Level:** {v.risk_level}",
    ]
    if v.position_sizing_pct is not None:
        lines.append(f"**Position sizing:** {v.position_sizing_pct:.1f}% of risk budget")
    if v.stop_loss_pct is not None:
        lines.append(f"**Stop loss:** {v.stop_loss_pct:.1f}%")
    if v.take_profit_pct is not None:
        lines.append(f"**Take profit:** {v.take_profit_pct:.1f}%")
    if v.accepted_arguments:
        lines.append("\n**Accepted arguments:**")
        lines.extend(f"- {a}" for a in v.accepted_arguments)
    if v.rejected_arguments:
        lines.append("\n**Rejected arguments:**")
        lines.extend(f"- {a}" for a in v.rejected_arguments)
    lines.append("")
    lines.append(f"**Rationale:** {v.rationale}")
    lines.append("")
    lines.append(v.full_text)
    return "\n".join(lines)
