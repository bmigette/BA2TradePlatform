"""Which expert rules are gated on ``iv_rank``, and which underlyings therefore need
a trailing ATM-IV series.

``IVRankCondition`` fails closed: with no stored history it logs "IV rank unavailable"
and returns False. That is the correct safety direction, but it also means an
iv_rank-gated rule is not merely *less likely* to fire — it can NEVER fire. On the live
book that made seven option experts completely inert (their only non-guard rules are
iv_rank-gated) without a single error in the log.

This module is the shared source of truth for two jobs that must agree:

* the daily recorder decides what to sample from :func:`recording_targets`;
* the startup report reads :func:`find_iv_rank_gates` so that "these rules were dead
  and are now waking up" is an announced state change rather than a surprise order.

Deriving both from one scan is the point. If the recorder sampled some other universe
(say every enabled instrument on every options account), a rule could stay silently
inert because its symbol happened to fall outside the recorder's list.

TWO KINDS OF UNIVERSE. An expert either names its instruments in settings (``static``)
or resolves them at analysis time (``screener``/``dynamic``/``expert``, for which
``get_enabled_instruments`` returns a SENTINEL rather than tickers). Only handling the
first kind is a two-thirds fix: on the live book four of the seven iv_rank-gated experts
are screener-driven and carry six of the nine gated rules. A deferred universe is
therefore recovered from what the expert actually analysed recently — see
``_recent_analysis_symbols``.

AND WHEN IT CANNOT BE RECOVERED, THAT IS A REPORTED FACT, NOT A ZERO. Every gate carries
``universe_source``; a gate whose universe is UNKNOWN is a blind spot the readiness
report has to shout about. The failure this guards against is not a crash — it is a
readiness report calmly printing ``0/0 ARMED`` for an expert whose universe it could not
see, a line that is literally true and reads as success.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ba2_common.logger import logger
from ba2_common.core.types import ExpertEventType

#: The ``event_type`` string a trigger carries when it is an IV-rank gate ("iv_rank").
IV_RANK_EVENT_TYPE = ExpertEventType.N_IV_RANK.value

#: Sentinels ``get_enabled_instruments`` returns for non-static instrument selection.
#: They name a SELECTION MODE, not a ticker, so they can never be sampled — asking a
#: broker for the option chain of "DYNAMIC" is a guaranteed daily error.
PLACEHOLDER_SYMBOLS = frozenset({"EXPERT", "DYNAMIC", "SCREENER", "OPEN_POSITIONS"})

#: How far back to look for the symbols a DEFERRED-universe expert actually analysed.
#:
#: 30 days, and the number is load-bearing. ``SmartRiskManagerToolkit`` answers the same
#: "is this symbol in the screener expert's universe?" question with a 24-HOUR window,
#: but it is authorising a single order moments after a screener run. The recorder runs
#: on its own cron against every gated expert, and the live screener experts run WEEKLY
#: (verified on the live book: experts 26/29/31/33 each emit ~15 symbols every 7 days).
#: A 24h window would therefore see nothing on six days in seven, so the series would be
#: sampled in bursts — precisely the non-uniform grid ``record_atm_iv``'s daily guard
#: exists to prevent. 30 days spans four runs of the slowest cadence actually configured.
#:
#: Ageing a name out is cheap and reversible: snapshot rows are never deleted, so a
#: symbol the screener re-selects next month still has its old series waiting, and a gap
#: of a few samples in a ~252-sample percentile changes nothing. Never recording a name
#: at all, by contrast, makes its rule permanently inert — which is the bug this window
#: exists to fix. The asymmetry is why the window errs wide.
DEFERRED_UNIVERSE_LOOKBACK_DAYS = 30

#: ``IVRankGate.universe_source`` values — WHERE a gate's symbol list came from, so the
#: readiness report can tell an authoritative list from a trailing observation from a
#: blind spot. Collapsing the last two into "no symbols" is what let the report print
#: ``0/0 ARMED`` for an expert whose universe it could not see at all.
UNIVERSE_CONFIGURED = "configured"
UNIVERSE_RECENT_ANALYSES = "recent-analyses"
UNIVERSE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class IVRankGate:
    """One enabled expert whose ruleset(s) contain at least one iv_rank-gated rule."""
    expert_id: int
    account_id: int
    expert: str
    rule_names: Tuple[str, ...]
    symbols: Tuple[str, ...]
    #: One of the ``UNIVERSE_*`` constants above.
    universe_source: str = UNIVERSE_CONFIGURED
    #: The selection-mode sentinels this expert reported ("SCREENER", ...), for the log.
    deferred_modes: Tuple[str, ...] = ()

    @property
    def universe_is_known(self) -> bool:
        """False when nobody can say what this expert will ask about.

        An UNKNOWN universe is not "zero underlyings". It means the recorder cannot
        record and the report must not imply otherwise — the gated rules stay inert and
        an operator has to be told, not left reading a well-formed count of nothing.
        """
        return self.universe_source != UNIVERSE_UNKNOWN


def triggers_gate_on_iv_rank(triggers: Optional[Dict[str, Any]]) -> bool:
    """True when any ``trigger_N`` in an EventAction's trigger blob is an iv_rank test."""
    if not isinstance(triggers, dict):
        return False
    for spec in triggers.values():
        if isinstance(spec, dict) and spec.get("event_type") == IV_RANK_EVENT_TYPE:
            return True
    return False


