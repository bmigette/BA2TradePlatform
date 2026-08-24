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


@dataclass(frozen=True)
class IVRankGate:
    """One enabled expert whose ruleset(s) contain at least one iv_rank-gated rule."""
    expert_id: int
    account_id: int
    expert: str
    rule_names: Tuple[str, ...]
    symbols: Tuple[str, ...]


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
        try:
            symbols = _expert_symbols(expert_id)
        except Exception as e:
            logger.error(
                f"Could not resolve the instrument universe of iv_rank-gated expert "
                f"{expert_id} ({expert}): {e}. Its ATM-IV series will not be recorded "
                f"and its gated rules stay inert.", exc_info=True)
            symbols = []
        gates.append(IVRankGate(
            expert_id=expert_id, account_id=account_id, expert=expert,
            rule_names=rule_names,
            symbols=tuple(s for s in symbols if s and s not in PLACEHOLDER_SYMBOLS),
        ))
    return gates


def recording_targets(gates: Optional[Sequence[IVRankGate]] = None) -> Dict[int, List[str]]:
    """``{account_id: [underlying, ...]}`` — exactly the series the recorder must keep.

    Sorted and deduped so the daily job's work (and its log line) is deterministic.
    """
    out: Dict[int, set] = {}
    for gate in (find_iv_rank_gates() if gates is None else gates):
        if gate.symbols:
            out.setdefault(gate.account_id, set()).update(gate.symbols)
    return {aid: sorted(syms) for aid, syms in out.items()}
