"""Portfolio allocation persistence: every read and write of the five allocation tables.

Pure DB code -- it never talks to a broker and never touches NiceGUI. What it
borrows from the allocation ENGINE (``ba2_common.core.portfolio_allocation``) is
deliberately tiny: the two ``VALUATION_MODE_*`` constants, so that the page, the
store and the engine cannot disagree on the spelling of a mode;
``even_split_pct``, so that the default weights this module hands the page are
bit-for-bit the ones the engine would compute; and ``consume_income_events``, so
that the FIFO rule exists exactly once. The UI calls these helpers; the engine
receives the plain values they produce. The dependency only ever runs store ->
engine: the engine stays IO-free and must never import this module.

Two rules the callers depend on:

* A ``portfolio_allocation_label`` row's EXISTENCE is the "this label is managed"
  flag -- deleting the row unmanages the label.
* ``portfolio_allocation_symbol`` rows are created LAZILY. A symbol with no row
  takes the even-split default, so ``get_symbol_weights()`` returns a computed
  weight for every symbol you ask about and never an empty dict.
"""
from __future__ import annotations

from datetime import date as Date, datetime as DateTime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import (
    PortfolioAllocationConfig,
    PortfolioAllocationLabel,
    PortfolioAllocationRun,
    PortfolioAllocationSymbol,
    PortfolioIncomeEvent,
)
from ba2_common.core.portfolio_allocation import (
    VALUATION_MODE_COST,
    VALUATION_MODE_MARKET,
    consume_income_events,
    even_split_pct,
)
from ba2_common.logger import logger


# ---------------------------------------------------------------------------
# Managed labels
# ---------------------------------------------------------------------------

