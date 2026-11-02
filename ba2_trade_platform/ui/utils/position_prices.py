"""Prices for open-P/L display: the BROKER's position price for what the account holds.

Every open-P/L view prices a held position the way the broker does, so the Live Trades rows, the
overview tables and the Floating P/L cards agree with each other and with the broker's own screen.
They did not: the cards read the broker position (Alpaca's last trade, after-hours included) while
the tables read the account price feed (FMP regular session). On 2026-10-28 FTNT fell ~25% after
hours and the same expert read -$108.42 on the overview and +$42.28 on Live Trades.

A symbol the broker does not hold (a position not yet reconciled, say) is priced from the account
price feed, the only price there is for it.
"""
from typing import Dict, Iterable

from ...logger import logger


def broker_position_prices_from(positions) -> Dict[str, float]:
    """``{symbol: broker current price}`` for every quoted position in *positions*.

    An unquoted position is left out (never coerced to zero), so it surfaces as unpriced.
    """
    prices: Dict[str, float] = {}
    for pos in positions:
        pos_dict = pos if isinstance(pos, dict) else dict(pos)
        symbol = pos_dict.get('symbol')
        current_price = pos_dict.get('current_price')
        if symbol is None or current_price is None:
            continue
        prices[symbol] = float(current_price)
    return prices


def held_position_prices(account, account_id: int, symbols: Iterable[str]) -> Dict[str, float]:
    """Display prices for *symbols*: the broker's position price, the account feed for the rest."""
    symbols = list(symbols)
    prices: Dict[str, float] = {}
    try:
        positions = account.get_positions()   # None = fetch failure (tri-state)
        held = broker_position_prices_from(positions) if positions is not None else None
        failure = "the position read failed"
    except Exception as e:
        held, failure = None, str(e)
    if held is None:
        logger.warning(f"Broker positions unavailable for account {account_id} ({failure}); "
                       f"pricing open P/L from the account price feed")
        held = {}
    for symbol in symbols:
        if symbol in held:
            prices[symbol] = held[symbol]
    rest = [s for s in symbols if s not in prices]
    if rest:
        fed = account.get_instrument_current_price(rest)
        if fed:
            prices.update({s: p for s, p in fed.items() if p is not None})
    return prices
