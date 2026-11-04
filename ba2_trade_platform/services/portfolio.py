"""Portfolio Allocation data loaders, shared by the page (``ui/pages/portfolio_allocation.py``) and the
management API (``api/portfolio.py``): ONE function, two callers.

The page calls these with their default arguments and keeps its old private names as thin wrappers. The API calls
``load_view_payload(..., read_only=True)`` and ``read_income_panel`` so a GET never writes the DB or the broker
(see ``docs/plans/2026-11-04-management-api-phase5.md``). Blocking; the page calls them through
``asyncio.to_thread``."""
import threading
import time
from typing import Any, Dict, List, Optional

from sqlmodel import select

from ..core import allocator_exclusion as exclusion_store
from ..core import allocator_protection_service as protect_svc
from ..core import portfolio_allocation_service as svc
from ..core.db import get_db
from ..core.models import ExpertInstance
from ..core.portfolio_allocation import (
    VALUATION_MODE_MARKET, cash_dividends_by_symbol, compute_base_notional, unconsumed_income_notice)
from ..core.portfolio_allocation_store import (
    get_allocation_config, get_managed_labels, get_previous_symbol_weights, get_symbol_comments, get_symbol_rows)
from ..logger import logger
from ..ui.utils.portfolio_allocation_view import (
    GateResult, ManagedLabel, account_value_from_snapshot, build_label_views, collect_managed_symbols,
    evaluate_gate, positions_by_symbol)
from ba2_common.core.symbol_facts import load_symbol_facts, save_symbol_facts
from ba2_common.core.symbol_stats import load_symbol_stats


def enabled_expert_names(account_id: int) -> List[str]:
    """Display names of the account's ENABLED experts; empty list when there are none."""
    with get_db() as session:
        rows = session.exec(
            select(ExpertInstance).where(
                ExpertInstance.account_id == account_id,
                ExpertInstance.enabled == True,  # noqa: E712 — SQL boolean, not identity
            )
        ).all()
        return [(r.alias or r.expert) for r in rows]


def load_gate(account_id: Optional[int]) -> GateResult:
    """Resolve the three gate inputs.

    An account that cannot be instantiated is reported as "not manual" rather than
    crashing the page — the user's next action (open Settings) is the same either way.
    """
    from ..core.utils import get_account_instance_from_id

    if account_id is None:
        return evaluate_gate(None, False, [])
    try:
        account = get_account_instance_from_id(account_id)
    except Exception as e:
        logger.error(f"Portfolio allocation: cannot load account {account_id}: {e}", exc_info=True)
        account = None
    if account is None:
        return evaluate_gate(account_id, False, [])
    manual = bool(account.get_setting_with_interface_default(
        'manual_trading_enabled', log_warning=False))
    return evaluate_gate(account_id, manual, enabled_expert_names(account_id))


def load_protection_payload(account, account_id: int, positions) -> Dict[str, Any]:
    """The TP/SL layer's share of one render. Blocking (a broker read on a supporting account).

    ``supported`` is tested with ``is True``: a MagicMock account is truthy on any attribute.
    DISPLAY ONLY (review F8): no reconcile and no order is placed or cancelled on page load -- that
    work scales with the number of protected symbols and must not run on every render. The
    reconcile runs in the TradeManager account refresh and in the explicit 'Check TP/SL orders now'
    action; the page draws what those last stored.
    """
    if getattr(account, 'supports_allocator_protection', False) is not True:
        return {'supported': False, 'items': {}, 'quantities': {}}
    items = {p.symbol: (p, protect_svc.get_slices(p.id))
             for p in protect_svc.list_protections(account_id)}
    # The broker's held quantity per symbol (read ONCE for the page), so a protection on a
    # symbol that is in no managed label is still judged against its real position.
    quantities = {sym: float(state.quantity) for sym, state in positions.items()}
    return {'supported': True, 'items': items, 'quantities': quantities}


def read_symbol_facts_strict(account_id: int, symbols) -> Dict[str, Any]:
    """Stored symbol facts for the API: like ``load_symbol_facts`` but a read FAILURE raises ``Unmeasurable``
    instead of reading as "no facts" (that helper returns ``{}`` on any error)."""
    from ..core.models import AccountSymbolFacts
    from .errors import Unmeasurable
    wanted = sorted({(x or "").strip().upper() for x in symbols if (x or "").strip()})
    if not wanted:
        return {}
    try:
        with get_db() as session:
            rows = session.exec(select(AccountSymbolFacts).where(
                AccountSymbolFacts.account_id == account_id,
                AccountSymbolFacts.symbol.in_(wanted))).all()
            for row in rows:
                session.expunge(row)
            return {row.symbol: row for row in rows}
    except Exception as e:  # noqa: BLE001 -- any DB failure is "unreadable", never "empty"
        logger.error(f"API: reading stored symbol facts for account {account_id} failed: {e}", exc_info=True)
        raise Unmeasurable(f"Account {account_id}: the stored symbol facts could not be read",
                           {"account_id": account_id, "stored_read_failed": "symbol_facts"}) from e


