"""Account Growth figures, UI-free: quantity timeline, monthly realized income, value denominators, total account
growth, growth by label / by position, single-position series. The Account Growth tab and the management API's
``/accounts/{id}/growth*`` endpoints call these same functions (one function, two callers);
``tests/golden_overview/test_growth_golden.py`` holds them to what the inline tab code computed before the extraction.

Inputs are the broker rows as the account adapters return them (``get_balance_history``, ``get_dividends``,
``get_filled_trades``, ``get_positions``) and ``{symbol: {date: close}}`` price history; nothing here talks to a broker."""
from collections import defaultdict, deque

from ..core.utils import get_labels_by_symbol, normalize_symbol
from ..logger import logger
from .date_range import date_in_range, filter_dates


def signed_trade_qty(side, qty) -> float:
    """+qty for a BUY, -qty for a SELL, 0 for anything else. ONE predicate shared by the
    quantity timeline and the dividend forecast, so an unknown side is ignored by both."""
    if side == 'BUY':
        return float(qty)
    if side == 'SELL':
        return -float(qty)
    return 0.0


def build_qty_timeline(current_qty, filled_trades, all_dates, dividends=None):
    """
    Build a mapping of date -> qty by walking backwards from current_qty through filled trades
    and DRIP dividend reinvestments.

    For each filled trade, reverses its effect:
    - BUY trade: subtract qty (going backwards means we had fewer shares before this buy)
    - SELL trade: add qty (going backwards means we had more shares before this sell)
    For each DRIP dividend, reverses the reinvestment:
    - DRIP: subtract drip_quantity (going backwards means we had fewer shares before the DRIP)

    Args:
        current_qty: Current position quantity
        filled_trades: List of dicts with 'qty', 'side', 'date' keys, for a single symbol
        all_dates: Sorted list of date strings (YYYY-MM-DD) used in the chart
        dividends: Optional list of dividend dicts with 'date', 'drip_quantity' keys

    Returns:
        tuple: (qty_by_date dict {date_str: qty}, first_purchase_date_str or None)
    """
    if not all_dates:
        return {}, None

    # Convert trade dates to YYYY-MM-DD strings
    trade_events = []
    for t in filled_trades:
        date_val = t.get('date')
        if date_val:
            if hasattr(date_val, 'strftime'):
                date_str = date_val.strftime('%Y-%m-%d')
            else:
                date_str = str(date_val)[:10]
        else:
            continue
        trade_events.append({
            'date': date_str,
            'qty': float(t.get('qty', 0)),
            'side': t.get('side', ''),
        })

    # Build a map of net qty change per date (forward direction: buys add, sells subtract)
    daily_change = {}
    for te in trade_events:
        d = te['date']
        if d not in daily_change:
            daily_change[d] = 0.0
        daily_change[d] += signed_trade_qty(te['side'], te['qty'])

    # Add DRIP qty changes (DRIP adds shares, so it's like a BUY in the forward direction)
    if dividends:
        for div in dividends:
            drip_qty = div.get('drip_quantity')
            if not drip_qty:
                continue
            date_val = div.get('date')
            if not date_val:
                continue
            if hasattr(date_val, 'strftime'):
                date_str = date_val.strftime('%Y-%m-%d')
            else:
                date_str = str(date_val)[:10]
            if date_str not in daily_change:
                daily_change[date_str] = 0.0
            daily_change[date_str] += float(drip_qty)

    # Walk backwards from the last date to reconstruct qty at each date
    qty_by_date = {}
    qty = current_qty
    first_purchase_date = None

    for d in reversed(all_dates):
        qty_by_date[d] = qty
        # If there's a trade/DRIP on this date, reverse its effect to get qty before
        if d in daily_change:
            qty -= daily_change[d]  # Reverse the forward change
        # Track when qty reaches zero or below going backwards
        if qty <= 0.0001:
            first_purchase_date = d
            # Set all earlier dates to 0
            idx = all_dates.index(d)
            for earlier_d in all_dates[:idx]:
                qty_by_date[earlier_d] = 0
            break

    return qty_by_date, first_purchase_date


def build_symbol_info(positions, symbol_labels):
    """``{symbol: {qty, cost_basis, labels}}`` over ``positions`` (lots of one symbol summed); a symbol with no labels
    is 'Unlabeled'. ``symbol_labels`` is ``get_labels_by_symbol`` output (keys normalised)."""
    symbol_info = {}
    for pos in positions:
        labels = symbol_labels.get(normalize_symbol(pos.symbol)) or ['Unlabeled']
        if pos.symbol not in symbol_info:
            symbol_info[pos.symbol] = {'qty': 0, 'cost_basis': 0, 'labels': labels}
        symbol_info[pos.symbol]['qty'] += pos.qty
        symbol_info[pos.symbol]['cost_basis'] += pos.cost_basis
    return symbol_info


def chart_dates(historical_prices, range_start):
    """The x axis of the per-label / per-position charts: every date any symbol has a close for, from ``range_start``."""
    all_dates_set = set()
    for sym_prices in historical_prices.values():
        all_dates_set.update(sym_prices.keys())
    return filter_dates(sorted(all_dates_set), range_start)