def get_managed_labels(account_id: int) -> List[PortfolioAllocationLabel]:
    """Every managed label of an account, in display order (sort_order, then name).

    Returns ``[]`` when the account manages nothing -- a legitimate empty state
    (nothing configured yet), not an error.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationLabel)
            .where(PortfolioAllocationLabel.account_id == account_id)
            .order_by(PortfolioAllocationLabel.sort_order, PortfolioAllocationLabel.label)
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows


def set_managed_label(account_id: int, label: str, *,
                      target_pct: Optional[float] = None,
                      sort_order: Optional[int] = None,
                      comment: Optional[str] = None) -> PortfolioAllocationLabel:
    """Create the managed-label row, or update only the fields you pass.

    ``None`` for a field means LEAVE IT UNCHANGED, so the page can save a comment
    without disturbing the percentage. Pass ``""`` to clear a comment.

    Raises:
        ValueError: when ``label`` is blank -- a nameless managed label is
        unreachable from the UI and would collide with the next blank one.
    """
    label = (label or "").strip()
    if not label:
        raise ValueError("set_managed_label requires a non-empty label")
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationLabel).where(
                PortfolioAllocationLabel.account_id == account_id,
                PortfolioAllocationLabel.label == label,
            )
        ).first()
        if row is None:
            row = PortfolioAllocationLabel(account_id=account_id, label=label)
            session.add(row)
        if target_pct is not None:
            row.target_pct = float(target_pct)
        if sort_order is not None:
            row.sort_order = int(sort_order)
        if comment is not None:
            row.comment = comment
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def remove_managed_label(account_id: int, label: str) -> bool:
    """Unmanage a label: delete its row AND every symbol-weight row underneath it.

    Returns True when a label row was deleted, False when the label was not
    managed in the first place.
    """
    label = (label or "").strip()
    if not label:
        return False
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationLabel).where(
                PortfolioAllocationLabel.account_id == account_id,
                PortfolioAllocationLabel.label == label,
            )
        ).first()
        symbol_rows = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
            )
        ).all()
        found = row is not None
        removed_symbols = len(symbol_rows)
        for symbol_row in symbol_rows:
            session.delete(symbol_row)
        if row is not None:
            session.delete(row)
        session.commit()
    if not found:
        return False
    logger.info(f"Unmanaged allocation label '{label}' for account {account_id} "
                f"({removed_symbols} symbol weight row(s) removed)")
    return True


# ---------------------------------------------------------------------------
# Symbol weights (created lazily -- absence means "even-split default")
# ---------------------------------------------------------------------------

def _split_evenly(total_pct: float, count: int) -> List[float]:
    """Split ``total_pct`` across ``count`` slots, remainder on the LAST slot.

    ``_split_evenly(100.0, 3) == [33.33, 33.33, 33.34]``, which sums to exactly
    100.0 -- a naive ``3 x 33.33`` totals 99.99 and the engine's
    ``validate_symbol_weights`` (0.01pp tolerance) rejects it. Returns ``[]`` for
    ``count <= 0`` (an empty label gets nothing, not a ZeroDivisionError).

    The split itself is NOT re-derived here: it is the engine's ``even_split_pct``,
    scaled down to ``total_pct`` exactly the way ``build_symbol_targets`` scales a
    leftover (4dp). Sharing the one function is what makes it impossible for the
    defaults shown on the page to drift from the ones the engine computes.
    """
    parts = even_split_pct(count)
    if not parts:
        return []
    return [round(total_pct * pct / 100.0, 4) for pct in parts]


def _normalise_symbols(symbols) -> List[str]:
    """Uppercase, strip, drop blanks and de-duplicate, PRESERVING the given order."""
    out: List[str] = []
    seen = set()
    for raw in symbols or []:
        symbol = (raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def get_symbol_rows(account_id: int, label: str) -> Dict[str, PortfolioAllocationSymbol]:
    """The STORED weight rows of one label, keyed by symbol.

    Only symbols the user has actually edited have a row, so this is normally a
    subset of the label's symbols. Use ``get_symbol_weights()`` when you need a
    weight for every symbol.
    """
    label = (label or "").strip()
    if not label:
        return {}
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
            )
        ).all()
        rows = list(rows)
        session.expunge_all()
        return {row.symbol: row for row in rows}


def get_symbol_weights(account_id: int, label: str, symbols) -> Dict[str, float]:
    """``{symbol: weight_pct}`` for every symbol of a label, defaults filled in.

    Weights are 1-100 WITHIN the label. Rows are lazy, so a symbol with no row is
    not an error: the un-stored symbols share whatever is left of 100% evenly
    (all of it when nothing is stored), with the remainder on the last one.
    Symbols are normalised (.strip().upper()), duplicates collapse, and the order
    of ``symbols`` is preserved in the returned dict.

    Unlike ``get_symbol_rows()``, this never returns an empty dict for a label you
    passed symbols for -- ``{}`` here means you asked about no symbols at all.
    """
    syms = _normalise_symbols(symbols)
    if not syms:
        return {}
    stored_rows = get_symbol_rows(account_id, label)
    stored = {s: float(stored_rows[s].weight_pct) for s in syms if s in stored_rows}
    unstored = [s for s in syms if s not in stored]
    remaining = max(0.0, 100.0 - sum(stored.values()))
    filled = dict(zip(unstored, _split_evenly(remaining, len(unstored))))
    return {s: stored[s] if s in stored else filled[s] for s in syms}


def set_symbol_weight(account_id: int, label: str, symbol: str, *,
                      weight_pct: Optional[float] = None,
                      comment: Optional[str] = None) -> PortfolioAllocationSymbol:
    """Create or update ONE symbol's weight/comment inside a label.

    ``None`` for a field leaves it unchanged; pass ``""`` to clear a comment.
    Writing a row makes the weight explicit -- the symbol stops taking the
    even-split default, which is exactly what the user asked for by editing it.

    Raises:
        ValueError: when ``label`` or ``symbol`` is blank.
    """
    label = (label or "").strip()
    symbol = (symbol or "").strip().upper()
    if not label or not symbol:
        raise ValueError("set_symbol_weight requires a non-empty label and symbol")
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
                PortfolioAllocationSymbol.symbol == symbol,
            )
        ).first()
        if row is None:
            row = PortfolioAllocationSymbol(account_id=account_id, label=label, symbol=symbol)
            session.add(row)
        if weight_pct is not None:
            row.weight_pct = float(weight_pct)
        if comment is not None:
            row.comment = comment
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def remove_symbol_weight(account_id: int, label: str, symbol: str) -> bool:
    """Drop a symbol's stored weight so it returns to the even-split default.

    Returns True when a row was deleted, False when the symbol had none.
    """
    label = (label or "").strip()
    symbol = (symbol or "").strip().upper()
    if not label or not symbol:
        return False
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
                PortfolioAllocationSymbol.symbol == symbol,
            )
        ).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
    return True


# ---------------------------------------------------------------------------
# Per-account config: valuation mode + the remembered fractional choice
# ---------------------------------------------------------------------------

def get_allocation_config(account_id: int) -> PortfolioAllocationConfig:
    """The account's allocation config, CREATING it with the defaults on first use.

    Defaults are ``valuation_mode="cost"`` and ``allow_fractional=False`` (spec
    decision 5a). Always returns a row, never ``None``: the page must always be
    able to state which valuation mode produced the numbers on screen.

    Pass the returned ``valuation_mode`` to the engine. It has to be passed: all
    three engine entry points (``compute_base_notional``, ``compute_allocation``,
    ``compute_label_investment``) take it as a REQUIRED keyword with no default,
    precisely so the base and the deltas cannot end up on different definitions of
    "current value". Their defaults used to disagree -- cost for the base, market
    for the solvers -- and a call site that forgot the keyword got both.
    """
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationConfig).where(
                PortfolioAllocationConfig.account_id == account_id)
        ).first()
        if row is None:
            row = PortfolioAllocationConfig(account_id=account_id)
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info(f"Created default allocation config for account {account_id} "
                        f"(valuation_mode={VALUATION_MODE_COST}, allow_fractional=False)")
        session.expunge(row)
        return row


def set_allocation_config(account_id: int, *,
                          valuation_mode: Optional[str] = None,
                          allow_fractional: Optional[bool] = None) -> PortfolioAllocationConfig:
    """Update the account's allocation config; ``None`` leaves a field unchanged.

    Raises:
        ValueError: when ``valuation_mode`` is neither ``VALUATION_MODE_COST`` nor
        ``VALUATION_MODE_MARKET``. A typo'd mode would silently reinterpret every
        percentage on the page -- and the engine only rejects it later, at plan
        time -- so it is refused here rather than stored.
    """
    if valuation_mode is not None and valuation_mode not in (
            VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")

    get_allocation_config(account_id)   # ensure the row exists
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationConfig).where(
                PortfolioAllocationConfig.account_id == account_id)
        ).one()
        if valuation_mode is not None:
            row.valuation_mode = valuation_mode
        if allow_fractional is not None:
            row.allow_fractional = bool(allow_fractional)
        row.updated_at = DateTime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        logger.info(f"Allocation config for account {account_id}: "
                    f"valuation_mode={row.valuation_mode}, allow_fractional={row.allow_fractional}")
        return row


# ---------------------------------------------------------------------------
# Income ledger
# ---------------------------------------------------------------------------

def upsert_income_event(account_id: int, external_id: str, event_date: Date,
                        event_type: str, amount: float,
                        symbol: Optional[str] = None) -> PortfolioIncomeEvent:
    """Insert or update one deposit/dividend, keyed on ``(account_id, external_id)``.

    ``external_id`` is the BROKER's own activity id, which makes re-syncing the
    same window idempotent -- exactly as ``OptionActivity`` does. Re-upserting an
    existing event refreshes date/type/amount/symbol and NEVER touches
    ``consumed_amount``: money already spent stays spent.

    The refresh OVERWRITES the amount, it does not accumulate: a re-sync presents
    every event of the window again, so summing would inflate the ledger on every
    single sync. Overwriting is also what makes a late DIVNRA tax withholding
    correct -- the broker re-states the dividend net of tax and the ledger follows.
    Its one lossy case is two DIV activities for one payer on one pay date that
    BOTH arrive with no broker id (see ``AlpacaAccount.get_cash_transfers``): they
    share the synthetic fallback key and the second overwrites the first. That has
    to be fixed where the duplicate is produced -- by aggregating per key inside
    the seam -- because this function cannot tell a duplicate apart from a re-sync.

    ``event_type`` is a plain str -- pass ``CASH_TRANSFER_DEPOSIT`` or
    ``CASH_TRANSFER_DIVIDEND`` from ``ba2_common.core.account_types``, never a
    bare literal. Withdrawals are not income and must not be sent here.

    Unlike the setters above, ``symbol=None`` here means "this event has no payer
    symbol", not "leave it unchanged": an upsert restates the whole event.

    Raises:
        ValueError: when ``external_id`` is blank -- the idempotency key would
        collapse every event of the account onto one row.
    """
    external_id = (external_id or "").strip()
    if not external_id:
        raise ValueError("upsert_income_event requires a non-empty external_id")
    with get_db() as session:
        row = session.exec(
            select(PortfolioIncomeEvent).where(
                PortfolioIncomeEvent.account_id == account_id,
                PortfolioIncomeEvent.external_id == external_id,
            )
        ).first()
        if row is None:
            row = PortfolioIncomeEvent(
                account_id=account_id, external_id=external_id, event_date=event_date,
                event_type=event_type, amount=float(amount), symbol=symbol,
            )
            session.add(row)
        else:
            if float(amount) < (row.consumed_amount or 0.0):
                # open_amount clamps at 0 so nothing over-allocates from here on,
                # but the platform has already spent more than the event turned out
                # to be worth (a dividend re-stated net of DIVNRA tax after a run
                # consumed the gross). consumed_amount keeps the TRUE spend rather
                # than being clamped, so say so instead of leaving it silent.
                logger.warning(
                    f"Income event {external_id} of account {account_id} was restated "
                    f"from {row.amount} to {float(amount)}, below the "
                    f"{row.consumed_amount} already consumed by allocation runs")
            row.event_date = event_date
            row.event_type = event_type
            row.amount = float(amount)
            row.symbol = symbol
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def get_open_income_events(account_id: int) -> List[PortfolioIncomeEvent]:
    """Income events with money left, OLDEST FIRST (event_date, then id).

    That is exactly the order ``consume_income()`` spends them in.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioIncomeEvent)
            .where(PortfolioIncomeEvent.account_id == account_id)
            .order_by(PortfolioIncomeEvent.event_date, PortfolioIncomeEvent.id)
        ).all()
        rows = [row for row in rows if row.open_amount > 0]
        session.expunge_all()
        return rows


