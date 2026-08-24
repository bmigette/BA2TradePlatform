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

Three rules the callers depend on:

* A ``portfolio_allocation_label`` row's EXISTENCE is the "this label is managed"
  flag -- deleting the row unmanages the label.
* ``portfolio_allocation_symbol`` rows are created LAZILY. A symbol with no row
  takes the even-split default, so ``get_symbol_weights()`` returns a computed
  weight for every symbol you ask about and never an empty dict.
* The income ledger is spent ONLY through ``finalise_allocation_run()``, which
  writes a run's totals, the ledger takes and the run's ``income_consumed_at``
  stamp in ONE transaction. There is deliberately no account-level "consume this
  much" entry point: money is only ever spent on behalf of a run, exactly once,
  and a run that never reached this call is visibly un-consumed
  (``get_unconsumed_runs()``).
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
    validate_unallocated_pct,
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
                      comment: Optional[str] = None,
                      color: Optional[str] = None) -> PortfolioAllocationLabel:
    """Create the managed-label row, or update only the fields you pass.

    ``None`` for a field means LEAVE IT UNCHANGED, so the page can save a comment
    without disturbing the percentage -- or a target without wiping the colour, which
    matters now that both are edited inline on the page and the comment box writes on
    every debounced keystroke. Pass ``""`` to clear a comment or a colour.

    ``color`` differs from ``comment`` in ONE way and it is deliberate: an empty
    comment is stored as ``''``, an empty colour as SQL NULL. ``''`` is not a colour,
    so storing it would create a third state that means exactly what NULL means and
    that nothing can render; NULL is how "no colour chosen" is spelled, and it is a
    different fact from a stored default (which is why the column is never
    back-filled -- see the model and revision c4d7e2b18a93).

    The VALUE is not validated here. The palette is a UI decision and lives in
    ``ba2_trade_platform.ui.utils.portfolio_allocation_view`` (``normalise_label_color``
    on the way in, ``resolve_label_icon_color`` whitelisting on the way out), so this
    module keeps its one job -- persistence -- and does not grow a dependency on the
    UI layer to write a display string.

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
        if color is not None:
            # '' clears to NULL rather than storing an empty string: see the
            # docstring -- '' is not a colour and would be a third state.
            row.color = color or None
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


def save_allocation_targets(account_id: int, labels, *,
                            save_label_targets: bool = True) -> Dict[str, int]:
    """Persist the targets an allocation RUN was launched with. ONE transaction.

    ``labels`` is a list of the engine's ``LabelTarget``s -- exactly what the
    wizard hands to ``on_dry_run`` -- so this writes every ``lt.target_pct`` and
    every ``st.weight_pct`` in one go. It is the answer to "what did I actually
    allocate to last time", which is why it is called on Continue and not on
    Submit: a plan the user reviews and abandons is still the set of numbers they
    last chose, and the fractional switch has been persisted from exactly that
    point since it shipped (``remember_fractional_choice``).

    **A SEPARATE WRITER, deliberately, and this is where the shift lives.** The
    previous generation -- ``previous_target_pct`` / ``previous_weight_pct``, what
    the wizard's Load-last button reads -- is written HERE and by nothing else.
    ``set_managed_label`` and ``set_symbol_weight`` are untouched by this and must
    stay that way: the comment-save path
    (``ui/pages/portfolio_allocation.py::_write_symbol_comment``) re-writes
    ``weight_pct`` on EVERY debounced keystroke -- on purpose, because a bare
    ``comment=`` write would create the row at the model default of 0.0 and the
    engine reads 0 as "hold none of this" (the c63d34c bug). A shift threaded
    through those setters would grind the real previous weight away one character
    at a time; keeping it here makes that impossible by construction rather than by
    a flag someone has to remember not to pass.

    **The shift fires only on a CHANGE.** Pressing Continue twice with the same
    numbers leaves the previous generation exactly where it was: one generation per
    change, not per click. A row created BY this call keeps ``NULL`` -- there is no
    value it held before. The first save of an existing row records the 0.0 the
    label picker created it with, which is a real prior state (the engine reads 0 as
    "hold none of this"), so NULL is reserved for "never saved through the wizard".

    **Never resurrects a label.** ``set_managed_label`` creates the row it cannot
    find, at ``target_pct=0``. A wizard opened before a label was unmanaged (in
    another tab, in the picker) still holds it in memory, so a label with no row is
    SKIPPED and counted, never re-created.

    **Symbol rows become explicit.** A symbol that was silently taking the
    even-split default gets a row. That is correct -- the user has just allocated
    real money with that number -- and it carries the same documented consequence
    ``_write_symbol_comment`` already accepts: a symbol added to the label later
    re-splits only what is LEFT of 100%, not the whole of it.

    ``save_label_targets=False`` writes the symbol weights ONLY. Pass it for an
    INVEST_LABEL run: that spends an explicit AMOUNT on one label, so the label's
    percentage played no part and restating it would record a choice the user never
    made. The weights DID split the money, so they are still persisted.

    Args:
        account_id: the account whose rows are written.
        labels: ``List[LabelTarget]``. ``None`` and ``[]`` are a no-op.
        save_label_targets: whether to write ``target_pct`` as well as the weights.

    Returns:
        Dict[str, int]: ``{'labels': n, 'symbols': n, 'skipped_labels': n}`` --
        what was written and what was dropped for being unmanaged. The caller shows
        the skip count, because a silently ignored save is how a user comes back
        tomorrow to numbers they did not choose.

    Raises:
        ValueError: on a blank label or a blank symbol, BEFORE anything is written.
        A set that cannot be stored in full is not stored at all: half of a target
        set is worse than none, because the next run would deploy against it.
    """
    items = list(labels or [])
    if not items:
        return {"labels": 0, "symbols": 0, "skipped_labels": 0}

    # Validate the WHOLE set first, so the single transaction below cannot abort
    # part way and leave the user's targets half-written.
    cleaned = []
    for lt in items:
        label = (getattr(lt, "label", "") or "").strip()
        if not label:
            raise ValueError("save_allocation_targets: a label is blank")
        weights = []
        for st in (getattr(lt, "symbols", None) or []):
            symbol = (getattr(st, "symbol", "") or "").strip().upper()
            if not symbol:
                raise ValueError(
                    f"save_allocation_targets: label '{label}' has a blank symbol")
            weights.append((symbol, float(st.weight_pct or 0.0)))
        cleaned.append((label, float(getattr(lt, "target_pct", 0.0) or 0.0), weights))

    written_labels = written_symbols = skipped = 0
    with get_db() as session:
        managed = {row.label: row for row in session.exec(
            select(PortfolioAllocationLabel)
            .where(PortfolioAllocationLabel.account_id == account_id)
        ).all()}
        symbol_rows = {(row.label, row.symbol): row for row in session.exec(
            select(PortfolioAllocationSymbol)
            .where(PortfolioAllocationSymbol.account_id == account_id)
        ).all()}

        for label, target_pct, weights in cleaned:
            label_row = managed.get(label)
            if label_row is None:
                skipped += 1
                logger.warning(
                    f"Allocation targets for '{label}' were dropped: account "
                    f"{account_id} no longer manages that label, and re-creating it "
                    f"here would put a label the user deleted back into every "
                    f"future rebalance")
                continue
            if save_label_targets:
                if float(label_row.target_pct or 0.0) != target_pct:
                    label_row.previous_target_pct = float(label_row.target_pct or 0.0)
                label_row.target_pct = target_pct
                session.add(label_row)
            written_labels += 1
            for symbol, weight_pct in weights:
                row = symbol_rows.get((label, symbol))
                if row is None:
                    # Brand new row: there is no value it held before, so its
                    # ``previous_weight_pct`` stays NULL. Recording the even-split
                    # default it was notionally taking would put a number behind
                    # Load last that the user never allocated with.
                    row = PortfolioAllocationSymbol(
                        account_id=account_id, label=label, symbol=symbol)
                    symbol_rows[(label, symbol)] = row
                elif float(row.weight_pct or 0.0) != weight_pct:
                    row.previous_weight_pct = float(row.weight_pct or 0.0)
                row.weight_pct = weight_pct
                session.add(row)
                written_symbols += 1

        session.commit()

    logger.info(f"Saved allocation targets for account {account_id}: "
                f"{written_labels} label(s), {written_symbols} symbol weight(s)"
                + (f", {skipped} label(s) skipped as unmanaged" if skipped else ""))
    return {"labels": written_labels, "symbols": written_symbols,
            "skipped_labels": skipped}


def get_previous_label_targets(account_id: int) -> Dict[str, Optional[float]]:
    """``{label: previous_target_pct}`` for every managed label. NULL stays ``None``.

    ``None`` means "there is no last" -- this label has never been through
    ``save_allocation_targets``. It is NOT interchangeable with 0.0, which means the
    last run allocated nothing to it, and the difference is exactly what the
    wizard's Load-last button reads to decide whether it has anything to offer.

    Every managed label is present in the result, so a missing key means the label
    is not managed rather than that it has no history.
    """
    return {row.label: row.previous_target_pct
            for row in get_managed_labels(account_id)}


def get_previous_symbol_weights(account_id: int, label: str,
                                symbols) -> Dict[str, Optional[float]]:
    """``{symbol: previous_weight_pct}`` for the symbols you ask about.

    The sharp difference from ``get_symbol_weights``: that one FILLS an absent row
    with the even-split default, because a symbol always has an effective weight.
    A symbol does not always have a PREVIOUS one, and there is no default for it --
    computing a plausible number here would put a value behind Load last that the
    user never allocated with. Absent, or stored with a NULL, both give ``None``.

    Symbols are normalised (.strip().upper()) and de-duplicated, order preserved,
    exactly as every other reader here does it.
    """
    syms = _normalise_symbols(symbols)
    if not syms:
        return {}
    stored = get_symbol_rows(account_id, label)
    return {s: (stored[s].previous_weight_pct if s in stored else None) for s in syms}


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

    Defaults are ``valuation_mode="market"`` and ``allow_fractional=True``. Always
    returns a row, never ``None``: the page must always be able to state which
    valuation mode produced the numbers on screen.

    MARKET, not the original spec-5a "cost": the requirement is to allocate by
    VALUE. Cost mode measures the allocatable base as ``buying power + what you
    PAID``, understating it by the whole unrealised P&L, so it tops a winner UP
    where market TRIMS it. Cost basis remains selectable and is the documented
    escape hatch when a held symbol's quote fails.

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
                        f"(valuation_mode={row.valuation_mode}, "
                        f"allow_fractional={row.allow_fractional})")
        session.expunge(row)
        return row


def set_allocation_config(account_id: int, *,
                          valuation_mode: Optional[str] = None,
                          allow_fractional: Optional[bool] = None,
                          unallocated_pct: Optional[float] = None
                          ) -> PortfolioAllocationConfig:
    """Update the account's allocation config; ``None`` leaves a field unchanged.

    ``None`` and not falsiness, for all three: ``allow_fractional=False`` and
    ``unallocated_pct=0.0`` are both real choices, and a truthiness guard would
    make "take the reserve back to zero" a silent no-op.

    Raises:
        ValueError: when ``valuation_mode`` is neither ``VALUATION_MODE_COST`` nor
        ``VALUATION_MODE_MARKET``. A typo'd mode would silently reinterpret every
        percentage on the page -- and the engine only rejects it later, at plan
        time -- so it is refused here rather than stored.
        ValueError: when ``unallocated_pct`` is outside 0-100. Refused where it is
        WRITTEN and not only where it is typed: the wizard is one caller, and a
        stored -20 would inflate the investable base of every future plan into
        money the account does not have.
    """
    if valuation_mode is not None and valuation_mode not in (
            VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    if unallocated_pct is not None:
        # The SAME rule the wizard shows, from the same function, so the box and
        # the column can never disagree about what is storable.
        problems = validate_unallocated_pct(unallocated_pct)
        if problems:
            raise ValueError("; ".join(problems))

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
        if unallocated_pct is not None:
            row.unallocated_pct = float(unallocated_pct)
        row.updated_at = DateTime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        logger.info(f"Allocation config for account {account_id}: "
                    f"valuation_mode={row.valuation_mode}, "
                    f"allow_fractional={row.allow_fractional}, "
                    f"unallocated_pct={row.unallocated_pct}")
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

    That is exactly the order ``finalise_allocation_run()`` spends them in.
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


def _apply_income_consumption(session, account_id: int,
                              net_buy_value: float) -> List[Tuple[int, float]]:
    """FIFO-consume the income ledger against a run's NET buy value. NO COMMIT.

    PRIVATE on purpose. It takes the caller's session and leaves it dirty so that
    the ledger writes land in the SAME transaction as the run's
    ``income_consumed_at`` stamp -- that single commit is what makes consumption
    idempotent per run. A public "consume this much for this account" entry point
    would be exactly the replay hazard this module is built to prevent, so there
    isn't one: go through ``finalise_allocation_run()``.

    ``net_buy_value`` is ``max(0, filled_buy_value - filled_sell_value)``: a
    rebalance funded entirely by its own sells consumes nothing. Anything ``<= 0``
    consumes nothing and returns ``[]``.

    Events are spent oldest-first -- ``event_date`` then ``id``, the SAME order
    ``get_open_income_events()`` promises -- and the LAST one may be PARTIAL, its
    remainder staying open for the next run.

    The FIFO arithmetic itself is NOT re-derived here: it is the engine's pure
    ``consume_income_events``, so the rule exists exactly once and the service
    layer's dry-run preview cannot drift from what this actually writes. All this
    function adds is the IO -- load in order, apply the takes.

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
    budget = float(net_buy_value or 0.0)
    if budget <= 0:
        return []
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
    return consumed


# ---------------------------------------------------------------------------
# Run audit
# ---------------------------------------------------------------------------

def _begin_write_transaction(session) -> None:
    """Take SQLite's single write lock NOW, before this transaction's first read.

    ``get_db()`` hands back a plain ``Session(engine)`` on pysqlite, which issues
    NO ``BEGIN`` ahead of a ``SELECT``: reads run in autocommit and see whatever
    is committed at the instant they execute, taking no snapshot. A
    check-then-act -- read a guard, decide, write -- is therefore completely
    unprotected by default, because two callers' reads both land before either
    write. ``BEGIN IMMEDIATE`` acquires the write lock up front, so the second
    caller's transaction cannot even start until the first has committed and its
    guard read sees the committed truth.

    IMMEDIATE, not DEFERRED: a deferred transaction still takes the lock only at
    the first write, which is exactly the moment that is too late here.

    Safe to WAIT on rather than fail: ``_build_engine`` sets
    ``busy_timeout=30000``, so a blocked writer parks for up to 30s instead of
    getting an instant "database is locked". The lock is held for two SELECTs and
    a handful of UPDATEs, and SQLite has only ONE write lock, so no ordering
    deadlock is possible -- the wait is bounded by the other caller's commit.

    SQLite-only by construction (``_build_engine`` builds nothing else), and
    deliberately NOT conditional on the dialect: a silent no-op branch for some
    other engine would quietly restore the double-spend it exists to prevent.
    """
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def record_allocation_run(account_id: int, mode: str, plan_json: Dict[str, Any], *,
                          scope_label: Optional[str] = None,
                          base_notional: float = 0.0,
                          available_buying_power: float = 0.0,
                          allow_fractional: bool = False,
                          filled_buy_value: float = 0.0,
                          filled_sell_value: float = 0.0,
                          order_ids: Optional[List[int]] = None) -> PortfolioAllocationRun:
    """Persist the audit row for one allocation run.

    ``mode`` is a plain str -- pass ``ALLOCATION_MODE_REBALANCE`` or
    ``ALLOCATION_MODE_INVEST_LABEL`` from ``ba2_common.core.portfolio_allocation``.
    ``plan_json`` is ``AllocationPlan.to_dict()`` captured at submit time, which
    keeps the dry-run reproducible after the weights change -- including its
    ``valuation_mode``, so the row stays interpretable after the account's mode
    toggle changes. Note ``base_notional`` carries the plan's TWO meanings: the
    allocatable base in a REBALANCE, the budget in an INVEST_LABEL run.

    The live service calls this BEFORE submitting, with zero values, so the run id
    exists to stamp into every order comment; it then refreshes the orders from the
    broker and calls ``finalise_allocation_run`` with what actually FILLED.

    This does NOT consume income, and the row it returns is a detached snapshot
    whose totals are whatever you passed (normally zeros) -- never feed its
    ``net_buy_value`` to anything. ``finalise_allocation_run`` re-reads the row
    and consumes the ledger from the totals IT writes, in one transaction.

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
            filled_buy_value=float(filled_buy_value),
            filled_sell_value=float(filled_sell_value),
            order_ids=list(order_ids or []),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        logger.info(f"Recorded allocation run {row.id} ({mode}) for account {account_id}: "
                    f"buys {row.filled_buy_value:.2f} / sells {row.filled_sell_value:.2f}")
        return row


def append_run_order_ids(run_id: int, order_ids: List[int]) -> List[int]:
    """Record order ids on a run AS SOON AS THEY EXIST, without touching anything else.

    ``record_allocation_run`` writes no ids and ``finalise_allocation_run`` runs
    only after the whole submission loop, so between them the run row claimed the
    run had created nothing -- for the whole of the slow, failure-prone part. A
    process killed in there, or an exception on the way to the measurement, left
    orders that had REALLY REACHED THE BROKER attached to a run whose ``order_ids``
    was ``[]``; ``reconcile_unconsumed_runs`` then measured that as "filled
    nothing", stamped the run and dropped it out of ``get_unconsumed_runs()``
    permanently. Money moved and the ledger could never learn of it.

    The caller must invoke this at the EARLIEST durable point for each id -- for a
    new order that is straight after the ``TradingOrder`` row is persisted and
    BEFORE it is handed to the broker, so the id is on disk while the order is
    still incapable of filling.

    APPEND-ONLY and idempotent: ids already on the row are kept and an id supplied
    twice (the whole-share fallback re-reports the order it already created) is
    stored once, because ``collect_order_fills`` would otherwise measure it twice.
    Nothing else on the row is touched -- in particular this NEVER consumes income
    and never stamps ``income_consumed_at``; a run that only ever reaches this
    call stays fully recoverable, which is the entire point.

    Appending to an ALREADY-consumed run is allowed. The stamp closes the ledger,
    not the audit: an order that surfaces afterwards still belongs to the run, and
    refusing it would leave a live broker order that no run in the system admits to.

    Serialised under ``BEGIN IMMEDIATE`` like every other writer here: this is a
    read-modify-write of a JSON column that runs once per order, potentially while
    another caller is finalising the same run, so without the lock an append is
    silently lost.

    Returns:
        List[int]: the run's ids AFTER the append, in order.

    Raises:
        InstanceNotFound: when the run row is gone -- a real inconsistency (it was
        recorded seconds earlier) that must not be swallowed, because the caller is
        about to send an order that would then belong to nothing.
    """
    from ba2_common.core.db import InstanceNotFound

    with get_db() as session:
        _begin_write_transaction(session)
        row = session.exec(
            select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == run_id)
        ).first()
        if row is None:
            raise InstanceNotFound(f"PortfolioAllocationRun {run_id} not found")
        merged = list(row.order_ids or [])
        added = []
        for raw in (order_ids or []):
            order_id = int(raw)
            if order_id not in merged:
                merged.append(order_id)
                added.append(order_id)
        if added:
            # Reassign rather than mutate: order_ids is a JSON column, and
            # SQLAlchemy does not track in-place mutation of a plain list.
            row.order_ids = merged
            session.add(row)
            session.commit()
            session.refresh(row)
        result = list(row.order_ids or [])
        session.expunge(row)
    if added:
        logger.debug(f"Allocation run {run_id} now owns order(s) {result}")
    return result


def finalise_allocation_run(run_id: int, *,
                            filled_buy_value: float,
                            filled_sell_value: float,
                            order_ids: List[int],
                            orders_settled: bool = True) -> PortfolioAllocationRun:
    """Close out a run: write what it FILLED AND spend the income ledger, atomically.

    Call this ONCE per run, after the submission loop, whether or not every order
    made it out -- a partial submission still has to be recorded and still has to
    consume what it actually bought.

    Both totals are RESTATED wholesale, never merged: they are the final tally of a
    finished submission loop as measured from the broker's own fills, and ``None``
    is rejected rather than read as "leave unchanged" -- a missing money figure
    would silently understate ``net_buy_value`` and so under-consume the ledger.
    They are FILLED value: see ``PortfolioAllocationRun``.

    **Why the totals and the consumption are one call.** The budget consumed is
    ``net_buy_value``, which is derived from the totals written by this very
    call, on the row as re-read here -- never from a value the caller carried
    over from ``record_allocation_run`` (that object is a detached snapshot whose
    totals are zero, and passing its ``net_buy_value`` would consume nothing and
    silently leave the whole ledger open). Splitting the two writes also left a
    window in which a crash meant money spent at the broker but still showing as
    unallocated income, so the next run would spend it again. One transaction, no
    window.

    **``orders_settled=False`` records the run WITHOUT spending the ledger.** Pass
    it when at least one of the run's orders can still fill, so its filled value is
    not final yet. The totals and ``order_ids`` are written, but
    ``income_consumed_at`` stays NULL and no income is taken -- which leaves the run
    listed by ``get_unconsumed_runs()``, to be finalised again (as settled) once the
    broker has decided. This is the ONLY safe answer for a working order: consuming
    its planned value spends income that was never deployed, and stamping a zero
    strands income that is about to be, and the one-shot stamp makes either
    permanent. It never UN-consumes: a run that already spent keeps its stamp and
    its breakdown regardless of what this flag says. Expect this path OFTEN -- a
    rebalance that trims positions routinely leaves a WAITING_TRIGGER order behind.

    **Idempotent per run.** ``income_consumed_at`` is checked and set inside that
    same transaction, so a service-layer retry re-states the totals but takes
    from the ledger exactly once. A run that crashed before reaching this call
    has a NULL stamp and is listed by ``get_unconsumed_runs()``, so a recovery
    path can tell the difference between "consumed nothing" and "never got that
    far".

    **Serialised, because the stamp is a check-then-act.** The whole body runs
    under ``_begin_write_transaction`` -- ``BEGIN IMMEDIATE`` before the first
    read -- so concurrent callers queue instead of interleaving. Without it the
    guard is worth nothing: pysqlite starts no transaction for a ``SELECT``, so
    two callers on ONE run both read a NULL stamp and both spend. That was not
    theoretical -- 400 against a 1,000 deposit left the ledger showing 200 open
    instead of 600, silently, in four trials out of five. The same lock also
    closes the narrower window between two DIFFERENT runs, where both read the
    same ``consumed_amount`` and the second write erases the first; a conditional
    ``UPDATE ... WHERE income_consumed_at IS NULL`` on the run row would have
    fixed the replay but not that.

    Returns:
        PortfolioAllocationRun: the detached, refreshed row. Read
        ``income_consumed_amount`` / ``income_consumed_events`` off it for what
        the ledger actually gave up -- possibly LESS than ``net_buy_value``, which
        is not an error: buying power, not the ledger, is the feasibility
        constraint. On a repeat call those fields still describe the ONE
        consumption that happened.

    Raises:
        ValueError: when either total is None.
        InstanceNotFound: when the run row is gone. That is a real inconsistency
        (the run was recorded seconds earlier) and must not be swallowed.
    """
    from ba2_common.core.db import InstanceNotFound

    if filled_buy_value is None or filled_sell_value is None:
        raise ValueError(
            f"finalise_allocation_run({run_id}) needs both totals as numbers, got "
            f"buys={filled_buy_value!r} sells={filled_sell_value!r}; pass 0.0 for "
            f"'nothing was submitted'")

    with get_db() as session:
        # BEGIN IMMEDIATE BEFORE the first read, not just before the first write:
        # everything below is a check-then-act on money, on BOTH paths. Making this
        # conditional on `orders_settled` would reopen the double-spend race. See
        # _begin_write_transaction.
        _begin_write_transaction(session)
        row = session.exec(
            select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == run_id)
        ).first()
        if row is None:
            raise InstanceNotFound(f"PortfolioAllocationRun {run_id} not found")
        account_id = row.account_id
        replayed = row.income_consumed_at is not None
        deferred = not replayed and not orders_settled
        row.filled_buy_value = float(filled_buy_value)
        row.filled_sell_value = float(filled_sell_value)
        row.order_ids = list(order_ids or [])
        if replayed or deferred:
            # replayed: the ledger is NOT touched again. The totals above are
            # re-stated because restating them is harmless; spending twice is not.
            # deferred: the filled value is not final, so there is nothing correct
            # to spend yet -- and no stamp, so the run stays recoverable.
            consumed = [tuple(pair) for pair in (row.income_consumed_events or [])]
        else:
            consumed = _apply_income_consumption(session, account_id, row.net_buy_value)
            row.income_consumed_events = [[event_id, amount] for event_id, amount in consumed]
            row.income_consumed_at = DateTime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)

    if replayed:
        logger.warning(
            f"Allocation run {run_id} of account {account_id} was finalised again; its "
            f"totals were re-stated but the income ledger was left alone (it already "
            f"consumed {row.income_consumed_amount:.2f} from {len(consumed)} event(s))")
    elif deferred:
        logger.warning(
            f"Allocation run {run_id} of account {account_id} recorded filled buys "
            f"{row.filled_buy_value:.2f} / sells {row.filled_sell_value:.2f} but did NOT "
            f"consume income: at least one of its {len(row.order_ids)} order(s) can still "
            f"fill. The run stays in get_unconsumed_runs() and is finalised for real once "
            f"the broker settles it")
    else:
        logger.info(
            f"Finalised allocation run {run_id} for account {account_id}: buys "
            f"{row.filled_buy_value:.2f} / sells {row.filled_sell_value:.2f}, "
            f"consumed {row.income_consumed_amount:.2f} from {len(consumed)} income "
            f"event(s) against a net buy value of {row.net_buy_value:.2f}")
    return row


def get_unconsumed_runs(account_id: int,
                        limit: Optional[int] = 20) -> List[PortfolioAllocationRun]:
    """Runs that never reached ``finalise_allocation_run()``, NEWEST first.

    ``limit=None`` means NO cap, and that is what the drain and the panel pass.
    The default 20 is a display convenience, and inheriting it silently is a bug:
    25 deferred runs left 5 behind on every reconcile pass -- the same 5, forever,
    because the pass consumes the newest 20 and the next pass sees the same
    oldest 5 fall outside the window again -- while the panel reported a backlog
    of 20 out of 25 and an unallocated total that never came down.

    The recovery view, and it is NOT normally empty. A row here either submitted
    orders and died before finalising, or was finalised with ``orders_settled=False``
    because at least one order could still fill -- the ordinary outcome of a
    rebalance that trims held positions. Either way its ``income_consumed_at`` is
    NULL, so the income that funded it still shows as unallocated and the NEXT run
    would spend it a second time.
    ``portfolio_allocation_service.reconcile_unconsumed_runs()`` drains this list at
    the start of every run AND on every income-panel refresh; anything that survives
    several passes wants a human, because only the broker knows what actually went
    out.

    A run that consumed nothing legitimately (a rebalance funded by its own sells)
    is NOT here: it is stamped, with an empty ``income_consumed_events``.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationRun)
            .where(PortfolioAllocationRun.account_id == account_id,
                   PortfolioAllocationRun.income_consumed_at.is_(None))
            .order_by(PortfolioAllocationRun.created_at.desc(), PortfolioAllocationRun.id.desc())
            .limit(limit)
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows


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


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------

def delete_account_allocation_data(account_id: int) -> Dict[str, int]:
    """Delete every allocation row of an account. Returns per-table delete counts.

    The live DB runs with ``PRAGMA foreign_keys = 0``, so the ``ondelete="CASCADE"``
    declared on these tables NEVER fires. Account deletion must call this
    explicitly, exactly as the ``AccountSetting`` cleanup loop in
    ``ui/pages/settings.py`` does. Skipping it strands five tables of rows on a
    dead ``account_id`` -- and because ``portfolio_allocation_config.account_id`` is
    UNIQUE, the next account to reuse that id would collide with the corpse.

    The returned counts are the ONLY surviving record that this account ever
    tracked income: deleting the events discards their ``consumed_amount``
    history, which is right for a deletion but unrecoverable. Hence the log line.
    """
    counts: Dict[str, int] = {}
    with get_db() as session:
        for key, model in (("config", PortfolioAllocationConfig),
                           ("labels", PortfolioAllocationLabel),
                           ("symbols", PortfolioAllocationSymbol),
                           ("income_events", PortfolioIncomeEvent),
                           ("runs", PortfolioAllocationRun)):
            rows = session.exec(select(model).where(model.account_id == account_id)).all()
            for row in rows:
                session.delete(row)
            counts[key] = len(rows)
        session.commit()
    logger.info(f"Deleted portfolio allocation data for account {account_id}: {counts}")
    return counts


# ---------------------------------------------------------------------------
# Page helpers: bulk label selection, symbol membership, comment reads
# ---------------------------------------------------------------------------

def replace_managed_labels(account_id: int, labels) -> Dict[str, int]:
    """Make ``labels`` EXACTLY the account's managed set, in the given order.

    This is the label-picker's writer; ``set_managed_label`` remains the writer
    for ONE label's target/comment. A label that SURVIVES the change keeps its
    row -- and therefore its ``target_pct`` and ``comment`` -- and only has its
    ``sort_order`` restated: the picker fires on every change event, so
    re-creating a survivor would wipe the user's target every time they ticked an
    unrelated label. Unmanaging a label also deletes that account's lazy symbol
    rows for it (the live DB runs with ``PRAGMA foreign_keys = 0``, so nothing
    cascades on its own).

    Returns:
        Dict[str, int]: ``{'added': n, 'removed': n}``. Re-saving the same
        selection returns zeroes, which lets an eager on-change handler skip a
        pointless write.
    """
    wanted: List[str] = []
    for label in (labels or []):
        text = (label or "").strip()
        if text and text not in wanted:
            wanted.append(text)

    added = removed = 0
    with get_db() as session:
        existing = session.exec(
            select(PortfolioAllocationLabel)
            .where(PortfolioAllocationLabel.account_id == account_id)
        ).all()
        by_label = {row.label: row for row in existing}

        for label, row in list(by_label.items()):
            if label in wanted:
                continue
            for srow in session.exec(select(PortfolioAllocationSymbol).where(
                    PortfolioAllocationSymbol.account_id == account_id,
                    PortfolioAllocationSymbol.label == label)).all():
                session.delete(srow)
            session.delete(row)
            removed += 1

        for order, label in enumerate(wanted):
            row = by_label.get(label)
            if row is None:
                session.add(PortfolioAllocationLabel(
                    account_id=account_id, label=label, target_pct=0.0, sort_order=order))
                added += 1
            elif row.sort_order != order:
                row.sort_order = order
                session.add(row)

        session.commit()

    logger.info(f"Managed labels for account {account_id}: +{added} / -{removed} -> {wanted}")
    return {'added': added, 'removed': removed}


def get_symbol_comments(account_id: int, label: str) -> Dict[str, str]:
    """``{SYMBOL: comment}`` for one managed label; symbols with no comment are omitted."""
    return {symbol: row.comment
            for symbol, row in get_symbol_rows(account_id, label).items()
            if row.comment}


def add_symbols_to_label(account_id: int, label: str, symbols) -> int:
    """Give ``symbols`` the instrument label ``label``. Returns instruments changed.

    Instrument labels are GLOBAL (they live on the ``instrument`` row), so this
    also affects any other account managing the same label. ``account_id`` is
    accepted and logged for auditability.

    A symbol does NOT need an open position: the page adds and removes label
    membership for anything the user names, and the allocation engine treats a
    labelled symbol with no position as a target to buy into.
    """
    from ba2_common.core.utils import add_label_to_instruments

    lbl = (label or "").strip()
    syms = _normalise_symbols(symbols)
    if not lbl or not syms:
        return 0
    changed = add_label_to_instruments(syms, lbl)
    logger.info(f"Account {account_id}: added label '{lbl}' to "
                f"{changed}/{len(syms)} instrument(s)")
    return changed


def remove_symbols_from_label(account_id: int, label: str, symbols) -> int:
    """Drop the instrument label AND delete this account's lazy symbol rows.

    The row delete is scoped by ``(account_id, label, symbol)``: a symbol may sit
    in several managed labels, and dropping it from one must not discard the
    weight or comment it carries under another.

    Returns the number of instruments whose label list changed.
    """
    from ba2_common.core.utils import remove_label_from_instruments

    lbl = (label or "").strip()
    syms = _normalise_symbols(symbols)
    if not lbl or not syms:
        return 0
    changed = remove_label_from_instruments(syms, lbl)
    with get_db() as session:
        rows = session.exec(select(PortfolioAllocationSymbol).where(
            PortfolioAllocationSymbol.account_id == account_id,
            PortfolioAllocationSymbol.label == lbl,
            PortfolioAllocationSymbol.symbol.in_(syms))).all()
        for row in rows:
            session.delete(row)
        if rows:
            session.commit()
    logger.info(f"Account {account_id}: removed label '{lbl}' from "
                f"{changed}/{len(syms)} instrument(s)")
    return changed