def read_symbol_stats_strict(symbols) -> Dict[str, Any]:
    """Stored symbol stats for the API; a read failure raises ``Unmeasurable`` (see ``read_symbol_facts_strict``)."""
    from ..core.models import SymbolMarketStats
    from .errors import Unmeasurable
    wanted = sorted({(x or "").strip().upper() for x in symbols if (x or "").strip()})
    if not wanted:
        return {}
    try:
        with get_db() as session:
            rows = session.exec(select(SymbolMarketStats).where(SymbolMarketStats.symbol.in_(wanted))).all()
            for row in rows:
                session.expunge(row)
            return {row.symbol: row for row in rows}
    except Exception as e:  # noqa: BLE001
        logger.error(f"API: reading stored symbol stats failed: {e}", exc_info=True)
        raise Unmeasurable("The stored symbol stats could not be read", {"stored_read_failed": "symbol_stats"}) from e


def refresh_symbol_facts(account, account_id: int, symbols):
    """Re-ask the broker what it knows about ``symbols``, store it, return it.

    The chips exist to answer "can this be split?" and "what does it cost in buying
    power?" without opening a dialog, and both answers are per ACCOUNT -- so they are
    fetched through the account's own seam and cached against its id.

    A fetch that fails or comes back empty falls back to the STORED rows rather than
    to nothing: the previous answer is still the best available statement of a fact
    that changes rarely (Alpaca revokes marginability, it does not churn it), and
    blanking the chips would read as "the broker says no" when the truth is "the
    broker did not answer just now".
    """
    if not symbols:
        return {}
    try:
        info = svc.fetch_margin_info(account, list(symbols))
        if info:
            save_symbol_facts(account_id, info)
    except Exception as e:  # noqa: BLE001 -- the chips are never worth the page
        logger.warning(f"Refreshing symbol facts for account {account_id} failed: {e}")
    try:
        return load_symbol_facts(account_id, symbols)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reading stored symbol facts for account {account_id} failed: {e}")
        return {}


