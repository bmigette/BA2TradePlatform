import asyncio
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, date, timedelta

from ...logger import logger
from ...core.models import Position
from ...core.types import OrderDirection
from ...core.interfaces import ReadOnlyAccountInterface


class TastyTradeAccount(ReadOnlyAccountInterface):
    """
    Read-only account interface for TastyTrade brokerage.

    Uses the tastytrade Python SDK (async) with sync wrappers.
    This account does NOT support trading operations.
    """
    supports_trading = False

    def __init__(self, id: int):
        super().__init__(id)
        self._session = None
        self._account = None
        self._authentication_error = None
        # Persistent event loop for this account instance so the httpx
        # async client's connections are never invalidated by a closed loop.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        try:
            required_settings = ["client_secret", "refresh_token", "account_id"]
            missing = [k for k in required_settings if k not in self.settings or self.settings[k] is None]
            if missing:
                error_msg = f"Missing required settings: {', '.join(missing)}"
                self._authentication_error = error_msg
                logger.error(f"TastyTradeAccount {id}: {error_msg}")
                raise ValueError(error_msg)

            self._connect()
            logger.info(f"TastyTrade session initialized for account {id}.")
        except Exception as e:
            self._authentication_error = str(e)
            logger.error(f"Failed to initialize TastyTrade session for account {id}: {e}", exc_info=True)
            raise

    def _run_async(self, coro):
        """Run an async coroutine on this account's persistent event loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    def _connect(self):
        """Establish connection to TastyTrade API."""
        from tastytrade.session import Session as TastySession
        from tastytrade.account import Account as TastyAccount

        is_test = bool(self.settings.get("is_test", False))

        # Create session on the persistent loop so httpx client binds to it
        self._session = self._run_async(self._create_session_async(
            provider_secret=self.settings["client_secret"],
            refresh_token=self.settings["refresh_token"],
            is_test=is_test,
        ))

        # Fetch the specific account by account number
        target_id = self.settings["account_id"]
        self._account = self._run_async(TastyAccount.get(self._session, account_number=target_id))

        if self._account is None:
            raise ValueError(f"Account {target_id} not found on TastyTrade.")

    @staticmethod
    async def _create_session_async(provider_secret, refresh_token, is_test):
        """Create TastyTrade session on the async loop so httpx client binds correctly."""
        from tastytrade.session import Session as TastySession
        return TastySession(
            provider_secret=provider_secret,
            refresh_token=refresh_token,
            is_test=is_test,
        )

    def _check_authentication(self) -> bool:
        if self._session is None or self._account is None:
            logger.error(f"TastyTradeAccount {self.id}: Not authenticated - {self._authentication_error}")
            return False
        return True

    @staticmethod
    def get_settings_definitions() -> Dict[str, Any]:
        return {
            "client_secret": {
                "type": "str",
                "required": True,
                "description": "OAuth provider secret (mapped to provider_secret in SDK)"
            },
            "refresh_token": {
                "type": "str",
                "required": True,
                "description": "OAuth refresh token"
            },
            "account_id": {
                "type": "str",
                "required": True,
                "description": "TastyTrade account number"
            },
            "is_test": {
                "type": "bool",
                "required": False,
                "default": False,
                "description": "Use sandbox/test environment"
            }
        }

    def get_balance(self) -> Optional[float]:
        if not self._check_authentication():
            return None
        try:
            balances = self._run_async(self._account.get_balances(self._session))
            return float(balances.net_liquidating_value)
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting balance: {e}", exc_info=True)
            return None

    def get_account_info(self) -> Dict[str, Any]:
        if not self._check_authentication():
            return {}
        try:
            balances = self._run_async(self._account.get_balances(self._session))
            return {
                "account_number": self._account.account_number,
                "account_type": self._account.account_type_name,
                "net_liquidating_value": float(balances.net_liquidating_value),
                "cash_balance": float(balances.cash_balance),
                "equity_buying_power": float(balances.equity_buying_power),
                "long_equity_value": float(balances.long_equity_value),
                "short_equity_value": float(balances.short_equity_value),
                "margin_equity": float(balances.margin_equity),
                "maintenance_requirement": float(balances.maintenance_requirement),
                "pending_cash": float(balances.pending_cash),
                "cash_available_to_withdraw": float(balances.cash_available_to_withdraw),
                "supports_trading": False,
            }
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting account info: {e}", exc_info=True)
            return {}

    def get_positions(self) -> List[Position]:
        if not self._check_authentication():
            return []
        try:
            tt_positions = self._run_async(self._account.get_positions(self._session, include_marks=True))
            positions = []
            for pos in tt_positions:
                qty = float(pos.quantity)
                if qty == 0:
                    continue

                multiplier = int(pos.multiplier) if pos.multiplier else 1
                avg_price = float(pos.average_open_price)
                # mark_price is per-share; mark is total position value
                current = float(pos.mark_price or pos.close_price or avg_price)
                cost_basis = avg_price * abs(qty) * multiplier
                market_val = current * abs(qty) * multiplier
                unrealized_pl = market_val - cost_basis
                unrealized_plpc = (unrealized_pl / cost_basis) if cost_basis else 0

                side = OrderDirection.BUY if pos.quantity_direction == "Long" else OrderDirection.SELL

                position = Position(
                    asset_class=str(pos.instrument_type.value) if pos.instrument_type else "Equity",
                    avg_entry_price=avg_price,
                    avg_entry_swap_rate=None,
                    change_today=0.0,
                    cost_basis=cost_basis,
                    current_price=current,
                    exchange="",
                    lastday_price=float(pos.close_price) if pos.close_price else current,
                    market_value=market_val,
                    qty=abs(qty),
                    qty_available=abs(qty),
                    side=side,
                    swap_rate=None,
                    symbol=pos.symbol,
                    unrealized_intraday_pl=float(pos.realized_day_gain or 0),
                    unrealized_intraday_plpc=0.0,
                    unrealized_pl=unrealized_pl,
                    unrealized_plpc=unrealized_plpc,
                )
                positions.append(position)

            logger.debug(f"[Account {self.id}] Retrieved {len(positions)} positions from TastyTrade")
            return positions
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting positions: {e}", exc_info=True)
            return []

    def get_orders(self, status=None) -> Any:
        if not self._check_authentication():
            return []
        try:
            orders = self._run_async(self._account.get_order_history(self._session))
            logger.debug(f"[Account {self.id}] Retrieved {len(orders)} orders from TastyTrade")
            return orders
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting orders: {e}", exc_info=True)
            return []

    def get_order(self, order_id: str) -> Any:
        if not self._check_authentication():
            return None
        try:
            from tastytrade.account import Account as TastyAccount
            order = self._run_async(self._account.get_order(self._session, int(order_id)))
            return order
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting order {order_id}: {e}", exc_info=True)
            return None

    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        if not self._check_authentication():
            return {s: False for s in symbols}
        try:
            from tastytrade.instruments import Equity
            result = {}
            try:
                equities = self._run_async(Equity.get(self._session, symbols))
                if isinstance(equities, list):
                    found_symbols = {e.symbol for e in equities}
                else:
                    found_symbols = {equities.symbol}
            except Exception as e:
                logger.warning(f"[Account {self.id}] Symbol lookup failed: {e}")
                found_symbols = set()

            for s in symbols:
                result[s] = s in found_symbols
            return result
        except Exception as e:
            logger.error(f"[Account {self.id}] Error checking symbols: {e}", exc_info=True)
            return {s: False for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        if not self._check_authentication():
            if isinstance(symbol_or_symbols, list):
                return {s: None for s in symbol_or_symbols}
            return None

        try:
            from tastytrade.market_data import get_market_data
            from tastytrade.order import InstrumentType

            if isinstance(symbol_or_symbols, str):
                data = self._run_async(get_market_data(self._session, symbol_or_symbols, InstrumentType.EQUITY))
                if price_type == 'bid' and data.bid:
                    return float(data.bid)
                elif price_type == 'ask' and data.ask:
                    return float(data.ask)
                elif price_type == 'mid' and data.mid:
                    return float(data.mid)
                elif data.last:
                    return float(data.last)
                elif data.close:
                    return float(data.close)
                return None
            else:
                result = {}
                for symbol in symbol_or_symbols:
                    try:
                        data = self._run_async(get_market_data(self._session, symbol, InstrumentType.EQUITY))
                        if price_type == 'bid' and data.bid:
                            result[symbol] = float(data.bid)
                        elif price_type == 'ask' and data.ask:
                            result[symbol] = float(data.ask)
                        elif price_type == 'mid' and data.mid:
                            result[symbol] = float(data.mid)
                        elif data.last:
                            result[symbol] = float(data.last)
                        elif data.close:
                            result[symbol] = float(data.close)
                        else:
                            result[symbol] = None
                    except Exception as e:
                        logger.warning(f"[Account {self.id}] Error fetching price for {symbol}: {e}")
                        result[symbol] = None
                return result
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting price: {e}", exc_info=True)
            if isinstance(symbol_or_symbols, list):
                return {s: None for s in symbol_or_symbols}
            return None

    def refresh_positions(self) -> bool:
        # Positions are always fetched live from API
        return True

    def refresh_orders(self, **kwargs) -> bool:
        # Orders are always fetched live from API
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None) -> List[Dict]:
        """Return one record per dividend the account received.

        Dividends are driven by the CASH event, not the reinvestment. TastyTrade
        posts each dividend as a group of ``Money Movement`` transactions with
        ``transaction_sub_type == "Dividend"`` per (symbol, date):
            +1.57  "TIDAL TRUST II"   gross dividend (positive)
            -0.24  "TIDAL TRUST II"   tax withheld   (negative)
        When DRIP is enabled there is additionally:
            - a ``Money Movement`` / ``Withdrawal`` "Cash dividend reinvested
              into X" leg (the net cash spent buying shares), and
            - a ``Receive Deliver`` / ``Dividend`` leg crediting the shares.
        We anchor on the ``Money Movement`` / ``Dividend`` cash so a dividend
        shows up whether or not it was reinvested (with DRIP off there is no
        ``Receive Deliver`` leg at all), and we ENRICH with the share-receipt
        leg only when present.

        ``amount`` is the NET dividend (gross - tax) — the income actually kept.
        ``gross_amount`` / ``tax_withheld`` carry the breakdown.
        ``drip_quantity`` / ``drip_price`` are None for non-reinvested (cash)
        dividends.
        """
        if not self._check_authentication():
            return []
        try:
            def _range(p):
                if start_date:
                    p["start_date"] = start_date.date() if isinstance(start_date, datetime) else start_date
                if end_date:
                    p["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date
                if symbol:
                    p["symbol"] = symbol
                return p

            # DRIP share-receipt legs (enrichment only): Receive Deliver / Dividend.
            drip_map = {}  # (symbol, date) -> (qty, price)
            try:
                rd_txns = self._run_async(self._account.get_history(
                    self._session, **_range({"types": ["Receive Deliver"],
                                             "sub_types": ["Dividend"], "sort": "Asc"})))
                for txn in rd_txns:
                    sym = getattr(txn, 'underlying_symbol', None) or getattr(txn, 'symbol', None)
                    d = getattr(txn, 'transaction_date', None)
                    qty = float(getattr(txn, 'quantity', 0) or 0)
                    if sym and d and qty > 0:
                        drip_map[(sym, d)] = (qty, float(getattr(txn, 'price', 0) or 0))
            except Exception as e:
                logger.warning(f"[Account {self.id}] DRIP share-receipt fetch failed: {e}")

            # Cash dividends + tax: Money Movement transactions, sub_type "Dividend"
            # only (excludes the "Withdrawal" reinvest leg, ACH deposits, fee
            # adjustments, etc.). Positive net = gross, negative net = tax.
            gross_map = {}  # (symbol, date) -> gross dividend
            tax_map = {}    # (symbol, date) -> tax withheld (positive)
            mm_txns = self._run_async(self._account.get_history(
                self._session, **_range({"types": ["Money Movement"], "sort": "Asc"})))
            for mm in mm_txns:
                if getattr(mm, 'transaction_sub_type', None) != 'Dividend':
                    continue
                sym = getattr(mm, 'underlying_symbol', None) or getattr(mm, 'symbol', None)
                d = getattr(mm, 'transaction_date', None)
                if not sym or not d:
                    continue
                net_val = float(getattr(mm, 'net_value', 0) or 0)
                key = (sym, d)
                if net_val > 0:
                    gross_map[key] = gross_map.get(key, 0.0) + net_val
                elif net_val < 0:
                    tax_map[key] = tax_map.get(key, 0.0) + abs(net_val)

            dividends = []
            for key in sorted(set(gross_map) | set(tax_map), key=lambda k: (str(k[1]), str(k[0]))):
                sym, d = key
                gross = round(gross_map.get(key, 0.0), 2)
                tax = round(tax_map.get(key, 0.0), 2)
                drip_qty, drip_price = drip_map.get(key, (None, None))
                dividends.append({
                    'symbol': sym,
                    'amount': round(gross - tax, 2),   # NET dividend (income kept)
                    'gross_amount': gross,
                    'tax_withheld': tax,
                    'date': d,
                    'drip_quantity': drip_qty,
                    'drip_price': drip_price,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(dividends)} dividend records")
            return dividends
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching dividends: {e}", exc_info=True)
            return []

    def get_balance_history(self, start_date=None, end_date=None) -> List[Dict]:
        if not self._check_authentication():
            return []
        try:
            # TastyTrade requires start_date to return daily snapshots;
            # without it, only a few snapshots are returned.
            if start_date:
                sd = start_date.date() if isinstance(start_date, datetime) else start_date
            else:
                sd = date.today() - timedelta(days=365)

            params = {"start_date": sd}
            if end_date:
                params["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date

            snapshots = self._run_async(self._account.get_balance_snapshots(
                self._session,
                page_offset=None,  # Get all pages
                **params
            ))

            result = []
            for snap in snapshots:
                cash = float(snap.cash_balance) if snap.cash_balance else 0.0
                nlv = float(snap.net_liquidating_value) if snap.net_liquidating_value else 0.0
                equity = nlv - cash

                result.append({
                    'date': snap.snapshot_date if hasattr(snap, 'snapshot_date') else None,
                    'net_liquidating_value': nlv,
                    'cash_balance': cash,
                    'equity_value': equity,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(result)} balance history snapshots")
            return result
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching balance history: {e}", exc_info=True)
            return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None) -> List[Dict]:
        """Get filled trade history from TastyTrade transaction history."""
        if not self._check_authentication():
            return []

        try:
            params = {
                "types": ["Trade"],
                "sort": "Asc",
            }
            if symbol:
                params["symbol"] = symbol
            if start_date:
                params["start_date"] = start_date.date() if isinstance(start_date, datetime) else start_date
            if end_date:
                params["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date

            transactions = self._run_async(self._account.get_history(self._session, **params))

            trades = []
            for txn in transactions:
                txn_symbol = getattr(txn, 'underlying_symbol', None) or getattr(txn, 'symbol', None)
                txn_qty = float(getattr(txn, 'quantity', 0) or 0)
                if txn_qty <= 0:
                    continue

                txn_date = getattr(txn, 'transaction_date', None) or getattr(txn, 'executed_at', None)
                txn_price = float(getattr(txn, 'price', 0) or 0)

                # Determine side from action field
                action = str(getattr(txn, 'action', '') or '').lower()
                if 'buy' in action:
                    side = 'BUY'
                elif 'sell' in action:
                    side = 'SELL'
                else:
                    continue

                trades.append({
                    'symbol': txn_symbol,
                    'qty': txn_qty,
                    'side': side,
                    'date': txn_date,
                    'price': txn_price,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(trades)} filled trades")
            return trades

        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching filled trades: {e}", exc_info=True)
            return []