def _expert_symbols(expert_id: int) -> Sequence[str]:
    """The expert's configured universe, via the live instance resolver.

    Split out as a module-level function so it is a single monkeypatchable seam: the
    resolver reaches into the live expert registry, which tests do not have.
    """
    from ba2_common.core.instance_resolver import get_instance_resolver
    expert = get_instance_resolver().get_expert_instance(expert_id)
    if expert is None:
        return []
    return expert.get_enabled_instruments() or []


def _recent_analysis_symbols(expert_id: int, lookback_days: int) -> List[str]:
    """Symbols this expert actually ran an analysis on in the trailing window.

    The recovery path for a DEFERRED universe (the SCREENER/DYNAMIC/EXPERT sentinels).
    The alternative — re-running the screener from the recorder's cron — is wrong twice:
    the FMP screener call is deliberately uncached, so it doubles the daily bill, and the
    universe it returns at 16:30 is not the one the morning analysis pass used, so it
    would sample names no rule asks about while missing names that do.

    Every status counts, not just COMPLETED. ``SmartRiskManagerToolkit`` filters to
    COMPLETED because it is AUTHORISING an order and wants proof of finished work; this
    is PRE-WARMING a series and wants the widest honest superset. A symbol whose analysis
    was skipped today is one the screener hands back tomorrow, and the cost of guessing
    wrong is asymmetric: one wasted chain fetch versus a permanently inert rule.

    A module-level function so it is a single monkeypatchable seam, matching
    ``_expert_symbols``.
    """
    from datetime import datetime, timedelta, timezone
    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import MarketAnalysis

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    with get_db() as session:
        rows = session.exec(
            select(MarketAnalysis.symbol).where(
                MarketAnalysis.expert_instance_id == expert_id,
                MarketAnalysis.created_at >= cutoff,
            ).distinct()
        ).all()
    return [r for r in rows if r]


def _iv_rank_rule_names(session, ruleset_ids: Sequence[int]) -> List[str]:
    """Names of the iv_rank-gated EventActions linked to any of `ruleset_ids`."""
    from sqlmodel import select
    from ba2_common.core.models import EventAction, RulesetEventActionLink

    ids = [r for r in ruleset_ids if r is not None]
    if not ids:
        return []
    rows = session.exec(
        select(EventAction, RulesetEventActionLink.order_index)
        .join(RulesetEventActionLink,
              RulesetEventActionLink.eventaction_id == EventAction.id)
        .where(RulesetEventActionLink.ruleset_id.in_(ids))
        .order_by(RulesetEventActionLink.order_index, EventAction.id)
    ).all()
    return [ea.name for ea, _idx in rows if triggers_gate_on_iv_rank(ea.triggers)]