def load_view_payload(account_id: int, valuation_mode: str,
                      base_override: Optional[float] = None, *, read_only: bool = False,
                      account=None, config=None, dividends_source=None) -> Dict[str, Any]:
    """One render's worth of data: managed labels, membership, positions, prices.

    ``valuation_mode`` is threaded through to ``build_label_views`` and echoed back
    in the payload so the render names the mode that produced the numbers next to
    them -- switching modes RE-COMPUTES rather than silently reinterpreting.

    ALSO reads the account snapshot, which this function did not do at all before
    W3: without buying power the page cannot show cash, cannot show the unallocated
    group requirement 3 asks for, and cannot put the current and target percentages
    under one denominator. The cost is bounded -- Alpaca caches the snapshot for 5s
    -- and a broker that will not answer costs the RESERVE ROW, not the page:
    ``base_notional`` and ``available_buying_power`` come back ``None`` and every
    figure derived from them is omitted rather than guessed.

    ``read_only=True`` is the management API's mode (a GET must not write): the symbol facts are the STORED rows
    instead of a broker refresh that also writes the DB, and the allocation config row is the caller's ``config``
    (never created here). ``account`` lets the caller supply an already-resolved account. Every other read is the
    same as the page's.

    Raises:
        PositionFetchFailed: the broker position fetch failed (NOT a flat account).
        RuntimeError: the account could not be instantiated.
    """
    from ..core.utils import (get_account_instance_from_id, get_company_names,
                               get_symbols_by_label)

    # ``previous_target_pct`` travels with the label, NULL and all: it is what the
    # row prints as "last N%" and what the page's Load-last reads, and a ``or 0.0``
    # anywhere on this path would turn "never allocated" into "allocated nothing".
    managed = [ManagedLabel(label=row.label, target_pct=row.target_pct,
                            comment=row.comment, color=row.color,
                            previous_target_pct=row.previous_target_pct)
               for row in get_managed_labels(account_id)]
    symbols_by_label = get_symbols_by_label([m.label for m in managed])
    symbols = collect_managed_symbols(symbols_by_label)
    # THE OPERATOR'S EXCLUSIONS (manual disable). An excluded symbol is still priced and drawn
    # (quantity, value and P&L are information) but it is OUTSIDE the managed money: its value is
    # not in any label, nor in the investable base below.
    exclusions = exclusion_store.get_exclusions(account_id)
    enabled_symbols = exclusion_store.split_included(symbols, exclusions)

    if account is None:
        account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")

    positions = positions_by_symbol(account.get_positions())

    protection = load_protection_payload(account, account_id, positions)

    prices: Dict[str, Optional[float]] = {}
    if symbols:
        # THE MARK, EXPLICITLY -- see build_position_states for the full reasoning. This is
        # the SECOND price fetch on this page: the wizard prices through
        # build_position_states, the label table prices here, and fixing only the first left
        # MAGY still reading 16.86 on the very screen the discrepancy was reported from.
        # Both are valuations and both must ask for the same thing.
        fetched = account.get_instrument_current_price(symbols, price_type='mark')
        if isinstance(fetched, dict):
            prices = dict(fetched)
        else:
            logger.warning(f"Bulk price fetch returned {type(fetched).__name__}, "
                           f"expected a dict — rendering without prices")

    # ``positions_by_symbol`` does not populate ``price`` -- quotes come from the
    # bulk call above -- so stamp them on before anything measures a market value.
    # ``build_label_views`` builds its own priced copy either way; what needs this
    # is ``compute_base_notional`` below, which reads ``PositionState.price``
    # directly and would otherwise value the whole book at 0 in market mode.
    for symbol, state in positions.items():
        if state.price is None:
            state.price = prices.get(symbol)

    # Broker facts (fractionability, margin rate) for the ⓘ chips. Refreshed HERE,
    # which is what the Refresh button re-runs, and cached in the DB so the chips
    # survive a broker that will not answer: a failed fetch falls back to the last
    # stored answer rather than blanking facts that were true five minutes ago.
    # Never fatal -- the page's job is the label table, and the chips are on top.
    if read_only:
        facts = read_symbol_facts_strict(account_id, symbols)
    else:
        facts = refresh_symbol_facts(account, account_id, symbols)
    # READ ONLY. The yield and the 1Y/3Y return need several FMP calls per symbol,
    # far too slow for a render, so the page draws whatever is already cached (nothing
    # on a cold start) and the top-up runs from the PAGE's refresh handler instead.
    #
    # The refresh deliberately does NOT start here: this function is called directly by
    # the page tests, and a daemon thread doing DB work outlives the test that started
    # it -- which raced the fixture's teardown into a Windows access violation. A
    # loader that spawns background work is a loader that cannot be called safely.
    stats = read_symbol_stats_strict(symbols) if read_only else load_symbol_stats(symbols)

    comments: Dict[tuple, str] = {}
    weights: Dict[str, Dict[str, float]] = {}
    previous_weights: Dict[str, Dict[str, Optional[float]]] = {}
    for entry in managed:
        for symbol, text in get_symbol_comments(account_id, entry.label).items():
            comments[(entry.label, symbol)] = text
        members = symbols_by_label.get(entry.label, [])
        # THE SAVED ROWS ONLY -- ``get_symbol_rows``, not ``get_symbol_weights``.
        # That one fills an absent row with the FAIR SHARE, which is precisely the
        # default being removed: nine symbols in a label all reading 11.11 while
        # their real shares were 26.78 / 22.19 / ... The effective share is decided
        # in ``resolve_symbol_weights`` (saved wins, else the actual share, else
        # blank), and it needs to be told which rows are genuinely saved.
        weights[entry.label] = {symbol: float(row.weight_pct)
                                for symbol, row
                                in get_symbol_rows(account_id, entry.label).items()
                                if symbol in members}
        # A SEPARATE reader from ``get_symbol_weights`` on purpose: that one fills
        # an absent row with the even-split default, and there is no default for a
        # weight nobody has ever allocated with.
        previous_weights[entry.label] = get_previous_symbol_weights(
            account_id, entry.label, members)

    buying_power = base_notional = account_value = None
    snapshot_available = True
    try:
        # ONE snapshot for both figures. A second ``get_account_snapshot()`` for
        # the account value would double the REST cost of every refresh -- and on
        # Alpaca that call is two endpoints, not one.
        snapshot = account.get_account_snapshot()
        # Extracted FIRST, so a failure in the base arithmetic below costs the
        # reserve row (as it always has) and not the account-value card as well.
        account_value = account_value_from_snapshot(snapshot)
        if snapshot is not None and snapshot.buying_power is not None:
            buying_power = float(snapshot.buying_power)
            # Exactly ``compute_base_notional``'s rule, which is what the wizard's
            # ``build_base_snapshot`` uses: buying power plus the DISTINCT managed
            # value under the active mode. Anything else and the page's percentages
            # would divide by a different base from the plan's.
            base_notional = compute_base_notional(buying_power, positions, enabled_symbols,
                                                  valuation_mode=valuation_mode)
    except Exception as e:
        snapshot_available = False
        # The label table is this page's job; buying power is a bonus on top of it.
        # None propagates and the reserve row is simply not drawn -- better a
        # missing row than one measured against a guessed base.
        logger.warning(f"Account snapshot unavailable for account {account_id}: {e}; "
                       f"the unallocated group and the % of base will be omitted")

    # The stored reserve, read from the SAME config row as the valuation mode. It
    # scales the TARGET money the page shows so those figures match the ones the
    # dry run will solve with; every "current" figure keeps dividing the gross base.
    if config is None:
        config = get_allocation_config(account_id)
    unallocated_pct = float(config.unallocated_pct or 0.0)

    # SIMULATION. A what-if base replaces the measured one for every figure DERIVED
    # from it -- target money, % of base, the reserve row -- and nothing else. The
    # held positions, prices, buying power and account value stay exactly as the
    # broker reported them, because those are measurements and a simulation of them
    # would be a fabricated broker answer. The real base is carried alongside so the
    # banner can name what was replaced, and ``simulated_base`` is what every consumer
    # tests rather than comparing two floats.
    real_base_notional = base_notional
    simulated = base_override is not None and float(base_override) > 0
    if simulated:
        # WHICH TERM ABSORBS THE CHANGE. ``compute_base_notional`` defines
        # base = managed + free buying power, and the whole page leans on that being an
        # IDENTITY: ``format_base_composition``, the reserve row and the allocation bar
        # all derive the managed side as ``base - buying_power`` rather than re-summing
        # it. Replacing the base alone therefore did not simulate a bigger account -- it
        # silently inflated MANAGED by the difference, so an $8,500 what-if over $4,764
        # of real positions reported "$8,036.72 managed" and 94% allocated.
        #
        # Simulating a bigger base means simulating more CASH: the positions are a
        # measurement (real quantities at real prices) and cannot be what-if'd, so free
        # buying power is the derived term and the identity holds again. Allocated then
        # falls as the base grows, which is the question being asked.
        #
        # Left signed rather than clamped at zero: a base BELOW the managed value is a
        # legitimate what-if (positions financed on margin), and the page already renders
        # an over-100% allocation. Clamping would restore a number at the cost of
        # breaking the identity a second time.
        managed_real = (None if real_base_notional is None or buying_power is None
                        else float(real_base_notional) - float(buying_power))
        base_notional = float(base_override)
        if managed_real is not None:
            buying_power = base_notional - managed_real

    dividends = account.get_dividends() if dividends_source is None else dividends_source()
    # Market mode values an unpriced managed holding at 0 inside ``compute_base_notional``, so the base above is a
    # LOWER BOUND whenever one exists (the page does not flag this; the API does).
    unpriced_managed = sorted(sym for sym in enabled_symbols
                              if sym in positions and valuation_mode == VALUATION_MODE_MARKET
                              and positions[sym].quantity and positions[sym].price is None)
    return {
        'views': build_label_views(managed, symbols_by_label, positions, prices, comments,
                                   valuation_mode=valuation_mode,
                                   base_notional=base_notional,
                                   symbol_weights=weights,
                                   symbol_previous_weights=previous_weights,
                                   # One indexed query over the managed set, not one per
                                   # row: the ⓘ tooltip is the only consumer and it is
                                   # not worth a lookup per cell.
                                   company_names=get_company_names(symbols),
                                   # The "w/ div" half of the P&L column: the
                                   # broker's FULL dividend history, cash only.
                                   #
                                   # NOT the income ledger. get_dividends_by_symbol
                                   # answers "what cash is waiting to be deployed"
                                   # and is synced over a rolling
                                   # INCOME_WINDOW_DAYS=30 window, so it held six
                                   # weeks of a six-month position and reported a
                                   # fifth of what the holding had paid
                                   # (WHEEL_L1_HR, 2026-09-07: 46.98 against ~189).
                                   # It stays the source for CONSUMPTION, which is
                                   # the question it is right for.
                                   #
                                   # Cash only, via the same helper the growth
                                   # charts use: a reinvested dividend is already
                                   # in market value and cost basis, so counting it
                                   # here would book the money twice.
                                   #
                                   # This one costs a REST call, unlike the ledger
                                   # read it replaces. It is on the same background
                                   # thread as the rest of this payload, and a
                                   # broker that will not answer costs the "w/ div"
                                   # figure, not the page: get_dividends returns []
                                   # on failure and every row simply has no
                                   # dividend-adjusted number.
                                   dividends_by_symbol=cash_dividends_by_symbol(dividends),
                                   unallocated_pct=unallocated_pct,
                                   excluded=exclusions,
                                   freed_labels=exclusion_store.labels_with_weight_changes(account_id)),
        'symbols_by_label': symbols_by_label,
        'valuation_mode': valuation_mode,
        'base_notional': base_notional,
        'available_buying_power': buying_power,
        # The account's OWN value (net liquidating value), as distinct from the
        # managed positions' market value. ``None`` is UNKNOWN, never 0.0.
        'account_value': account_value,
        'unallocated_pct': unallocated_pct,
        #: True while the base above is a user's what-if rather than a measurement.
        'simulated_base': simulated,
        #: The MEASURED base the simulation replaced. None when the broker could not
        #: supply one -- which is a legitimate reason to simulate in the first place.
        'real_base_notional': real_base_notional,
        #: ``{symbol: AccountSymbolFacts}``. A symbol the broker has never described
        #: is simply absent, and the row then draws no chips -- see
        #: ``fractionable_badge`` / ``leverage_badge``.
        'symbol_facts': facts,
        #: ``{symbol: SymbolMarketStats}`` for the ⓘ tooltip. Absent until the
        #: background refresh has reached that symbol.
        'symbol_stats': stats,
        #: The allocator TP/SL layer: ``{'supported': bool, 'items': {symbol: (protection,
        #: slices)}}``. ``supported`` False on every broker that cannot do it, and then the
        #: page draws nothing of it.
        'protection': protection,
        #: ``{SYMBOL: AllocatorExclusion}`` -- the symbols the operator switched off.
        'exclusions': exclusions,
        'account_id': account_id,
        #: False when the snapshot read failed (base / buying power / account value are then None).
        'snapshot_available': snapshot_available,
        #: Enabled managed holdings with no quote (market mode): the base excludes their value.
        'unpriced_managed_holdings': unpriced_managed,
        #: How many rows ``get_dividends`` returned. It returns ``[]`` on a failed fetch too, so 0 cannot tell
        #: "pays nothing" from "could not ask"; the API says so next to the figure.
        'dividend_payments': len(dividends or []),
    }


def load_valuation_mode(account_id: int) -> str:
    """The account's stored valuation mode, creating the config row on first use."""
    return get_allocation_config(account_id).valuation_mode


def peek_allocation_config(account_id: int):
    """The account's allocation config row, or ``None`` when it has none. NEVER creates one (a GET is pure;
    ``get_allocation_config`` creates the row on first use, which is the page's behaviour)."""
    from ..core.models import PortfolioAllocationConfig
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig).where(
            PortfolioAllocationConfig.account_id == account_id)).first()
        if row is not None:
            session.expunge(row)
        return row


def read_income_panel(account_id: int):
    """The STORED half of the income panel: no broker call, no write. ``(events, open_total, working_note)``.

    ``describe_unconsumed_runs`` is the DB-only read behind the working-orders sentence."""
    outstanding = svc.describe_unconsumed_runs(account_id)
    working_note = unconsumed_income_notice(len(outstanding["run_ids"]),
                                            len(outstanding["working_order_ids"]))
    return (svc.get_recent_income_events(account_id),
            svc.get_open_income_total(account_id),
            working_note)


def load_income_panel(account_id: int):
    """Sync the ledger from the broker, read it back, and say what is still owed.

    Blocking; always called through ``asyncio.to_thread``. WRITES (``sync_income_events`` upserts the ledger and
    runs the unconsumed-run reconcile pass): the page's load and Refresh only; the API uses ``read_income_panel``.

    ``svc.sync_income_events`` runs the unconsumed-run reconcile pass first (it is
    the panel's Refresh handler AND this page's load call, so that one hook covers
    both). Runs whose orders were still working when they finalised consumed NO
    income, and with about a quarter of the book on whole shares that is the common
    outcome, not the rare one -- so the drain has to be automatic, and what SURVIVES
    it has to be visible.

    Returns:
        ``(events, open_total, working_note)`` where ``working_note`` is the
        ``(text, severity)`` pair or ``None``.
    """
    from ..core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    svc.sync_income_events(account)
    return read_income_panel(account_id)


# ---------------------------------------------------------------------------
# Management API reads. Pure: nothing below writes the DB or asks a broker to refresh anything.
# ---------------------------------------------------------------------------

#: Per-account TTL cache of ``get_dividends()`` for the API path only (the call is a REST fetch of the broker's
#: full dividend history). Only a non-empty answer is cached: ``[]`` is also what a failed fetch returns.
DIVIDENDS_TTL_S = 60.0
_DIVIDENDS_CACHE: Dict[int, Any] = {}
_DIVIDENDS_LOCK = threading.Lock()


def cached_dividends(account_id: int, fetch):
    """``(rows, age_s, from_cache)``: ``fetch()`` at most once per ``DIVIDENDS_TTL_S`` per account."""
    with _DIVIDENDS_LOCK:
        hit = _DIVIDENDS_CACHE[account_id] if account_id in _DIVIDENDS_CACHE else None
        if hit is not None and time.monotonic() - hit[0] < DIVIDENDS_TTL_S:
            return list(hit[1]), time.monotonic() - hit[0], True
    rows = fetch()
    if rows:
        with _DIVIDENDS_LOCK:
            _DIVIDENDS_CACHE[account_id] = (time.monotonic(), list(rows))
    return rows, 0.0, False


def _pnl_dict(pnl) -> Dict[str, Any]:
    """``UnrealisedPnL`` as the API shows it. ``amount`` / ``pct`` are null when they cannot be measured, and the
    ``with_dividends_*`` pair is null whenever there is no dividend cash to add (the engine's own rule)."""
    return {"amount": pnl.amount, "pct": pnl.pct, "with_dividends_amount": pnl.total_amount,
            "with_dividends_pct": pnl.total_pct, "dividend_cash": pnl.dividends,
            "priced": pnl.priced, "unpriced": pnl.unpriced}


def _row_dict(row, facts, stats, *, label_complete: bool, book_complete: bool, base_known: bool) -> Dict[str, Any]:
    f = facts[row.symbol] if row.symbol in facts else None
    st = stats[row.symbol] if row.symbol in stats else None
    measured = row.measurable
    return {
        "symbol": row.symbol, "company_name": row.company_name, "labels": list(row.labels),
        "quantity": row.quantity, "cost_basis": row.cost_basis, "price": row.price,
        "market_value": row.market_value,
        # An unpriced holding has no knowable value: null, never the 0.0 the view builder uses internally.
        "measured": measured,
        "current_value": row.current_value if measured else None,
        # A share is a ratio over a denominator that includes the unpriced holdings: with one unmeasured it is
        # understated for every other row, so it is null (pct_of_label: per label; pct_of_total: whole book).
        "pct_of_label": row.pct_of_label if measured and label_complete else None,
        "pct_of_label_target": row.pct_of_label_target if measured and base_known else None,
        "pct_of_total": row.pct_of_total if measured and book_complete else None,
        "target_share_pct": row.weight_pct, "target_share_source": row.weight_source,
        "effective_target_share_pct": row.effective_weight_pct,
        "target_value": row.target_value if base_known else None,
        "previous_target_share_pct": row.previous_weight_pct, "comment": row.comment,
        "pnl": _pnl_dict(row.pnl),
        "excluded": row.excluded, "excluded_reason": row.excluded_reason, "excluded_note": row.excluded_note,
        "excluded_since": row.excluded_since,
        "facts": None if f is None else {"fractionable": f.fractionable, "marginable": f.marginable,
                                         "tradable": f.tradable, "bp_factor": f.bp_factor,
                                         "fetched_at": f.fetched_at},
        "stats": None if st is None else {"dividend_yield_pct": st.dividend_yield_pct,
                                          "total_return_1y_pct": st.total_return_1y_pct,
                                          "total_return_3y_pct": st.total_return_3y_pct, "error": st.error,
                                          "fetched_at": st.fetched_at},
    }