def get_open_income_total(account_id: int) -> float:
    """Total un-consumed income of an account; 0.0 when the ledger is empty."""
    return float(sum(row.open_amount for row in get_open_income_events(account_id)))


def get_income_events_since(account_id: int, since: Date) -> List[PortfolioIncomeEvent]:
    """Every income event on or after ``since``, NEWEST first -- the 30-day panel."""
    with get_db() as session:
        rows = session.exec(
            select(PortfolioIncomeEvent)
            .where(PortfolioIncomeEvent.account_id == account_id,
                   PortfolioIncomeEvent.event_date >= since)
            .order_by(PortfolioIncomeEvent.event_date.desc(), PortfolioIncomeEvent.id.desc())
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows


def consume_income(account_id: int, net_buy_value: float) -> List[Tuple[int, float]]:
    """FIFO-consume the income ledger against a run's NET buy value.

    ``net_buy_value`` is ``max(0, submitted_buy_value - submitted_sell_value)``: a
    rebalance funded entirely by its own sells consumes nothing. Anything ``<= 0``
    consumes nothing and returns ``[]``.

    Events are spent oldest-first -- ``event_date`` then ``id``, the SAME order
    ``get_open_income_events()`` promises -- and the LAST one may be PARTIAL, its
    remainder staying open for the next run.

    The FIFO arithmetic itself is NOT re-derived here: it is the engine's pure
    ``consume_income_events``, so the rule exists exactly once and the service
    layer's dry-run preview cannot drift from what this actually writes. All this
    function adds is the IO -- load in order, apply the takes, commit.

    Events whose ``open_amount`` is 0 are skipped, which covers the over-consumed
    case: a DIVNRA tax leg can restate a dividend BELOW its ``consumed_amount``
    (deliberately left alone, as the true record of what was spent), and
    ``open_amount`` clamps at 0 rather than going negative.

    Returns:
        List[Tuple[int, float]]: ``[(income_event_id, amount_consumed)]`` for the
        events actually touched, oldest first. The total may be LESS than
        ``net_buy_value`` when the ledger cannot cover it; buying power, not the
        ledger, is the feasibility constraint.
    """
    budget = float(net_buy_value)
    if budget <= 0:
        return []
    with get_db() as session:
        rows = session.exec(
            select(PortfolioIncomeEvent)
            .where(PortfolioIncomeEvent.account_id == account_id)
            .order_by(PortfolioIncomeEvent.event_date, PortfolioIncomeEvent.id)
        ).all()
        # open_amount is a PROPERTY, not a column, so it cannot be filtered in SQL;
        # the rows are still attached here, which is what makes reading it legal.
        open_rows = [row for row in rows if row.open_amount > 0]
        consumed = consume_income_events(
            [(row.id, row.open_amount) for row in open_rows], budget)
        by_id = {row.id: row for row in open_rows}
        for event_id, take in consumed:
            row = by_id[event_id]
            row.consumed_amount = (row.consumed_amount or 0.0) + take
            session.add(row)
        if consumed:
            session.commit()
    logger.info(f"Allocation run consumed {len(consumed)} income event(s) for account "
                f"{account_id} against a net buy value of {net_buy_value:.2f}")
    return consumed


# ---------------------------------------------------------------------------
# Run audit
# ---------------------------------------------------------------------------

def record_allocation_run(account_id: int, mode: str, plan_json: Dict[str, Any], *,
                          scope_label: Optional[str] = None,
                          base_notional: float = 0.0,
                          available_buying_power: float = 0.0,
                          allow_fractional: bool = False,
                          submitted_buy_value: float = 0.0,
                          submitted_sell_value: float = 0.0,
                          order_ids: Optional[List[int]] = None) -> PortfolioAllocationRun:
    """Persist the audit row for one allocation run.

    ``mode`` is a plain str -- pass ``ALLOCATION_MODE_REBALANCE`` or
    ``ALLOCATION_MODE_INVEST_LABEL`` from ``ba2_common.core.portfolio_allocation``.
    ``plan_json`` is ``AllocationPlan.to_dict()`` captured at submit time, which
    keeps the dry-run reproducible after the weights change -- including its
    ``valuation_mode``, so the row stays interpretable after the account's mode
    toggle changes. Note ``base_notional`` carries the plan's TWO meanings: the
    allocatable base in a REBALANCE, the budget in an INVEST_LABEL run.

    The live service calls this BEFORE submitting, with zero submitted values, so
    the run id exists to stamp into every order comment; it then calls
    ``update_allocation_run_totals`` with what was actually submitted.

    This does NOT consume income: call ``consume_income(account_id,
    run.net_buy_value)`` next, so the two writes stay separately auditable.

    Every argument is written WHOLESALE -- there is no "None means leave
    unchanged" here, because the row does not exist yet.
    """
    with get_db() as session:
        row = PortfolioAllocationRun(
            account_id=account_id,
            mode=mode,
            scope_label=scope_label,
            base_notional=float(base_notional),
            available_buying_power=float(available_buying_power),
            allow_fractional=bool(allow_fractional),
            plan_json=plan_json or {},
            submitted_buy_value=float(submitted_buy_value),
            submitted_sell_value=float(submitted_sell_value),
            order_ids=list(order_ids or []),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        logger.info(f"Recorded allocation run {row.id} ({mode}) for account {account_id}: "
                    f"buys {row.submitted_buy_value:.2f} / sells {row.submitted_sell_value:.2f}")
        return row


def update_allocation_run_totals(run_id: int, *,
                                 submitted_buy_value: float,
                                 submitted_sell_value: float,
                                 order_ids: List[int]) -> PortfolioAllocationRun:
    """Write back what a run ACTUALLY submitted, after the orders have gone out.

    Both totals are RESTATED wholesale, never merged: they are the final tally of
    a finished submission loop, and ``None`` is rejected rather than read as
    "leave unchanged" -- a missing money figure would silently understate
    ``net_buy_value`` and so under-consume the income ledger.

    Raises:
        ValueError: when either total is None.
        InstanceNotFound: when the run row is gone. That is a real inconsistency
        (the run was recorded seconds earlier) and must not be swallowed.
    """
    from ba2_common.core.db import InstanceNotFound

    if submitted_buy_value is None or submitted_sell_value is None:
        raise ValueError(
            f"update_allocation_run_totals({run_id}) needs both totals as numbers, got "
            f"buys={submitted_buy_value!r} sells={submitted_sell_value!r}; pass 0.0 for "
            f"'nothing was submitted'")

    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == run_id)
        ).first()
        if row is None:
            raise InstanceNotFound(f"PortfolioAllocationRun {run_id} not found")
        row.submitted_buy_value = float(submitted_buy_value)
        row.submitted_sell_value = float(submitted_sell_value)
        row.order_ids = list(order_ids or [])
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def get_recent_runs(account_id: int, limit: int = 20) -> List[PortfolioAllocationRun]:
    """The account's most recent allocation runs, NEWEST first.

    ``plan_json`` / ``order_ids`` come back as ``None`` -- not ``{}`` / ``[]`` --
    on a row written outside this module (raw SQL, an old migration), because the
    columns are nullable JSON whose defaults are applied Python-side. Callers
    must tolerate that.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationRun)
            .where(PortfolioAllocationRun.account_id == account_id)
            .order_by(PortfolioAllocationRun.created_at.desc(), PortfolioAllocationRun.id.desc())
            .limit(limit)
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows
