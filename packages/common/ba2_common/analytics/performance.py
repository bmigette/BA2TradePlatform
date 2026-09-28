"""Trade-performance metrics (moved from ba2_trade_platform/ui/components/performance_charts.py
and the per-expert aggregation in ui/pages/performance.py, 2026-09, site plan P0a).
Pure: operates on transaction-like objects, never touches a DB."""
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from ba2_common.core.utils import calculate_transaction_pnl


def calculate_sharpe_ratio(returns: List[float], risk_free_rate: float = 0.02) -> Optional[float]:
    """
    Calculate annualized Sharpe ratio.
    
    Args:
        returns: List of daily returns
        risk_free_rate: Annual risk-free rate (default 2%)
    
    Returns:
        Sharpe ratio or None if insufficient data
    """
    if len(returns) < 30:
        return None
    
    returns_array = np.array(returns)
    mean_return = np.mean(returns_array)
    std_return = np.std(returns_array, ddof=1)
    
    if std_return == 0:
        return 0.0
    
    # Annualize assuming 252 trading days
    daily_rf = risk_free_rate / 252
    sharpe = (mean_return - daily_rf) / std_return * np.sqrt(252)
    
    return sharpe


def calculate_win_loss_ratio(transactions: List[Dict[str, Any]]) -> Tuple[float, int, int]:
    """
    Calculate win/loss ratio and counts.
    
    Args:
        transactions: List of transaction dictionaries with 'pnl' field
    
    Returns:
        Tuple of (win_rate_percentage, win_count, loss_count)
    """
    wins = sum(1 for t in transactions if t.get('pnl', 0) > 0)
    losses = sum(1 for t in transactions if t.get('pnl', 0) < 0)
    total = wins + losses
    
    win_rate = (wins / total * 100) if total > 0 else 0.0
    
    return win_rate, wins, losses


def calculate_max_drawdown(equity_curve: List[float]) -> float:
    """
    Calculate maximum drawdown from equity curve.
    
    Args:
        equity_curve: List of equity values over time
    
    Returns:
        Maximum drawdown as a percentage
    """
    if not equity_curve or len(equity_curve) < 2:
        return 0.0
    
    equity_array = np.array(equity_curve)
    running_max = np.maximum.accumulate(equity_array)
    drawdown = (equity_array - running_max) / running_max * 100
    
    return abs(np.min(drawdown))


def max_drawdown_from_pnl(pnls: List[float]) -> Tuple[float, Optional[float]]:
    """``(worst peak-to-trough fall in dollars, that fall as a % of the peak)``.

    Takes a sequence of per-trade P&Ls IN CLOSE ORDER and walks the cumulative curve
    itself, rather than handing that curve to ``calculate_max_drawdown``. The existing
    function divides by the running maximum, which is exactly 0 at the start of any
    cumulative P&L series and stays 0 or negative for an expert that is down on its
    first trades -- so the first drawdown it meets is a divide-by-zero and every later
    one is measured against a negative base.

    THE PERCENTAGE IS ``None`` WHEN THERE IS NO POSITIVE PEAK TO MEASURE AGAINST. An
    expert whose cumulative P&L has never been above zero has a real dollar drawdown and
    no meaningful percentage one: "down 100% of a peak of -$40" is not a fact. The
    dollar figure is always available, which is why both are returned.

    A drawdown of 0.0 with a positive peak means the curve only ever rose -- a genuine
    measurement, and distinct from the ``None`` above.
    """
    if not pnls:
        return 0.0, None
    cumulative = 0.0
    peak = 0.0
    worst_fall = 0.0
    worst_pct: Optional[float] = None
    for pnl in pnls:
        cumulative += float(pnl)
        if cumulative > peak:
            peak = cumulative
        fall = peak - cumulative
        if fall > worst_fall:
            worst_fall = fall
        # Measured at each point against the peak IN FORCE THEN, not against the final
        # peak: a 30% fall early on does not become a 5% one because the curve tripled
        # afterwards.
        if peak > 0 and fall > 0:
            pct = fall / peak * 100.0
            if worst_pct is None or pct > worst_pct:
                worst_pct = pct
    if worst_pct is None and peak > 0:
        worst_pct = 0.0
    return round(worst_fall, 2), (None if worst_pct is None else round(worst_pct, 2))