def _label_dict(view, facts, stats, *, book_complete: bool, base_known: bool) -> Dict[str, Any]:
    unmeasured = [r.symbol for r in view.rows if not r.measurable and not r.excluded]
    complete = not unmeasured
    rows = [_row_dict(r, facts, stats, label_complete=complete, book_complete=book_complete,
                      base_known=base_known) for r in view.rows]
    return {
        "label": view.label, "color": view.color, "comment": view.comment,
        "target_pct": view.target_pct, "previous_target_pct": view.previous_target_pct,
        # Current weight is comparable with the target: share of the allocatable base (buying power + managed
        # value). With an unpriced holding in the label the sum is understated, so it is null, not a smaller number.
        "current_pct_of_base": view.pct_of_base if complete and base_known else None,
        "pct_of_managed": view.pct_of_total if book_complete else None,
        "target_value": view.target_value if base_known else None,
        "invested": view.cost_basis,                 # what the held positions cost
        "current_value": view.current_value if complete else None,
        "current_value_measured_part": view.current_value,
        "complete": complete, "unmeasured_symbols": unmeasured,
        "pnl": _pnl_dict(view.pnl),
        "excluded_value": view.excluded_value, "freed_pct": view.freed_pct,
        "symbol_counts": {"total": len(rows), "held": sum(1 for r in rows if r["quantity"]),
                          "excluded": view.excluded_count, "unmeasured": len(unmeasured)},
        "symbols": rows,
    }


def _newest(rows) -> Optional[Any]:
    stamps = [r.fetched_at for r in rows.values() if r.fetched_at is not None]
    return max(stamps) if stamps else None


def portfolio_view(account_id: int) -> Dict[str, Any]:
    """``GET /accounts/{id}/portfolio``: the allocation page's payload for one account, as plain data.

    Pure: built by ``load_view_payload(read_only=True)`` (stored symbol facts, config row never created) and
    ``read_income_panel`` (stored ledger). ``NotFound`` when the account, or its allocation config, does not exist;
    a failed position fetch is ``BrokerUnreachable`` (never an empty book)."""
    from ..core.models import AccountDefinition
    from ..core.portfolio_allocation import PositionFetchFailed
    from ._common import jsonable, require_row, utcnow_iso
    from .accounts import _live_account, broker_read
    from .errors import BrokerUnreachable, NotFound

    with get_db() as session:
        require_row(session, AccountDefinition, account_id, "account")
    config = peek_allocation_config(account_id)
    if config is None:
        raise NotFound("no portfolio on this account", {"account_id": account_id})
    account = _live_account(account_id)
    div_age: Dict[str, Any] = {}

    def dividends_source():
        rows, age, cached = cached_dividends(account_id, account.get_dividends)
        div_age.update(age_s=age, cached=cached)
        return rows

    try:
        payload = broker_read(account_id, "the portfolio payload", load_view_payload, account_id,
                              config.valuation_mode, read_only=True, account=account, config=config,
                              dividends_source=dividends_source)
    except PositionFetchFailed as e:
        raise BrokerUnreachable(f"Account {account_id}: the broker positions fetch failed",
                                {"account_id": account_id}) from e
    _events, open_total, working_note = read_income_panel(account_id)
    facts, stats = payload["symbol_facts"], payload["symbol_stats"]
    book_complete = all(r.measurable or r.excluded for v in payload["views"] for r in v.rows)
    base = payload["base_notional"]
    # compute_base_notional values an unpriced managed holding at 0, so the base is only a lower bound then.
    base_known = base is not None and not payload["unpriced_managed_holdings"]
    labels = [_label_dict(v, facts, stats, book_complete=book_complete, base_known=base_known)
              for v in payload["views"]]
    payments = payload["dividend_payments"]
    out = {
        "as_of": utcnow_iso(), "account_id": account_id, "valuation_mode": payload["valuation_mode"],
        "allow_fractional": config.allow_fractional, "unallocated_pct": payload["unallocated_pct"],
        "base_notional": base if base_known else None,
        "base_notional_lower_bound": base if base is not None and not base_known else None,
        "unpriced_managed_holdings": payload["unpriced_managed_holdings"],
        "available_buying_power": payload["available_buying_power"],
        "account_value": payload["account_value"],
        "target_total_pct": sum(lb["target_pct"] for lb in labels),
        "labels": labels,
        "dividends": {"payments": payments, "age_s": div_age["age_s"], "cached": div_age["cached"],
                      "note": None if payments else
                      "The broker returned no dividend payments; a failed fetch reads the same, so the "
                      "with_dividends figures are null rather than equal to the plain P&L."},
        "stored": {"symbol_facts_fetched_at": _newest(facts), "symbol_stats_fetched_at": _newest(stats),
                   "note": "Symbol facts and stats are the stored rows (a GET never refreshes them); the page's "
                           "Refresh updates them."},
        "income": {"open_total": open_total, "working_note": working_note,
                   "synced_by": "the Portfolio Allocation page load / Refresh; the API never syncs the ledger"},
    }
    # Derived from the values actually being None (an all-None snapshot does not raise on some adapters).
    out["unmeasured"] = ([name for name in ("base_notional", "available_buying_power", "account_value")
                          if out[name] is None]
                         + [f"label:{lb['label']}" for lb in labels if not lb["complete"]])
    return jsonable(out)


_RUN_LIST_COLUMNS = ("id", "account_id", "mode", "scope_label", "base_notional", "available_buying_power",
                     "allow_fractional", "filled_buy_value", "filled_sell_value", "order_ids",
                     "income_consumed_at", "created_at")


def allocation_runs(account_id: int, *, limit: int, offset: int) -> Dict[str, Any]:
    """``GET /accounts/{id}/portfolio/runs``: allocation runs, newest first, paged in SQL over the list columns
    only (``plan_json`` and ``income_consumed_events`` are never loaded).

    Fill status is read from the stored order rows (DB only, no broker): ``working`` while any execution order can
    still fill or could not be valued, ``settled`` once all are final, ``no_orders`` for a run that placed none.
    ``filled_*`` are the figures the run recorded; ``measured_*`` are the same sums over the order rows NOW."""
    from ..core.models import AccountDefinition, PortfolioAllocationRun
    from ..core.portfolio_allocation import measure_filled_values
    from ._common import jsonable, page, require_row

    with get_db() as session:
        require_row(session, AccountDefinition, account_id, "account")
        columns = [getattr(PortfolioAllocationRun, name) for name in _RUN_LIST_COLUMNS]
        stmt = (select(*columns).where(PortfolioAllocationRun.account_id == account_id)
                .order_by(PortfolioAllocationRun.created_at.desc(), PortfolioAllocationRun.id.desc()))
        result = page(session, stmt, limit=limit, offset=offset, convert=lambda r: dict(r._mapping))
    from .errors import Unmeasurable
    run_orders: Dict[int, List[int]] = {}
    for run in result["items"]:
        try:
            run_orders[run["id"]] = [int(i) for i in (run["order_ids"] or [])]
        except (TypeError, ValueError) as e:
            raise Unmeasurable(f"Allocation run {run['id']}: its stored order_ids are corrupt "
                               f"({type(e).__name__}); its fills cannot be read",
                               {"run_id": run["id"], "account_id": account_id}) from e
    wanted = sorted({i for ids in run_orders.values() for i in ids})
    missing_ids: List[int] = []
    fills_by_order = {f.order_id: f for f in svc.collect_order_fills(account_id, wanted, on_missing=missing_ids.extend)}
    if missing_ids:
        logger.warning(f"API: account {account_id} allocation runs reference order rows that do not exist: "
                       f"{sorted(missing_ids)}")
    for run in result["items"]:
        ids = run_orders[run["id"]]
        fills = [fills_by_order[i] for i in dict.fromkeys(ids) if i in fills_by_order]
        totals = measure_filled_values(fills)
        run["order_ids"] = ids
        run["income_consumed"] = run["income_consumed_at"] is not None
        if not ids:
            status = "no_orders"
        elif not fills:
            status = "no_execution_orders"          # only protective TP/SL legs
        else:
            status = "settled" if totals.settled else "working"
        # A run that never finalised (income not consumed, orders not final) carries the zeros it was recorded
        # with before the fills were measured: those are not figures.
        if status == "working" and run["income_consumed_at"] is None:
            run["filled_buy_value"] = run["filled_sell_value"] = None
            run["filled_note"] = "The run has not finalised: the recorded filled values are not final."
        else:
            run["filled_note"] = None
        run["fill"] = {
            "status": status,
            "orders": len(ids), "execution_orders": len(fills),
            "working_order_ids": list(totals.working_order_ids),
            "unmeasurable_order_ids": list(totals.unmeasurable_order_ids),
            # A fill that cannot be valued makes the sums lower bounds: null, not a smaller number.
            "measured_buy_value": None if totals.unmeasurable_order_ids else totals.buy_value,
            "measured_sell_value": None if totals.unmeasurable_order_ids else totals.sell_value,
        }
    return jsonable(result)