def monthly_realized(all_filled_trades, all_dividends):
    """Aggregate realized closed-trade P&L and dividend income by month (and by label).

    Realized P&L is derived from broker FILLS via per-symbol FIFO matching (a SELL
    realizes against the oldest open BUY lots). This works for read-only broker
    accounts (e.g. TastyTrade) that have no local platform transactions, as well as
    trading accounts. The realizing month is the month of the SELL.

    Dividend income includes ALL dividends received (cash + reinvested) — this is the
    income earned that month. (The portfolio-value chart separately excludes
    reinvested dividends to avoid double-counting against share value.)

    Returns (months, monthly_income, monthly_by_label, labels, symbol_detail).
    """
    from collections import defaultdict, deque

    monthly_income = {}
    monthly_by_label = {}
    by_symbol = {}          # month -> {symbol: realized + dividends}
    labels_of = {}          # symbol -> its labels
    labels_set = set()

    # Group fills by symbol
    trades_by_sym = defaultdict(list)
    for t in (all_filled_trades or []):
        sym = t.get('symbol')
        if sym and t.get('date'):
            trades_by_sym[sym].append(t)

    # Labels are needed for both trade symbols and dividend symbols
    div_symbols = {d.get('symbol') for d in (all_dividends or []) if d.get('symbol')}
    symbols = sorted(set(trades_by_sym.keys()) | div_symbols)
    labels_by_symbol = get_labels_by_symbol(symbols) if symbols else {}

    # FIFO realized P&L per symbol
    for sym, trades in trades_by_sym.items():
        trades.sort(key=lambda x: str(x.get('date')))
        lots = deque()  # open BUY lots: [qty, price]
        for t in trades:
            side = (t.get('side') or '').upper()
            qty = abs(float(t.get('qty', 0) or 0))
            price = float(t.get('price', 0) or 0)
            if qty <= 0:
                continue
            if side == 'BUY':
                lots.append([qty, price])
            elif side == 'SELL':
                remaining, realized = qty, 0.0
                while remaining > 1e-9 and lots:
                    lot = lots[0]
                    take = min(remaining, lot[0])
                    realized += take * (price - lot[1])
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 1e-9:
                        lots.popleft()
                if abs(realized) < 1e-9 and remaining > 0:
                    continue  # sell with no matching buy in window — skip
                dt = t.get('date')
                month = dt.strftime('%Y-%m') if hasattr(dt, 'strftime') else str(dt)[:7]
                monthly_income.setdefault(month, {'pnl': 0.0, 'div': 0.0})['pnl'] += realized
                mlbl = monthly_by_label.setdefault(month, {})
                by_symbol_m = by_symbol.setdefault(month, {})
                by_symbol_m[sym] = by_symbol_m.get(sym, 0.0) + realized
                labels_of[sym] = labels_by_symbol.get(normalize_symbol(sym)) or ['Unlabeled']
                for lb in (labels_by_symbol.get(normalize_symbol(sym)) or ['Unlabeled']):
                    mlbl[lb] = mlbl.get(lb, 0.0) + realized
                    labels_set.add(lb)

    # Dividend income by month (all dividends — cash received + reinvested),
    # also attributed per-label by the dividend's symbol.
    for div in (all_dividends or []):
        dt = div.get('date')
        if not dt:
            continue
        month = dt.strftime('%Y-%m') if hasattr(dt, 'strftime') else str(dt)[:7]
        amount = float(div.get('amount', 0))
        monthly_income.setdefault(month, {'pnl': 0.0, 'div': 0.0})['div'] += amount
        mlbl = monthly_by_label.setdefault(month, {})
        dsym = div.get('symbol') or '?'
        by_symbol_m = by_symbol.setdefault(month, {})
        by_symbol_m[dsym] = by_symbol_m.get(dsym, 0.0) + amount
        labels_of[dsym] = labels_by_symbol.get(normalize_symbol(div.get('symbol'))) or ['Unlabeled']
        for lb in (labels_by_symbol.get(normalize_symbol(div.get('symbol'))) or ['Unlabeled']):
            mlbl[lb] = mlbl.get(lb, 0.0) + amount
            labels_set.add(lb)

    months = sorted(monthly_income.keys())
    for m in months:
        monthly_income[m]['pnl'] = round(monthly_income[m]['pnl'], 2)
        monthly_income[m]['div'] = round(monthly_income[m]['div'], 2)
    for m in monthly_by_label:
        for lb in monthly_by_label[m]:
            monthly_by_label[m][lb] = round(monthly_by_label[m][lb], 2)
    for m in by_symbol:
        by_symbol[m] = {k: round(v, 2) for k, v in by_symbol[m].items()}
    # Kept for the label chart: a symbol with several labels is in several segments, so the
    # chart's Total must count DISTINCT symbols (see chart_axes.shown_label_totals).
    symbol_detail = {'by_symbol': by_symbol, 'labels_of': labels_of}
    return months, monthly_income, monthly_by_label, sorted(labels_set), symbol_detail