def find_iv_rank_gates() -> List[IVRankGate]:
    """Every enabled expert instance with at least one iv_rank-gated rule.

    Disabled experts are skipped: they cannot trade, so sampling IV for their universe
    is a daily option-chain request per symbol bought for nothing.
    """
    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import ExpertInstance

    gates: List[IVRankGate] = []
    with get_db() as session:
        instances = session.exec(
            select(ExpertInstance).where(ExpertInstance.enabled == True)  # noqa: E712
            .order_by(ExpertInstance.id)
        ).all()
        gated = []
        for inst in instances:
            names = _iv_rank_rule_names(
                session, [inst.enter_market_ruleset_id, inst.open_positions_ruleset_id])
            if names:
                gated.append((inst.id, inst.account_id, inst.expert, tuple(names)))

    # Symbol resolution instantiates experts, so it happens OUTSIDE the DB session.
    for expert_id, account_id, expert, rule_names in gated:
        gates.append(_resolve_gate(expert_id, account_id, expert, rule_names))
    return gates


def _tickers(symbols: Sequence[str]) -> List[str]:
    """Drop selection-mode sentinels and blanks; whatever is left is a real ticker."""
    return [s for s in symbols if s and s not in PLACEHOLDER_SYMBOLS]


def _resolve_gate(expert_id: int, account_id: int, expert: str,
                  rule_names: Tuple[str, ...]) -> IVRankGate:
    """Build one gate, resolving a DEFERRED universe from the analysis history.

    Three outcomes, and keeping them distinct is the whole point:

    * ``configured``   — a static list. Authoritative; empty means misconfigured.
    * ``recent-analyses`` — the expert picks its symbols at analysis time and we
      recovered what it actually looked at. Best-effort and it moves.
    * ``unknown``      — a deferred universe with no analysis history yet, or a resolver
      that raised. NOTHING can be recorded and the gated rules stay inert. This is the
      state that used to be indistinguishable from "an expert with zero symbols", which
      is how the readiness report came to print ``0/0 ARMED`` — a line that is true,
      reads as success, and appears exactly when the recorder is blind.
    """
    try:
        raw = list(_expert_symbols(expert_id))
    except Exception as e:
        logger.error(
            f"Could not resolve the instrument universe of iv_rank-gated expert "
            f"{expert_id} ({expert}): {e}. Its ATM-IV series will not be recorded "
            f"and its gated rules stay inert.", exc_info=True)
        return IVRankGate(expert_id=expert_id, account_id=account_id, expert=expert,
                          rule_names=rule_names, symbols=(),
                          universe_source=UNIVERSE_UNKNOWN)

    symbols = _tickers(raw)
    deferred = tuple(sorted({s for s in raw if s in PLACEHOLDER_SYMBOLS}))
    if not deferred:
        return IVRankGate(expert_id=expert_id, account_id=account_id, expert=expert,
                          rule_names=rule_names, symbols=tuple(sorted(set(symbols))),
                          universe_source=UNIVERSE_CONFIGURED)

    try:
        recovered = _tickers(_recent_analysis_symbols(
            expert_id, DEFERRED_UNIVERSE_LOOKBACK_DAYS))
    except Exception as e:
        logger.error(
            f"Could not read the recent-analysis history of iv_rank-gated expert "
            f"{expert_id} ({expert}): {e}. Its {'/'.join(deferred)} universe stays "
            f"invisible and its gated rules stay inert.", exc_info=True)
        recovered = []

    merged = tuple(sorted(set(symbols) | set(recovered)))
    return IVRankGate(
        expert_id=expert_id, account_id=account_id, expert=expert,
        rule_names=rule_names, symbols=merged, deferred_modes=deferred,
        # A recovered symbol proves the history is readable and the expert is running.
        # Nothing recovered means the deferred part of the universe is a blind spot,
        # even if a static remainder happens to be known.
        universe_source=UNIVERSE_RECENT_ANALYSES if recovered else UNIVERSE_UNKNOWN,
    )


def recording_targets(gates: Optional[Sequence[IVRankGate]] = None) -> Dict[int, List[str]]:
    """``{account_id: [underlying, ...]}`` — exactly the series the recorder must keep.

    Sorted and deduped so the daily job's work (and its log line) is deterministic.

    A gate with an UNKNOWN universe contributes nothing here — there is nothing to
    contribute. That silence is safe ONLY because ``report_iv_rank_readiness`` and
    ``record_daily_iv_snapshots`` both warn about such gates by name; an empty result
    from this function must never be read as "nothing to do".
    """
    out: Dict[int, set] = {}
    for gate in (find_iv_rank_gates() if gates is None else gates):
        if gate.symbols:
            out.setdefault(gate.account_id, set()).update(gate.symbols)
    return {aid: sorted(syms) for aid, syms in out.items()}