def calculate_profit_factor(winning_trades: List[float], losing_trades: List[float]) -> Optional[float]:
    """
    Calculate profit factor (gross profit / gross loss).
    
    Args:
        winning_trades: List of winning trade P&Ls
        losing_trades: List of losing trade P&Ls
    
    Returns:
        Profit factor or None if no losing trades
    """
    gross_profit = sum(winning_trades) if winning_trades else 0
    gross_loss = abs(sum(losing_trades)) if losing_trades else 0
    
    if gross_loss == 0:
        return None if gross_profit == 0 else float('inf')
    
    return gross_profit / gross_loss


def expert_performance(txns: Iterable[Any]) -> Dict[str, Any]:
    """Per-expert metrics exactly as the live Performance page computes them (one expert's
    transactions in, one metrics dict out). Order-sensitive pieces (drawdown) sort by close date
    themselves; everything else follows the given order, as the page always did."""
    txns = list(txns)
    durations = []
    for txn in txns:
        if txn.open_date and txn.close_date:
            durations.append((txn.close_date - txn.open_date).total_seconds() / 86400)

    pnls = []
    for txn in txns:
        pnl = calculate_transaction_pnl(txn)
        if pnl is not None:
            pnls.append(pnl)

    winning_pnls = [p for p in pnls if p > 0]
    losing_pnls = [p for p in pnls if p < 0]
    win_rate, wins, losses = calculate_win_loss_ratio([{'pnl': pnl} for pnl in pnls])

    returns = []
    for txn in txns:
        pnl = calculate_transaction_pnl(txn)
        if pnl is not None:
            position_value = txn.open_price * txn.quantity * (getattr(txn, "multiplier", None) or 1)
            if position_value != 0:
                returns.append(pnl / position_value)

    ordered = sorted((t for t in txns if t.close_date), key=lambda t: t.close_date)
    ordered_pnls = [pnl for pnl in (calculate_transaction_pnl(t) for t in ordered)
                    if pnl is not None]
    max_dd, max_dd_pct = max_drawdown_from_pnl(ordered_pnls)

    return {
        'total_transactions': len(txns),
        'max_drawdown': max_dd,
        'max_drawdown_pct': max_dd_pct,
        'avg_duration_days': np.mean(durations) if durations else 0,
        'total_pnl': sum(pnls) if pnls else 0,
        'avg_pnl': np.mean(pnls) if pnls else 0,
        'win_rate': win_rate,
        'wins': wins,
        'losses': losses,
        'profit_factor': calculate_profit_factor(winning_pnls, losing_pnls),
        'largest_win': max(winning_pnls) if winning_pnls else None,
        'largest_loss': min(losing_pnls) if losing_pnls else None,
        'sharpe_ratio': calculate_sharpe_ratio(returns) if len(returns) >= 30 else None,
        'transactions': txns,
        'returns': returns,
    }


def monthly_pnl(txns: Iterable[Any]) -> Dict[str, Dict[str, float]]:
    """``{"YYYY-MM": {"pnl": sum, "count": n}}`` by CLOSE month, in the given order (the live
    page's monthly chart, for one expert). Open or unmeasurable transactions are skipped."""
    out: Dict[str, Dict[str, float]] = defaultdict(lambda: {'pnl': 0, 'count': 0})
    for txn in txns:
        pnl = calculate_transaction_pnl(txn)
        if txn.close_date and pnl is not None:
            bucket = out[txn.close_date.strftime('%Y-%m')]
            bucket['pnl'] += pnl
            bucket['count'] += 1
    return dict(out)