def value_denominators(all_balance_history, all_positions, historical_prices, all_filled_trades, all_dividends, months):
    """Account value at each month-end, for expressing income as % growth.

    Returns (monthly_global, monthly_label):
      monthly_global: {month: account NLV at month-end} (from balance history)
      monthly_label:  {month: {label: per-label market value at month-end}}
    A month absent from a dict means its value is unavailable (caller shows no %).
    """
    monthly_global = {}
    monthly_label = {}

    # Global NLV by month-end: per-account forward-fill, summed.
    per_acc = {}
    bdates = set()
    for e in (all_balance_history or []):
        dv = e.get('date')
        if not dv:
            continue
        ds = dv.strftime('%Y-%m-%d') if hasattr(dv, 'strftime') else str(dv)[:10]
        per_acc.setdefault(e.get('account_id', 0), {})[ds] = float(e.get('net_liquidating_value', 0) or 0)
        bdates.add(ds)
    bdates = sorted(bdates)
    if bdates:
        ff = {a: 0.0 for a in per_acc}
        global_by_date = {}
        for ds in bdates:
            for a in per_acc:
                if ds in per_acc[a]:
                    ff[a] = per_acc[a][ds]
            global_by_date[ds] = sum(ff.values())
        for m in months:
            in_m = [ds for ds in bdates if ds[:7] == m]
            before = [ds for ds in bdates if ds[:7] <= m]
            pick = in_m[-1] if in_m else (before[-1] if before else None)
            if pick:
                monthly_global[m] = round(global_by_date[pick], 2)

    # Per-label market value by month-end (current positions x qty timeline x prices).
    if historical_prices and all_positions:
        symbol_labels = get_labels_by_symbol([p.symbol for p in all_positions]) or {}
        symbol_info = {}
        for pos in all_positions:
            lbls = symbol_labels.get(normalize_symbol(pos.symbol)) or ['Unlabeled']
            si = symbol_info.setdefault(pos.symbol, {'qty': 0.0, 'labels': lbls})
            si['qty'] += float(getattr(pos, 'qty', 0) or 0)
        pdates = sorted({d for sp in historical_prices.values() for d in sp.keys()})
        if pdates:
            sym_qty = {}
            for sym, si in symbol_info.items():
                strades = [t for t in (all_filled_trades or []) if t.get('symbol') == sym]
                sdivs = [d for d in (all_dividends or []) if d.get('symbol') == sym]
                qbd, _ = build_qty_timeline(si['qty'], strades, pdates, dividends=sdivs)
                sym_qty[sym] = qbd
            label_val_by_date = {}
            last_price = {sym: None for sym in symbol_info}
            for d in pdates:
                day = {}
                for sym, si in symbol_info.items():
                    sp = historical_prices.get(sym, {})
                    if d in sp:
                        last_price[sym] = sp[d]
                    price = last_price[sym]
                    if price is None:
                        continue
                    q = sym_qty[sym].get(d, 0)
                    if q <= 0:
                        continue
                    val = q * price
                    for lb in si['labels']:
                        day[lb] = day.get(lb, 0.0) + val
                label_val_by_date[d] = day
            for m in months:
                in_m = [d for d in pdates if d[:7] == m]
                before = [d for d in pdates if d[:7] <= m]
                pick = in_m[-1] if in_m else (before[-1] if before else None)
                if pick:
                    monthly_label[m] = {lb: round(v, 2) for lb, v in label_val_by_date[pick].items()}

    return monthly_global, monthly_label


def total_growth(balance_history, dividends, all_filled_trades=None, range_start=None):
    """Total account growth series: per-account forward-filled net liquidation value and cash, cumulative dividends,
    invested capital (external flows detected per account) and P&L %. ``None`` when the range holds neither balance
    history nor dividends. TIME RANGE: the inputs are cut at ``range_start``, so everything cumulative restarts there."""
    balance_history = [e for e in (balance_history or [])
                       if date_in_range(e.get('date'), range_start)]
    dividends = [d for d in (dividends or [])
                 if date_in_range(d.get('date'), range_start)]
    if not balance_history and not dividends:
        return None

    # Aggregate balance history per date across accounts.
    # Each entry has 'account_id' and 'date'. For each date, sum
    # the NLV of all accounts using per-account forward-fill.
    balance_dates_set = set()
    per_account_data = {}  # account_id -> {date_str: nlv}
    per_account_cash = {}  # account_id -> {date_str: cash_balance}
    per_account_daily_pl = {}  # account_id -> {date_str: daily_pl}
    accounts_with_daily_pl = set()
    if balance_history:
        for entry in balance_history:
            date_val = entry.get('date')
            if not date_val:
                continue
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)
            acc_id = entry.get('account_id', 0)
            balance_dates_set.add(date_str)
            if acc_id not in per_account_data:
                per_account_data[acc_id] = {}
                per_account_cash[acc_id] = {}
            per_account_data[acc_id][date_str] = float(entry.get('net_liquidating_value', 0))
            per_account_cash[acc_id][date_str] = float(entry.get('cash_balance', 0))
            pl = entry.get('profit_loss')
            if pl is not None:
                accounts_with_daily_pl.add(acc_id)
                if acc_id not in per_account_daily_pl:
                    per_account_daily_pl[acc_id] = {}
                per_account_daily_pl[acc_id][date_str] = float(pl)

    # Prepare cumulative dividend series
    div_dates_set = set()
    div_by_date = {}  # date_str -> total amount
    if dividends:
        sorted_divs = sorted(dividends, key=lambda x: str(x.get('date', '')))
        cumulative = 0
        for entry in sorted_divs:
            # Show the full dividend amount (cash + reinvested) — this is an
            # informational line, separate from Portfolio Value, so it is not
            # double-counted. (Reinvested dividends are counted in invested.)
            date_val = entry.get('date')
            if date_val:
                date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)
                cumulative += float(entry.get('amount', 0))
                div_dates_set.add(date_str)
                div_by_date[date_str] = round(cumulative, 2)

    # Merge all dates for x-axis
    all_dates = sorted(balance_dates_set | div_dates_set)

    # Build aligned balance series: for each date, sum per-account
    # NLV with forward-fill per account.
    aligned_balance = []
    last_per_account = {acc_id: 0.0 for acc_id in per_account_data}
    for d in all_dates:
        for acc_id, acc_data in per_account_data.items():
            if d in acc_data:
                last_per_account[acc_id] = acc_data[d]
        aligned_balance.append(round(sum(last_per_account.values()), 2))

    # Build aligned cash series with forward-fill per account
    aligned_cash = []
    last_per_account_cash = {acc_id: 0.0 for acc_id in per_account_cash}
    for d in all_dates:
        for acc_id, acc_data in per_account_cash.items():
            if d in acc_data:
                last_per_account_cash[acc_id] = acc_data[d]
        aligned_cash.append(round(sum(last_per_account_cash.values()), 2))

    # Build aligned dividend series with forward-fill
    aligned_div = []
    last_div = 0
    for d in all_dates:
        if d in div_by_date:
            last_div = div_by_date[d]
        aligned_div.append(last_div)

    # Compute P/L % per account then aggregate.
    # For accounts with daily_pl (e.g. Alpaca): external_flow = ΔNLV - daily_pl
    # For accounts with cash_balance (e.g. TastyTrade): external_flow = Δcash - expected_trade/div_cash

    # Build per-account trade cash impact (for cash-based accounts)
    per_acc_trade_cash = {}  # acc_id -> {date_str: cash_impact}
    for t in (all_filled_trades or []):
        date_val = t.get('date')
        if not date_val:
            continue
        d = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
        a_id = t.get('account_id', 0)
        tqty = float(t.get('qty', 0))
        tprice = float(t.get('price', 0))
        side = t.get('side', '')
        if a_id not in per_acc_trade_cash:
            per_acc_trade_cash[a_id] = {}
        if side == 'BUY':
            per_acc_trade_cash[a_id][d] = per_acc_trade_cash[a_id].get(d, 0.0) - tqty * tprice
        elif side == 'SELL':
            per_acc_trade_cash[a_id][d] = per_acc_trade_cash[a_id].get(d, 0.0) + tqty * tprice

    # Build per-account dividend cash impact (for cash-based accounts)
    per_acc_div_cash = {}  # acc_id -> {date_str: cash_impact}
    for div in (dividends or []):
        date_val = div.get('date')
        if not date_val:
            continue
        d = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
        a_id = div.get('account_id', 0)
        amount = float(div.get('amount', 0))   # already net of tax
        cash_in = amount
        drip_qty = div.get('drip_quantity')
        drip_price = div.get('drip_price')
        cash_out = float(drip_qty) * float(drip_price) if drip_qty and drip_price else 0.0
        if a_id not in per_acc_div_cash:
            per_acc_div_cash[a_id] = {}
        per_acc_div_cash[a_id][d] = per_acc_div_cash[a_id].get(d, 0.0) + cash_in - cash_out

    pnl_pct_data = []
    aligned_invested = []
    per_acc_invested = {}  # acc_id -> invested_capital
    per_acc_prev_nlv = {}  # acc_id -> previous NLV (forward-filled)
    per_acc_prev_cash = {}  # acc_id -> previous cash (forward-filled)
    ff_nlv = {acc_id: 0.0 for acc_id in per_account_data}
    ff_cash = {acc_id: 0.0 for acc_id in per_account_cash}

    for i, d in enumerate(all_dates):
        total_nlv = 0.0
        total_invested = 0.0

        for acc_id in per_account_data:
            if d in per_account_data[acc_id]:
                ff_nlv[acc_id] = per_account_data[acc_id][d]
            acc_nlv = ff_nlv[acc_id]

            if acc_id in per_account_cash and d in per_account_cash[acc_id]:
                ff_cash[acc_id] = per_account_cash[acc_id][d]
            acc_cash = ff_cash.get(acc_id, 0.0)

            if acc_id not in per_acc_invested:
                per_acc_invested[acc_id] = acc_nlv
                per_acc_prev_nlv[acc_id] = acc_nlv
                per_acc_prev_cash[acc_id] = acc_cash
            else:
                if acc_id in accounts_with_daily_pl:
                    delta_nlv = acc_nlv - per_acc_prev_nlv[acc_id]
                    daily_pl = per_account_daily_pl.get(acc_id, {}).get(d, 0.0)
                    external_flow = delta_nlv - daily_pl
                else:
                    actual_cash_change = acc_cash - per_acc_prev_cash[acc_id]
                    trade_cash = per_acc_trade_cash.get(acc_id, {}).get(d, 0.0)
                    div_cash = per_acc_div_cash.get(acc_id, {}).get(d, 0.0)
                    external_flow = actual_cash_change - trade_cash - div_cash

                if abs(external_flow) > 1.0:
                    per_acc_invested[acc_id] += external_flow

                per_acc_prev_nlv[acc_id] = acc_nlv
                per_acc_prev_cash[acc_id] = acc_cash

            total_nlv += acc_nlv
            total_invested += per_acc_invested[acc_id]

        aligned_invested.append(round(total_invested, 2))

        if total_invested > 0:
            pnl = total_nlv - total_invested
            pnl_pct = round(pnl / total_invested * 100, 2)
            pnl_pct_data.append(pnl_pct)
        else:
            pnl_pct_data.append(None)
    return {'all_dates': all_dates, 'aligned_balance': aligned_balance, 'aligned_cash': aligned_cash,
            'aligned_div': aligned_div, 'aligned_invested': aligned_invested, 'pnl_pct_data': pnl_pct_data}


def label_growth(symbol_info, all_dates, historical_prices, all_filled_trades, all_dividends):
    """Growth by label: per label the daily holdings value, cumulative dividends (all / cash only) and cumulative
    invested capital, aligned with ``all_dates``. ``symbol_info`` is ``{symbol: {qty, cost_basis, labels}}``."""
    # Compute daily market value per label
    all_labels_set = set()
    for info in symbol_info.values():
        all_labels_set.update(info['labels'])
    all_labels = sorted(all_labels_set)

    # label -> [daily_value] aligned with all_dates
    label_daily_values = {label: [0.0] * len(all_dates) for label in all_labels}

    # Build per-symbol qty timelines using filled trades and DRIP dividends
    sym_qty_timelines = {}
    for sym, info in symbol_info.items():
        sym_trades = [t for t in (all_filled_trades or []) if t.get('symbol') == sym]
        sym_divs = [d for d in (all_dividends or []) if d.get('symbol') == sym]
        qty_by_date, _ = build_qty_timeline(info['qty'], sym_trades, all_dates, dividends=sym_divs)
        sym_qty_timelines[sym] = qty_by_date

    for sym, info in symbol_info.items():
        sym_prices = historical_prices.get(sym, {})
        if not sym_prices:
            continue
        qty_timeline = sym_qty_timelines.get(sym, {})
        for i, d in enumerate(all_dates):
            qty_at_date = qty_timeline.get(d, 0)
            if qty_at_date <= 0:
                continue
            price = sym_prices.get(d)
            if price is not None:
                val = qty_at_date * price
                for label in info['labels']:
                    label_daily_values[label][i] += val
            else:
                # Forward-fill: use previous value for this symbol
                last_price = None
                for prev_d in all_dates[:i]:
                    if prev_d in sym_prices:
                        last_price = sym_prices[prev_d]
                if last_price is not None:
                    val = qty_at_date * last_price
                    for label in info['labels']:
                        label_daily_values[label][i] += val

    # Build two cumulative dividend series per label:
    # - label_cum_divs: ALL dividends (DRIP + cash) — shown as the display line
    # - label_cum_cash_divs: cash dividends only — used for the Total line
    #   (DRIP shares are already reflected in qty * price via _build_qty_timeline,
    #    so only cash dividends should be added on top of the value)
    label_cum_divs = {label: [0.0] * len(all_dates) for label in all_labels}
    label_cum_cash_divs = {label: [0.0] * len(all_dates) for label in all_labels}
    if all_dividends:
        for div in all_dividends:
            sym = div.get('symbol')
            if not sym or sym not in symbol_info:
                continue
            date_val = div.get('date')
            if not date_val:
                continue
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)
            if date_str not in all_dates:
                continue
            idx = all_dates.index(date_str)
            amount = float(div.get('amount', 0))
            is_drip = bool(div.get('drip_quantity'))
            labels_for_sym = symbol_info[sym]['labels']
            for label in labels_for_sym:
                label_cum_divs[label][idx] += amount
                if not is_drip:
                    label_cum_cash_divs[label][idx] += amount

        # Convert both to cumulative running totals
        for label in all_labels:
            for i in range(1, len(all_dates)):
                label_cum_divs[label][i] += label_cum_divs[label][i - 1]
                label_cum_cash_divs[label][i] += label_cum_cash_divs[label][i - 1]
            label_cum_divs[label] = [round(v, 2) for v in label_cum_divs[label]]
            label_cum_cash_divs[label] = [round(v, 2) for v in label_cum_cash_divs[label]]

    has_any_dividends = any(label_cum_divs[l][-1] > 0 for l in all_labels) if all_dates else False

    # Build cumulative invested per label per date:
    # base = qty_at_first_date * (cost_basis / current_qty) for each symbol,
    # then add/subtract actual BUY/SELL trade costs within the chart window,
    # plus DRIP reinvested money (drip_qty * drip_price) — reinvested dividends
    # now count as invested capital.
    label_cum_invested = {label: [0.0] * len(all_dates) for label in all_labels}
    for sym, info in symbol_info.items():
        current_qty = info['qty']
        cost_basis = info['cost_basis']
        if current_qty <= 0:
            continue
        cost_per_share = cost_basis / current_qty
        qty_timeline = sym_qty_timelines.get(sym, {})
        # Base invested at first date
        start_qty = qty_timeline.get(all_dates[0], 0) if all_dates else 0
        base_invested = start_qty * cost_per_share
        # Build a delta array: trade costs that fall within all_dates
        date_to_idx = {d: i for i, d in enumerate(all_dates)}
        trade_deltas = [0.0] * len(all_dates)
        for t in (all_filled_trades or []):
            if t.get('symbol') != sym:
                continue
            date_val = t.get('date')
            if not date_val:
                continue
            t_date = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            if t_date not in date_to_idx:
                continue
            idx = date_to_idx[t_date]
            cost = float(t.get('qty', 0)) * float(t.get('price', 0))
            if t.get('side') == 'BUY':
                trade_deltas[idx] += cost
            elif t.get('side') == 'SELL':
                trade_deltas[idx] -= cost
        # DRIP reinvested money counts as invested (detected from dividend logs)
        for div in (all_dividends or []):
            if div.get('symbol') != sym:
                continue
            drip_qty = div.get('drip_quantity')
            drip_price = div.get('drip_price')
            if not drip_qty or not drip_price:
                continue
            date_val = div.get('date')
            if not date_val:
                continue
            d_date = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            if d_date not in date_to_idx:
                continue
            trade_deltas[date_to_idx[d_date]] += float(drip_qty) * float(drip_price)
        # Cumulate
        running = base_invested
        for i in range(len(all_dates)):
            running += trade_deltas[i]
            for label in info['labels']:
                label_cum_invested[label][i] += running
    # Round
    for label in all_labels:
        label_cum_invested[label] = [round(v, 2) for v in label_cum_invested[label]]

    has_any_invested = any(label_cum_invested[l][-1] > 0 for l in all_labels) if all_dates else False
    return {'all_dates': all_dates, 'all_labels': all_labels, 'label_daily_values': label_daily_values,
            'label_cum_divs': label_cum_divs, 'label_cum_cash_divs': label_cum_cash_divs,
            'label_cum_invested': label_cum_invested, 'has_any_dividends': has_any_dividends,
            'has_any_invested': has_any_invested}


def position_growth(symbol_info, all_dates, historical_prices, all_filled_trades, all_dividends):
    """Growth by position: per symbol the daily holdings value, cumulative dividends (all / cash only) and cumulative
    invested capital (a SELL removes the cost of the shares sold at the running average, never the proceeds)."""
    # Build per-symbol qty timelines
    sym_qty_timelines = {}
    for sym, info in symbol_info.items():
        sym_trades = [t for t in (all_filled_trades or []) if t.get('symbol') == sym]
        sym_divs = [d for d in (all_dividends or []) if d.get('symbol') == sym]
        qty_by_date, _ = build_qty_timeline(info['qty'], sym_trades, all_dates, dividends=sym_divs)
        sym_qty_timelines[sym] = qty_by_date

    # Build per-symbol daily market values
    sym_daily_values = {}
    for sym, info in symbol_info.items():
        sym_prices = historical_prices.get(sym, {})
        if not sym_prices:
            continue
        qty_timeline = sym_qty_timelines.get(sym, {})
        values = [0.0] * len(all_dates)
        for i, d in enumerate(all_dates):
            qty_at_date = qty_timeline.get(d, 0)
            if qty_at_date <= 0:
                continue
            price = sym_prices.get(d)
            if price is not None:
                values[i] = qty_at_date * price
            else:
                last_price = None
                for prev_d in all_dates[:i]:
                    if prev_d in sym_prices:
                        last_price = sym_prices[prev_d]
                if last_price is not None:
                    values[i] = qty_at_date * last_price
        sym_daily_values[sym] = values

    # Build two per-symbol cumulative dividend series:
    # - sym_cum_divs: ALL dividends (DRIP + cash) — shown as the display line
    # - sym_cum_cash_divs: cash dividends only — used for the Total line
    sym_cum_divs = {sym: [0.0] * len(all_dates) for sym in symbol_info}
    sym_cum_cash_divs = {sym: [0.0] * len(all_dates) for sym in symbol_info}
    if all_dividends:
        for div in all_dividends:
            sym = div.get('symbol')
            if not sym or sym not in symbol_info:
                continue
            date_val = div.get('date')
            if not date_val:
                continue
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)
            if date_str not in all_dates:
                continue
            idx = all_dates.index(date_str)
            amount = float(div.get('amount', 0))
            is_drip = bool(div.get('drip_quantity'))
            sym_cum_divs[sym][idx] += amount
            if not is_drip:
                sym_cum_cash_divs[sym][idx] += amount
        for sym in symbol_info:
            for i in range(1, len(all_dates)):
                sym_cum_divs[sym][i] += sym_cum_divs[sym][i - 1]
                sym_cum_cash_divs[sym][i] += sym_cum_cash_divs[sym][i - 1]
            sym_cum_divs[sym] = [round(v, 2) for v in sym_cum_divs[sym]]
            sym_cum_cash_divs[sym] = [round(v, 2) for v in sym_cum_cash_divs[sym]]

    # Build per-symbol cumulative invested amounts:
    # base at first date = qty_at_first_date * (cost_basis / current_qty),
    # then add BUY costs / subtract SELL proceeds within the chart window.
    sym_cum_invested = {sym: [0.0] * len(all_dates) for sym in symbol_info}
    date_to_idx_pos = {d: i for i, d in enumerate(all_dates)}
    for sym, info in symbol_info.items():
        current_qty = info['qty']
        cost_basis = info['cost_basis']
        if current_qty <= 0:
            continue
        cost_per_share = cost_basis / current_qty
        qty_timeline = sym_qty_timelines.get(sym, {})
        start_qty = qty_timeline.get(all_dates[0], 0) if all_dates else 0
        base_invested = start_qty * cost_per_share
        sym_trades = [t for t in (all_filled_trades or []) if t.get('symbol') == sym]
        # A SELL REDUCES COST BASIS BY THE COST OF THE SHARES SOLD, at the running
        # average — NOT by the sale proceeds. Subtracting proceeds from a COST
        # accumulator mixes two different quantities: a profitable exit removes more
        # than was ever put in, and a full liquidation at a gain drives "invested"
        # NEGATIVE. That is what a margin call did here, and in % mode the resulting
        # `value / -8 * 100` was a finite -1,200% that rescaled the chart and
        # flattened every other series onto the axis.
        #
        # `_render_position_growth_chart_from_data` has always done it this way
        # ("For sells, reduce cost at current running avg (not sale price)"); this
        # chart is the copy that did not.
        events_by_idx = {}
        for t in sym_trades:
            date_val = t.get('date')
            if not date_val:
                continue
            t_date = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            if t_date not in date_to_idx_pos:
                continue
            events_by_idx.setdefault(date_to_idx_pos[t_date], []).append(t)
        running = base_invested
        running_shares = float(start_qty)
        for i in range(len(all_dates)):
            for t in events_by_idx.get(i, []):
                raw_qty, raw_price = t.get('qty'), t.get('price')
                if raw_qty is None or raw_price is None:
                    # A fill with no size or no price cannot be applied to a cost
                    # basis. Reading either as 0 would silently book a free trade —
                    # a BUY that adds shares at no cost, or a SELL of nothing — and
                    # every later average would carry the error.
                    logger.warning(
                        f"Skipping {sym} trade with no qty/price in the invested "
                        f"series: qty={raw_qty!r} price={raw_price!r}")
                    continue
                qty = float(raw_qty)
                price = float(raw_price)
                if t.get('side') == 'BUY':
                    running += qty * price
                    running_shares += qty
                elif t.get('side') == 'SELL':
                    # Clamped to what is on file: a sell of more shares than this
                    # reconstruction knows about is a gap in the trade history, and
                    # honouring it literally would push cost below zero — the very
                    # state this rewrite exists to make unreachable.
                    if running_shares > 0:
                        sold = min(qty, running_shares)
                        running -= sold * (running / running_shares)
                        running_shares -= sold
            sym_cum_invested[sym][i] = round(running, 2)
    return {'all_dates': all_dates, 'sym_daily_values': sym_daily_values, 'sym_cum_divs': sym_cum_divs,
            'sym_cum_cash_divs': sym_cum_cash_divs, 'sym_cum_invested': sym_cum_invested}


def single_position_growth(dividends, hist_prices, current_qty, filled_trades, range_start=None):
    """One position's series from pre-fetched data: value, invested (running cost basis), P&L % against the
    running average cost, cumulative dividends and DRIP shares / dollars. ``None`` when the range holds neither
    prices nor dividends. DRIP is detected purely from the dividend rows."""
    start = range_start
    hist_prices = {d: p for d, p in (hist_prices or {}).items() if date_in_range(d, start)}
    # The FULL list still feeds the cost basis below: a DRIP share bought before the
    # range start is part of what the position cost, whatever window is drawn.
    dividends_all = list(dividends or [])
    dividends = [dv for dv in dividends_all if date_in_range(dv.get('date'), start)]
    has_dividends = bool(dividends)
    has_prices = bool(hist_prices)
    if not has_dividends and not has_prices:
        return None


    # Build dividend data
    div_map = {}  # date -> {cumulative_div, cumulative_drip, cumulative_drip_cost}
    if has_dividends:
        sorted_divs = sorted(dividends, key=lambda x: str(x.get('date', '')))
        cumulative = 0
        cumulative_drip = 0
        cumulative_drip_cost = 0.0  # reinvested dollars = drip_qty * drip_price
        for div in sorted_divs:
            date_val = div.get('date')
            if date_val:
                date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)
                cumulative += float(div.get('amount', 0))
                drip_qty = div.get('drip_quantity')
                drip_price = div.get('drip_price')
                if drip_qty:
                    cumulative_drip += float(drip_qty)
                    if drip_price:
                        cumulative_drip_cost += float(drip_qty) * float(drip_price)
                div_map[date_str] = {
                    'cumulative_div': round(cumulative, 2),
                    'cumulative_drip': round(cumulative_drip, 4),
                    'cumulative_drip_cost': round(cumulative_drip_cost, 2),
                }

    # Merge all dates from prices and dividends
    all_dates = sorted(set(list(hist_prices.keys()) + list(div_map.keys())))

    # Build qty timeline using filled trades and DRIP dividends
    qty_by_date, first_purchase_date = build_qty_timeline(
        current_qty, filled_trades or [], all_dates, dividends=dividends
    )

    # Build running cost basis from trades and DRIP events for accurate historical P&L.
    # Using the static avg_entry_price is wrong because it includes DRIP buys that
    # happened AFTER the initial purchase, making early dates look negative.
    cost_events = {}  # date_str -> {'cost_delta': float, 'qty_delta': float}
    for t in (filled_trades or []):
        date_val = t.get('date')
        if not date_val:
            continue
        d = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
        if d not in cost_events:
            cost_events[d] = {'cost_delta': 0.0, 'qty_delta': 0.0}
        tqty = float(t.get('qty', 0))
        tprice = float(t.get('price', 0))
        side = t.get('side', '')
        if side == 'BUY':
            cost_events[d]['cost_delta'] += tqty * tprice
            cost_events[d]['qty_delta'] += tqty
        elif side == 'SELL':
            cost_events[d]['qty_delta'] -= tqty
            # On sell, reduce cost proportionally (at current avg)
            cost_events[d]['cost_delta'] -= tqty * tprice

    if dividends_all:
        for div in dividends_all:
            drip_qty = div.get('drip_quantity')
            drip_price = div.get('drip_price')
            if not drip_qty or not drip_price:
                continue
            date_val = div.get('date')
            if not date_val:
                continue
            d = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            if d not in cost_events:
                cost_events[d] = {'cost_delta': 0.0, 'qty_delta': 0.0}
            cost_events[d]['cost_delta'] += float(drip_qty) * float(drip_price)
            cost_events[d]['qty_delta'] += float(drip_qty)

    # Build aligned series with forward-fill
    # Value = all shares (qty including DRIP) × price — matches broker Net Liq.
    #   DRIP reinvested shares now count as invested, so we show a single value line
    #   (no separate "purchased only" line) plus a dedicated "DRIP Invested" $ line.
    # P&L % = ((price - running_avg) / running_avg) * 100 — evolves with cost basis
    values = []
    #: Cumulative COST of the shares held, per date -- the "amount purchased" line
    #: the account-growth chart carries. It is ``running_cost``, which this loop
    #: already maintains for the P&L%, sampled once per date rather than recomputed:
    #: a second accumulation would be a second definition of cost basis, and the two
    #: would disagree the first time the sell-side handling changed on one of them.
    invested_data = []
    pnl_pct_data = []
    cumulative_divs = []
    drip_quantities = []
    drip_invested = []  # cumulative reinvested dollars (drip_qty × drip_price)
    last_price = None
    last_div = 0
    last_drip = 0
    last_drip_cost = 0.0
    running_cost = 0.0
    running_shares = 0.0

    def apply_cost_event(evt):
        nonlocal running_cost, running_shares
        if evt['qty_delta'] < 0 and running_shares > 0:
            # For sells, reduce cost at current running avg (not sale price)
            running_avg = running_cost / running_shares
            running_cost += evt['qty_delta'] * running_avg
            running_shares += evt['qty_delta']
        else:
            running_cost += evt['cost_delta']
            running_shares += evt['qty_delta']

    # Everything bought or sold BEFORE the first drawn date is already in the running
    # cost basis when the window opens: without this, a position bought before the
    # range start showed P&L 0% and an Invested line of just the in-window DRIP cost.
    for d0 in sorted(k for k in cost_events if k < all_dates[0]):
        apply_cost_event(cost_events[d0])

    for d in all_dates:
        if d in hist_prices:
            last_price = hist_prices[d]
        # Update dividend/drip tracking before position calc
        if d in div_map:
            last_div = div_map[d]['cumulative_div']
            last_drip = div_map[d]['cumulative_drip']
            last_drip_cost = div_map[d]['cumulative_drip_cost']
        cumulative_divs.append(last_div)
        drip_quantities.append(last_drip)
        drip_invested.append(last_drip_cost)

        # Update running cost basis
        if d in cost_events:
            apply_cost_event(cost_events[d])

        qty_at_date = qty_by_date.get(d, 0)
        # ``None`` before the first purchase, so the line STARTS at the first buy
        # instead of running along zero from the left edge -- the same treatment the
        # value line gets, and for the same reason: nothing was held there.
        invested_data.append(round(running_cost, 2) if running_shares > 0 else None)
        if qty_at_date > 0 and last_price:
            values.append(round(qty_at_date * last_price, 2))
            if running_shares > 0:
                running_avg = running_cost / running_shares
                pnl_pct = round((last_price - running_avg) / running_avg * 100, 2)
                pnl_pct_data.append(pnl_pct)
            else:
                pnl_pct_data.append(None)
        else:
            values.append(None)
            pnl_pct_data.append(None)
    return {'all_dates': all_dates, 'values': values, 'invested_data': invested_data,
            'pnl_pct_data': pnl_pct_data, 'cumulative_divs': cumulative_divs,
            'drip_quantities': drip_quantities, 'drip_invested': drip_invested}
