"""
Smart Risk Manager Toolkit

Provides LangChain-compatible tools for the Smart Risk Manager agent graph.
All tools are wrappers around existing platform functionality.
"""

import json
from typing import Dict, Any, List, Optional, Annotated
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select

from ..logger import logger
from .models import (
    Transaction, TradingOrder, MarketAnalysis, ExpertInstance,
    SmartRiskManagerJob
)
from .types import TransactionStatus, OrderStatus, OrderType, OrderDirection, MarketAnalysisStatus, OrderOpenType
from .db import get_db, get_instance, add_instance
from .utils import get_expert_instance_from_id, get_account_instance_from_id
from .interfaces import MarketExpertInterface
from .TransactionHelper import TransactionHelper


class SmartRiskManagerToolkit:
    """
    Toolkit providing access to portfolio data, market analyses, and trading actions
    for the Smart Risk Manager agent.
    """
    
    def __init__(self, expert_instance_id: int, account_id: int):
        """
        Initialize the toolkit for a specific expert and account.
        
        Args:
            expert_instance_id: ID of the ExpertInstance
            account_id: ID of the AccountDefinition
        """
        self.expert_instance_id = expert_instance_id
        self.account_id = account_id
        self.expert = get_expert_instance_from_id(expert_instance_id)
        self.account = get_account_instance_from_id(account_id)
        
        if not self.expert:
            raise ValueError(f"Expert instance {expert_instance_id} not found")
        if not self.account:
            raise ValueError(f"Account {account_id} not found")
    
    # ==================== Helper Methods ====================
    
    def _validate_transaction_ownership(self, transaction_id: int) -> Transaction:
        """
        Validate that a transaction belongs to this expert instance.
        
        CRITICAL SECURITY CHECK: Prevents one expert from modifying another expert's transactions.
        
        Args:
            transaction_id: Transaction ID to validate
            
        Returns:
            Transaction object if valid
            
        Raises:
            ValueError: If transaction doesn't exist or doesn't belong to this expert
        """
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            
            if not transaction:
                raise ValueError(f"Transaction #{transaction_id} not found")
            
            if transaction.expert_id != self.expert_instance_id:
                raise ValueError(
                    f"Transaction #{transaction_id} belongs to expert {transaction.expert_id}, "
                    f"not expert {self.expert_instance_id}. Cannot modify transactions from other experts."
                )
            
            return transaction
    
    def _create_trading_order(
        self,
        symbol: str,
        quantity: float,
        side: OrderDirection,
        order_type: OrderType,
        transaction_id: Optional[int] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        depends_on_order: Optional[int] = None,
        depends_order_status_trigger: Optional[OrderStatus] = None,
        good_for: Optional[str] = None,
        comment: Optional[str] = None,
        open_type: OrderOpenType = OrderOpenType.AUTOMATIC
    ) -> TradingOrder:
        """
        Create a TradingOrder object with proper field validation.
        
        This helper ensures all required fields are set correctly before submission.
        
        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            quantity: Number of shares
            side: OrderDirection.BUY or OrderDirection.SELL
            order_type: OrderType enum value (MARKET, BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP)
            transaction_id: Optional transaction ID (required for non-market orders)
            limit_price: Required for BUY_LIMIT/SELL_LIMIT orders
            stop_price: Required for BUY_STOP/SELL_STOP orders
            depends_on_order: Optional ID of order this depends on (for TP/SL)
            depends_order_status_trigger: Status trigger for dependent order
            good_for: Time-in-force (e.g., 'gtc', 'day')
            comment: Optional order comment
            open_type: Order open type (AUTOMATIC for Smart Risk Manager)
            
        Returns:
            TradingOrder: Configured order ready for submission
            
        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Validate limit orders have limit_price
        if order_type in [OrderType.BUY_LIMIT, OrderType.SELL_LIMIT]:
            if limit_price is None:
                raise ValueError(f"limit_price is required for {order_type.value} orders")
        
        # Validate stop orders have stop_price
        if order_type in [OrderType.BUY_STOP, OrderType.SELL_STOP]:
            if stop_price is None:
                raise ValueError(f"stop_price is required for {order_type.value} orders")
        
        # Validate non-market orders have transaction_id
        if order_type != OrderType.MARKET and transaction_id is None:
            raise ValueError(f"Non-market orders ({order_type.value}) must have a transaction_id")
        
        # Create the order
        order = TradingOrder(
            account_id=self.account_id,
            symbol=symbol,
            quantity=quantity,
            side=side,
            order_type=order_type,
            transaction_id=transaction_id,
            limit_price=limit_price,
            stop_price=stop_price,
            depends_on_order=depends_on_order,
            depends_order_status_trigger=depends_order_status_trigger,
            good_for=good_for,
            comment=comment,
            status=OrderStatus.PENDING,  # Will be updated by broker
            open_type=open_type
        )
        
        logger.debug(f"Created TradingOrder: {symbol} {side.value} {quantity} @ {order_type.value} (open_type={open_type.value})")
        return order
    
    # ==================== Portfolio & Account Tools ====================
    
    def get_portfolio_status(self) -> Dict[str, Any]:
        """
        Get current portfolio status including all open positions.
        
        Returns comprehensive portfolio data including equity, balance, open positions,
        unrealized P&L, and risk metrics.
        """
        try:
            logger.debug(f"Getting portfolio status for account {self.account_id}")
            
            # Get expert virtual balance and available balance using expert methods
            # These methods already handle virtual equity percentage calculation
            virtual_balance = self.expert.get_virtual_balance()
            available_balance = self.expert.get_available_balance()
            
            if virtual_balance is None or available_balance is None:
                logger.error(f"Could not get balance information for expert {self.expert_instance_id}")
                raise ValueError("Failed to get expert balance information")
            
            logger.debug(f"Expert virtual balance: ${virtual_balance:,.2f}, Available balance: ${available_balance:,.2f}")
            
            # Get open transactions (transactions are per expert, not per account)
            with get_db() as session:
                # First, let's check all transactions for this expert regardless of status
                all_expert_transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id == self.expert_instance_id)
                ).all()
                
                logger.info(f"Expert {self.expert_instance_id} has {len(all_expert_transactions)} total transactions")
                for t in all_expert_transactions:
                    logger.debug(f"  Transaction {t.id}: {t.symbol} status={t.status} expert_id={t.expert_id}")
                
                transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id == self.expert_instance_id)
                    .where(Transaction.status == TransactionStatus.OPENED)
                ).all()
                
                logger.info(f"Expert {self.expert_instance_id} has {len(transactions)} OPENED transactions")
                
                # Get pending transactions (WAITING status)
                pending_transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id == self.expert_instance_id)
                    .where(Transaction.status == TransactionStatus.WAITING)
                ).all()
                
                open_positions = []
                pending_positions = []
                total_unrealized_pnl = 0.0
                total_position_value = 0.0
                total_pending_value = 0.0
                largest_position_value = 0.0
                
                for trans in transactions:
                    # Get current price
                    try:
                        current_price = self.account.get_instrument_current_price(trans.symbol)
                    except Exception as e:
                        logger.error(f"Failed to get current price for {trans.symbol}: {e}")
                        current_price = trans.open_price  # Fallback
                    
                    # Get actual quantity from filled orders
                    # abs() because get_current_open_qty() returns signed qty (negative for shorts)
                    # and direction is already handled separately in the P&L if/else below
                    quantity = abs(trans.get_current_open_qty())
                    logger.debug(f"  Transaction {trans.id} ({trans.symbol}): quantity={quantity}")
                    
                    # Infer direction from first order
                    direction = None
                    if trans.trading_orders:
                        first_order = sorted(trans.trading_orders, key=lambda o: o.created_at)[0]
                        direction = first_order.side
                    
                    if not direction or not trans.open_price or quantity == 0:
                        logger.warning(f"Skipping transaction {trans.id} ({trans.symbol}) - direction={direction}, open_price={trans.open_price}, quantity={quantity}")
                        continue
                    
                    # Calculate P&L
                    if direction == OrderDirection.BUY:
                        unrealized_pnl = (current_price - trans.open_price) * quantity
                    else:  # SELL
                        unrealized_pnl = (trans.open_price - current_price) * quantity
                    
                    unrealized_pnl_pct = (unrealized_pnl / (trans.open_price * quantity)) * 100 if trans.open_price > 0 else 0.0
                    position_value = current_price * quantity
                    
                    # Get TP/SL orders - these are orders that depend on the entry order
                    # TP = SELL_LIMIT (for long) or BUY_LIMIT (for short) with depends_on_order set
                    # SL = SELL_STOP (for long) or BUY_STOP (for short) with depends_on_order set
                    entry_order = trans.trading_orders[0] if trans.trading_orders else None
                    tp_order = None
                    sl_order = None
                    
                    if entry_order:
                        # Get all dependent orders
                        dependent_orders = session.exec(
                            select(TradingOrder)
                            .where(TradingOrder.depends_on_order == entry_order.id)
                            .where(TradingOrder.status.not_in(OrderStatus.get_terminal_statuses()))
                        ).all()
                        
                        for order in dependent_orders:
                            # TP order: SELL_LIMIT (long) or BUY_LIMIT (short)
                            if (direction == OrderDirection.BUY and order.order_type == OrderType.SELL_LIMIT) or \
                               (direction == OrderDirection.SELL and order.order_type == OrderType.BUY_LIMIT):
                                tp_order = order
                            # SL order: SELL_STOP (long) or BUY_STOP (short)
                            elif (direction == OrderDirection.BUY and order.order_type == OrderType.SELL_STOP) or \
                                 (direction == OrderDirection.SELL and order.order_type == OrderType.BUY_STOP):
                                sl_order = order
                    
                    position_data = {
                        "transaction_id": trans.id,
                        "symbol": trans.symbol,
                        "direction": direction.value,
                        "quantity": quantity,
                        "entry_price": trans.open_price,
                        "current_price": current_price,
                        "unrealized_pnl": round(unrealized_pnl, 2),
                        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                        "position_value": round(position_value, 2),
                        "tp_order": {
                            "order_id": tp_order.id,
                            "price": tp_order.limit_price,
                            "quantity": tp_order.quantity,
                            "status": tp_order.status.value
                        } if tp_order else None,
                        "sl_order": {
                            "order_id": sl_order.id,
                            "price": sl_order.stop_price,
                            "quantity": sl_order.quantity,
                            "status": sl_order.status.value
                        } if sl_order else None
                    }
                    
                    open_positions.append(position_data)
                    total_unrealized_pnl += unrealized_pnl
                    total_position_value += position_value
                    largest_position_value = max(largest_position_value, position_value)
                
                # Process pending transactions (WAITING status)
                for trans in pending_transactions:
                    # Get pending quantity from unfilled orders
                    pending_qty = trans.get_pending_open_qty()
                    
                    if pending_qty == 0:
                        continue
                    
                    # Infer direction from pending orders
                    direction = None
                    estimated_price = None
                    if trans.trading_orders:
                        for order in trans.trading_orders:
                            if order.status in OrderStatus.get_unfilled_statuses() and order.depends_on_order is None:
                                direction = order.side
                                # Use limit price if available, otherwise use current market price as estimate
                                if order.limit_price:
                                    estimated_price = order.limit_price
                                break
                    
                    if not direction:
                        continue
                    
                    # Get current price for estimation
                    try:
                        current_price = self.account.get_instrument_current_price(trans.symbol)
                        if not estimated_price:
                            estimated_price = current_price
                    except Exception as e:
                        logger.error(f"Failed to get current price for pending transaction {trans.symbol}: {e}")
                        if not estimated_price:
                            continue
                    
                    # Calculate estimated value of pending position
                    pending_value = abs(pending_qty) * estimated_price
                    
                    pending_data = {
                        "transaction_id": trans.id,
                        "symbol": trans.symbol,
                        "direction": direction.value,
                        "pending_quantity": abs(pending_qty),
                        "estimated_price": round(estimated_price, 2),
                        "estimated_value": round(pending_value, 2)
                    }
                    
                    pending_positions.append(pending_data)
                    total_pending_value += pending_value
                
                # Calculate risk metrics using virtual_balance from expert
                balance_pct_available = (available_balance / virtual_balance * 100) if virtual_balance > 0 else 0.0
                largest_position_pct = (largest_position_value / virtual_balance * 100) if virtual_balance > 0 else 0.0
                pending_value_pct = (total_pending_value / virtual_balance * 100) if virtual_balance > 0 else 0.0
                
                result = {
                    "account_virtual_equity": round(virtual_balance, 2),
                    "account_available_balance": round(available_balance, 2),
                    "account_balance_pct_available": round(balance_pct_available, 2),
                    "pending_transactions_value": round(total_pending_value, 2),
                    "pending_transactions_pct": round(pending_value_pct, 2),
                    "open_positions": open_positions,
                    "pending_positions": pending_positions,
                    "total_unrealized_pnl": round(total_unrealized_pnl, 2),
                    "total_position_value": round(total_position_value, 2),
                    "risk_metrics": {
                        "largest_position_pct": round(largest_position_pct, 2),
                        "num_positions": len(open_positions),
                        "num_pending": len(pending_positions)
                    }
                }
                
                logger.debug(f"Portfolio status: {len(open_positions)} open positions, {len(pending_positions)} pending, virtual_balance=${virtual_balance:,.2f}, available_balance=${available_balance:,.2f}, pending_value=${total_pending_value:,.2f}, unrealized_pnl={total_unrealized_pnl}")
                return result
                
        except Exception as e:
            logger.error(f"Error getting portfolio status: {e}", exc_info=True)
            raise

    def get_all_transactions(
        self,
        include_pending_actions: bool = False,
        pending_actions: Optional[List[Dict[str, Any]]] = None,
        format_type: str = "markdown"
    ) -> str:
        """
        Retrieve all transactions (filled + pending + future recommended actions).
        
        This comprehensive tool provides a complete view of:
        1. FILLED positions (live trades currently in portfolio)
        2. PENDING positions (orders submitted to broker, awaiting fill)
        3. FUTURE transactions (recommended actions not yet executed)
        
        Can output as Markdown (for LLM consumption) or JSON (for structured data).
        
        Args:
            include_pending_actions: Whether to include recommended actions as future transactions
            pending_actions: List of recommended actions from research node (if include_pending_actions=True)
            format_type: Output format - "markdown" (default) or "json"
            
        Returns:
            Formatted string with all transactions data
            
        Examples:
            # Get all transactions as markdown (for research node to read)
            result = toolkit.get_all_transactions(format_type="markdown")
            
            # Get all transactions with pending actions as JSON
            result = toolkit.get_all_transactions(
                include_pending_actions=True,
                pending_actions=recommended_actions,
                format_type="json"
            )
        """
        try:
            logger.debug(f"Getting all transactions: include_pending={include_pending_actions}, format={format_type}")
            
            # Get portfolio status to access all transactions
            portfolio_status = self.get_portfolio_status()
            
            # Build data structure
            transactions_data = {
                "account_virtual_equity": portfolio_status["account_virtual_equity"],
                "account_available_balance": portfolio_status["account_available_balance"],
                "filled_positions": portfolio_status["open_positions"],
                "pending_positions": portfolio_status["pending_positions"],
                "future_transactions": []
            }
            
            # Add pending actions as future transactions if requested
            if include_pending_actions and pending_actions:
                transactions_data["future_transactions"] = pending_actions
            
            # Format output based on format_type
            if format_type == "json":
                return json.dumps(transactions_data, indent=2)
            else:  # markdown (default)
                return self._format_transactions_as_markdown(transactions_data)
        
        except Exception as e:
            logger.error(f"Error getting all transactions: {e}", exc_info=True)
            if format_type == "json":
                return json.dumps({"error": str(e)})
            else:
                return f"Error retrieving transactions: {str(e)}"
    
    def _format_transactions_as_markdown(self, transactions_data: Dict[str, Any]) -> str:
        """
        Format transaction data as readable markdown.
        
        Args:
            transactions_data: Transaction data from get_all_transactions
            
        Returns:
            Formatted markdown string
        """
        md = []
        md.append("# All Transactions Report")
        md.append("")
        md.append(f"**Virtual Equity:** ${transactions_data['account_virtual_equity']:,.2f}")
        md.append(f"**Available Balance:** ${transactions_data['account_available_balance']:,.2f}")
        md.append("")
        
        # Filled positions
        filled = transactions_data.get("filled_positions", [])
        md.append(f"## Filled Positions ({len(filled)} total)")
        md.append("")
        if filled:
            for pos in filled:
                md.append(f"### Transaction #{pos['transaction_id']}: {pos['symbol']}")
                md.append(f"- **Direction:** {pos['direction'].upper()}")
                md.append(f"- **Quantity:** {pos['quantity']} shares")
                md.append(f"- **Entry Price:** ${pos['entry_price']:.2f}")
                md.append(f"- **Current Price:** ${pos['current_price']:.2f}")
                md.append(f"- **P&L:** {pos['unrealized_pnl_pct']:.2f}% (${pos['unrealized_pnl']:,.2f})")
                md.append(f"- **Position Value:** ${pos['position_value']:,.2f}")
                
                tp_order = pos.get("tp_order")
                sl_order = pos.get("sl_order")
                
                if tp_order and tp_order.get("price"):
                    md.append(f"- **Take Profit:** ${tp_order['price']:.2f}")
                else:
                    md.append(f"- **Take Profit:** ⚠️ Not set")
                
                if sl_order and sl_order.get("price"):
                    md.append(f"- **Stop Loss:** ${sl_order['price']:.2f}")
                else:
                    md.append(f"- **Stop Loss:** ⚠️ Not set")
                
                md.append("")
        else:
            md.append("*(No filled positions)*")
            md.append("")
        
        # Pending positions
        pending = transactions_data.get("pending_positions", [])
        md.append(f"## Pending Positions ({len(pending)} total)")
        md.append("")
        if pending:
            for pos in pending:
                md.append(f"### Transaction #{pos['transaction_id']}: {pos['symbol']}")
                md.append(f"- **Direction:** {pos['direction'].upper()}")
                md.append(f"- **Pending Quantity:** {pos['pending_quantity']} shares")
                md.append(f"- **Estimated Price:** ${pos['estimated_price']:.2f}")
                md.append(f"- **Estimated Value:** ${pos['estimated_value']:,.2f}")
                md.append("")
        else:
            md.append("*(No pending positions)*")
            md.append("")
        
        # Future transactions (recommended actions)
        future = transactions_data.get("future_transactions", [])
        md.append(f"## Recommended Future Actions ({len(future)} total)")
        md.append("")
        if future:
            for i, action in enumerate(future, 1):
                if isinstance(action, dict):
                    action_type = action.get("action_type", "UNKNOWN")
                    md.append(f"### Action {i}: {action_type}")
                    for key, value in action.items():
                        if key != "action_type":
                            md.append(f"- **{key}:** {value}")
                    md.append("")
                else:
                    md.append(f"### Action {i}: {str(action)}")
                    md.append("")
        else:
            md.append("*(No recommended actions)*")
            md.append("")
        
        return "\n".join(md)

    def get_recent_analyses(
        self,
        max_age_hours: Annotated[int, "Maximum age of analyses to return in hours"] = 24
    ) -> List[Dict[str, Any]]:
        """
        Get recent market analyses for ALL symbols (not filtered by symbol).
        
        Returns all recent COMPLETED analyses for this expert instance within the time window.
        This allows the risk manager to see the full picture of recent market research across
        all instruments. If the most recent analysis for a symbol failed, falls back to the
        previous completed analysis within the time window.
        
        Use get_historical_analyses(symbol) to get deeper history for a specific symbol.
        
        Args:
            max_age_hours: Maximum age of analyses to return (default 72 hours)
            
        Returns:
            List of analysis summaries with metadata, sorted by timestamp DESC
        """
        try:
            logger.debug(f"Getting recent analyses for all symbols, max_age={max_age_hours}h")
            
            with get_db() as session:
                # Query market analyses (use created_at, not analysis_timestamp)
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
                
                # Build query - filter by expert instance and time only (no symbol filter)
                query = select(MarketAnalysis).where(
                    MarketAnalysis.expert_instance_id == self.expert_instance_id,
                    MarketAnalysis.created_at >= cutoff_time
                )
                
                # Order by most recent first
                query = query.order_by(MarketAnalysis.created_at.desc())
                
                all_analyses = session.exec(query).all()
                
                # Group analyses by symbol to handle fallback logic
                analyses_by_symbol = {}
                for analysis in all_analyses:
                    if analysis.symbol not in analyses_by_symbol:
                        analyses_by_symbol[analysis.symbol] = []
                    analyses_by_symbol[analysis.symbol].append(analysis)
                
                # Select the best analysis for each symbol (completed, or most recent completed if latest failed)
                selected_analyses = []
                for sym, sym_analyses in analyses_by_symbol.items():
                    # Find first completed analysis (most recent due to ordering)
                    completed_analysis = next(
                        (a for a in sym_analyses if a.status == MarketAnalysisStatus.COMPLETED),
                        None
                    )
                    
                    if completed_analysis:
                        selected_analyses.append(completed_analysis)
                    else:
                        # No completed analysis found within time window - log warning
                        logger.warning(f"No completed analysis found for {sym} within {max_age_hours}h window")
                
                # Build results from selected analyses
                results = []
                for analysis in selected_analyses:
                    # Handle timezone-naive datetime
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    age_hours = (now_utc - analysis.created_at).total_seconds() / 3600
                    
                    # Get expert to call get_analysis_summary
                    try:
                        expert_inst = get_expert_instance_from_id(analysis.expert_instance_id)
                        if expert_inst and hasattr(expert_inst, 'get_analysis_summary'):
                            summary = expert_inst.get_analysis_summary(analysis.id)
                        else:
                            summary = f"Analysis for {analysis.symbol} - Status: {analysis.status}"
                    except Exception as e:
                        logger.error(f"Failed to get summary for analysis {analysis.id}: {e}")
                        summary = f"Analysis for {analysis.symbol} (summary unavailable)"
                    
                    # Get expert name from ExpertInstance
                    expert_name = "Unknown"
                    try:
                        expert_instance = session.get(ExpertInstance, analysis.expert_instance_id)
                        if expert_instance:
                            expert_name = expert_instance.expert
                    except Exception:
                        pass
                    
                    # Get price at analysis time from ExpertRecommendation
                    price_at_analysis = None
                    try:
                        from .models import ExpertRecommendation
                        recommendation = session.exec(
                            select(ExpertRecommendation)
                            .where(ExpertRecommendation.market_analysis_id == analysis.id)
                            .limit(1)
                        ).first()
                        if recommendation and recommendation.price_at_date:
                            price_at_analysis = recommendation.price_at_date
                    except Exception as e:
                        logger.debug(f"Could not get price_at_date for analysis {analysis.id}: {e}")
                    
                    # Calculate price delta if we have the analysis price
                    price_delta_usd = None
                    price_delta_pct = None
                    current_price = None
                    if price_at_analysis:
                        try:
                            current_price = self.account.get_instrument_current_price(analysis.symbol)
                            if current_price:
                                price_delta_usd = round(current_price - price_at_analysis, 2)
                                price_delta_pct = round(((current_price - price_at_analysis) / price_at_analysis) * 100, 2)
                        except Exception as e:
                            logger.debug(f"Could not calculate price delta for {analysis.symbol}: {e}")
                    
                    results.append({
                        "analysis_id": analysis.id,
                        "symbol": analysis.symbol,
                        "timestamp": analysis.created_at.isoformat(),
                        "age_hours": round(age_hours, 1),
                        "expert_name": expert_name,
                        "expert_instance_id": analysis.expert_instance_id,
                        "status": analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status),
                        "summary": summary,
                        "price_at_analysis": price_at_analysis,
                        "current_price": current_price,
                        "price_delta_usd": price_delta_usd,
                        "price_delta_pct": price_delta_pct
                    })
                
                # Sort results by timestamp DESC
                results.sort(key=lambda x: x["timestamp"], reverse=True)
                
                logger.debug(f"Found {len(results)} completed recent analyses")
                
                # PROACTIVE PRICE CACHING: Prefetch prices for all symbols in bulk
                # This populates the cache before agent starts analyzing, reducing individual API calls
                if results:
                    unique_symbols = list(set(r["symbol"] for r in results))
                    logger.debug(f"Proactively prefetching prices for {len(unique_symbols)} symbols from recent analyses")
                    
                    try:
                        # Prefetch bid prices (used by get_current_price which defaults to 'bid')
                        logger.debug(f"Prefetching bid prices for {len(unique_symbols)} symbols in bulk")
                        self.account.get_instrument_current_price(unique_symbols, price_type='bid')
                        
                        # Also prefetch ask prices (may be needed for position analysis)
                        logger.debug(f"Prefetching ask prices for {len(unique_symbols)} symbols in bulk")
                        self.account.get_instrument_current_price(unique_symbols, price_type='ask')
                        
                        logger.debug(f"Proactive price cache populated for {len(unique_symbols)} symbols from recent analyses")
                    except Exception as e:
                        logger.warning(f"Failed to proactively prefetch prices for analyses: {e}")
                
                return results
                
        except Exception as e:
            logger.error(f"Error getting recent analyses: {e}", exc_info=True)
            raise

    def get_analysis_outputs(
        self, 
        analysis_id: Annotated[int, "ID of the MarketAnalysis to get outputs for"]
    ) -> Dict[str, str]:
        """
        Get available outputs for a specific analysis.
        
        Args:
            analysis_id: MarketAnalysis ID
            
        Returns:
            Dict mapping output_key to description
        """
        try:
            logger.debug(f"Getting analysis outputs for analysis {analysis_id}")
            
            with get_db() as session:
                analysis = session.get(MarketAnalysis, analysis_id)
                if not analysis:
                    raise ValueError(f"MarketAnalysis {analysis_id} not found")
                
                # Get expert instance
                expert_inst = get_expert_instance_from_id(analysis.expert_instance_id)
                if not expert_inst:
                    raise ValueError(f"Expert instance {analysis.expert_instance_id} not found")
                
                # Call get_available_outputs
                if hasattr(expert_inst, 'get_available_outputs'):
                    outputs = expert_inst.get_available_outputs(analysis_id)
                    logger.debug(f"Found {len(outputs)} outputs for analysis {analysis_id}")
                    return outputs
                else:
                    logger.warning(f"Expert does not implement get_available_outputs()")
                    return {}
                    
        except Exception as e:
            logger.error(f"Error getting analysis outputs: {e}", exc_info=True)
            raise

    def get_analysis_output_detail(
        self, 
        analysis_id: Annotated[int, "ID of the MarketAnalysis"],
        output_key: Annotated[str, "Key of the output to retrieve"]
    ) -> str:
        """
        Get full detail of a specific analysis output.
        
        Args:
            analysis_id: MarketAnalysis ID
            output_key: Output identifier (from get_analysis_outputs)
            
        Returns:
            Complete output content as string
        """
        try:
            logger.debug(f"Getting output detail for analysis {analysis_id}, output_key={output_key}")
            
            with get_db() as session:
                analysis = session.get(MarketAnalysis, analysis_id)
                if not analysis:
                    raise ValueError(f"MarketAnalysis {analysis_id} not found")
                
                # Get expert instance
                expert_inst = get_expert_instance_from_id(analysis.expert_instance_id)
                if not expert_inst:
                    raise ValueError(f"Expert instance {analysis.expert_instance_id} not found")
                
                # Check if expert implements get_output_detail
                has_method = hasattr(expert_inst, 'get_output_detail')
                is_callable = callable(getattr(expert_inst, 'get_output_detail', None))
                
                logger.debug(f"Expert type: {type(expert_inst).__name__}, has_method: {has_method}, is_callable: {is_callable}")
                
                # Try to call get_output_detail if expert implements it
                if has_method and is_callable:
                    try:
                        detail = expert_inst.get_output_detail(analysis_id, output_key)
                        logger.debug(f"Retrieved output detail from expert.get_output_detail() (length: {len(detail)} chars)")
                        return detail
                    except KeyError as ke:
                        # Expert method raised KeyError - output not found
                        logger.warning(f"Expert.get_output_detail() raised KeyError: {ke}")
                        return f"Output '{output_key}' not available for analysis {analysis_id}: {str(ke)}"
                    except Exception as e:
                        # Other error from expert method - log and fallback
                        logger.error(f"Expert.get_output_detail() failed: {e}", exc_info=True)
                        # Fall through to fallback
                
                # Fallback: Query AnalysisOutput table directly
                logger.debug(f"Using fallback - querying AnalysisOutput table directly")
                
                from sqlmodel import select
                from .models import AnalysisOutput
                
                # Try to find the output by name matching the output_key
                output = session.exec(
                    select(AnalysisOutput)
                    .where(AnalysisOutput.market_analysis_id == analysis_id)
                    .where(AnalysisOutput.name == output_key)
                ).first()
                
                if output and output.text:
                    logger.debug(f"Retrieved output detail from AnalysisOutput table (length: {len(output.text)} chars)")
                    return output.text
                else:
                    # Return a helpful message instead of raising an error
                    return f"Output '{output_key}' not available for analysis {analysis_id}"
                    
        except Exception as e:
            logger.error(f"Error getting analysis output detail: {e}", exc_info=True)
            raise

    def get_analysis_outputs_batch(
        self,
        analysis_ids: Annotated[
            List[int], 
            "List of MarketAnalysis IDs to fetch outputs from"
        ],
        output_keys: Annotated[
            List[str],
            "List of output keys to fetch for each analysis (e.g., ['analysis_summary', 'market_report'])"
        ],
        max_tokens: Annotated[int, "Maximum tokens in response (approximate, using 4 chars/token)"] = 50000
    ) -> Dict[str, Any]:
        """
        Fetch multiple analysis outputs in a single call with automatic truncation.
        
        This method fetches the SAME output keys from ALL specified analyses.
        For example, if you request analysis_ids=[123, 124] and output_keys=['analysis_summary', 'market_report'],
        it will fetch both keys from both analyses (4 outputs total).
        
        Args:
            analysis_ids: List of MarketAnalysis IDs to fetch from
            output_keys: List of output keys to fetch from each analysis
            max_tokens: Maximum tokens in response (default 100K, ~400K chars)
            
        Returns:
            Dict with:
                - outputs: List of dicts with analysis_id, output_key, symbol, content
                - truncated: bool - whether truncation occurred
                - skipped_items: List of items skipped due to size/errors
                - total_chars: Total characters in response
                - total_tokens_estimate: Estimated tokens (chars / 4)
                - items_included: Count of outputs included
                - items_skipped: Count of outputs skipped
                
        Example:
            # Fetch analysis_summary and market_report from analyses 123 and 124
            result = toolkit.get_analysis_outputs_batch(
                analysis_ids=[123, 124],
                output_keys=['analysis_summary', 'market_report']
            )
        """
        try:
            max_chars = max_tokens * 4  # Approximate: 1 token ≈ 4 chars
            
            logger.debug(f"Fetching batch outputs: {len(analysis_ids)} analyses x {len(output_keys)} keys, max_chars={max_chars:,}")
            
            outputs = []
            skipped_items = []
            total_chars = 0
            truncated = False
            
            # Process each analysis_id and output_key combination
            for analysis_id in analysis_ids:
                for output_key in output_keys:
                    # Check if we've exceeded the limit
                    if total_chars >= max_chars:
                        truncated = True
                        skipped_items.append({
                            "analysis_id": analysis_id, 
                            "output_key": output_key, 
                            "reason": "truncated_due_to_size_limit"
                        })
                        logger.debug(f"Truncating at analysis {analysis_id}, key {output_key} (reached {total_chars:,} chars)")
                        continue
                    
                    try:
                        # Use get_analysis_output_detail to fetch the content
                        result = self.get_analysis_output_detail(analysis_id, output_key)
                        
                        # Check if the result indicates an error or missing output
                        if (result.startswith("Error:") or 
                            result.startswith("Output") and "not available" in result or
                            result.startswith("Analysis") and "not found" in result):
                            skipped_items.append({
                                "analysis_id": analysis_id,
                                "output_key": output_key,
                                "reason": result
                            })
                            continue
                        
                        detail_length = len(result)
                        
                        # Check if adding this output would exceed limit
                        if total_chars + detail_length > max_chars:
                            # Try to fit partial content
                            remaining_chars = max_chars - total_chars
                            if remaining_chars > 1000:  # Only include if we can fit at least 1K chars
                                truncated_detail = result[:remaining_chars] + "\n\n<TRUNCATED - Content exceeded size limit>"
                                outputs.append({
                                    "analysis_id": analysis_id,
                                    "output_key": output_key,
                                    "content": truncated_detail,
                                    "truncated": True,
                                    "original_length": detail_length,
                                    "included_length": len(truncated_detail)
                                })
                                total_chars += len(truncated_detail)
                            else:
                                skipped_items.append({
                                    "analysis_id": analysis_id,
                                    "output_key": output_key,
                                    "reason": "insufficient_space_remaining"
                                })
                            
                            truncated = True
                            logger.debug(f"Partially included output for analysis {analysis_id}, key {output_key}")
                        else:
                            # Add full output
                            outputs.append({
                                "analysis_id": analysis_id,
                                "output_key": output_key,
                                "content": result,
                                "truncated": False,
                                "original_length": detail_length,
                                "included_length": detail_length
                            })
                            total_chars += detail_length
                            logger.debug(f"Added output for analysis {analysis_id}, key {output_key} ({detail_length:,} chars)")
                    
                    except Exception as e:
                        logger.error(f"Error fetching output for analysis {analysis_id}, key {output_key}: {e}")
                        skipped_items.append({
                            "analysis_id": analysis_id,
                            "output_key": output_key,
                            "reason": f"error: {str(e)}"
                        })
            
            total_tokens_estimate = total_chars // 4
            
            result = {
                "outputs": outputs,
                "truncated": truncated,
                "skipped_items": skipped_items,
                "total_chars": total_chars,
                "total_tokens_estimate": total_tokens_estimate,
                "items_included": len(outputs),
                "items_skipped": len(skipped_items)
            }
            
            logger.info(f"Batch fetch complete: {len(outputs)} outputs ({total_chars:,} chars, ~{total_tokens_estimate:,} tokens), {len(skipped_items)} skipped, truncated={truncated}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in batch output fetch: {e}", exc_info=True)
            raise

    def get_historical_analyses(
        self,
        symbol: Annotated[str, "Stock symbol to get historical analyses for"],
        limit: Annotated[int, "Maximum number of analyses to return"] = 10,
        offset: Annotated[int, "Number of analyses to skip (for pagination)"] = 0
    ) -> List[Dict[str, Any]]:
        """
        Get historical market analyses for deeper research.
        
        Returns only COMPLETED analyses. If the most recent analysis failed, returns
        the previous completed ones.
        
        Args:
            symbol: Symbol to query
            limit: Max number of results (default 10)
            offset: Skip first N results (for pagination)
            
        Returns:
            List of analysis summaries, ordered by timestamp DESC
        """
        try:
            logger.debug(f"Getting historical analyses for {symbol}, limit={limit}, offset={offset}")
            
            with get_db() as session:
                # Query only COMPLETED analyses
                analyses = session.exec(
                    select(MarketAnalysis)
                    .where(
                        MarketAnalysis.symbol == symbol,
                        MarketAnalysis.status == MarketAnalysisStatus.COMPLETED
                    )
                    .order_by(MarketAnalysis.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                ).all()
                
                results = []
                for analysis in analyses:
                    # Handle timezone-naive datetime
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    age_hours = (now_utc - analysis.created_at).total_seconds() / 3600
                    
                    # Get summary
                    try:
                        expert_inst = get_expert_instance_from_id(analysis.expert_instance_id)
                        if expert_inst and hasattr(expert_inst, 'get_analysis_summary'):
                            summary = expert_inst.get_analysis_summary(analysis.id)
                        else:
                            summary = f"Analysis for {analysis.symbol} - Status: {analysis.status}"
                    except Exception as e:
                        logger.error(f"Failed to get summary for analysis {analysis.id}: {e}")
                        summary = f"Analysis for {analysis.symbol} (summary unavailable)"
                    
                    # Get expert name from ExpertInstance
                    expert_name = "Unknown"
                    try:
                        expert_instance = session.get(ExpertInstance, analysis.expert_instance_id)
                        if expert_instance:
                            expert_name = expert_instance.expert
                    except Exception:
                        pass
                    
                    # Get price at analysis time from ExpertRecommendation
                    price_at_analysis = None
                    try:
                        from .models import ExpertRecommendation
                        recommendation = session.exec(
                            select(ExpertRecommendation)
                            .where(ExpertRecommendation.market_analysis_id == analysis.id)
                            .limit(1)
                        ).first()
                        if recommendation and recommendation.price_at_date:
                            price_at_analysis = recommendation.price_at_date
                    except Exception as e:
                        logger.debug(f"Could not get price_at_date for analysis {analysis.id}: {e}")
                    
                    # Calculate price delta if we have the analysis price
                    price_delta_usd = None
                    price_delta_pct = None
                    current_price = None
                    if price_at_analysis:
                        try:
                            current_price = self.account.get_instrument_current_price(analysis.symbol)
                            if current_price:
                                price_delta_usd = round(current_price - price_at_analysis, 2)
                                price_delta_pct = round(((current_price - price_at_analysis) / price_at_analysis) * 100, 2)
                        except Exception as e:
                            logger.debug(f"Could not calculate price delta for {analysis.symbol}: {e}")
                    
                    results.append({
                        "analysis_id": analysis.id,
                        "symbol": analysis.symbol,
                        "timestamp": analysis.created_at.isoformat(),
                        "age_hours": round(age_hours, 1),
                        "expert_name": expert_name,
                        "expert_instance_id": analysis.expert_instance_id,
                        "status": analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status),
                        "summary": summary,
                        "price_at_analysis": price_at_analysis,
                        "current_price": current_price,
                        "price_delta_usd": price_delta_usd,
                        "price_delta_pct": price_delta_pct
                    })
                
                logger.debug(f"Found {len(results)} completed historical analyses")
                return results
                
        except Exception as e:
            logger.error(f"Error getting historical analyses: {e}", exc_info=True)
            raise
    
    def get_analysis_at_open_time(
        self,
        symbol: Annotated[str, "Stock symbol for the open position"],
        open_time: Annotated[datetime, "Timestamp when the position was opened"]
    ) -> Dict[str, Any]:
        """
        Get the most recent market analysis and Smart Risk Manager job analysis 
        for a symbol just before a position was opened.
        
        This is useful for understanding what analysis led to a position being opened.
        Returns both:
        1. Latest market analysis for the symbol before open_time
        2. Latest Smart Risk Manager job that completed before open_time
        
        Args:
            symbol: Symbol of the open position
            open_time: Timestamp when the position was opened
            
        Returns:
            Dictionary with:
                - market_analysis: Latest analysis for symbol before open_time (or None)
                - risk_manager_job: Latest SRM job before open_time (or None)
                - market_analysis_details: Available outputs from the market analysis
                - risk_manager_summary: Summary from the SRM job
        """
        try:
            logger.debug(f"Getting analysis at open time for {symbol} at {open_time}")
            
            result = {
                "symbol": symbol,
                "open_time": open_time.isoformat() if open_time else None,
                "market_analysis": None,
                "risk_manager_job": None,
                "market_analysis_details": {},
                "risk_manager_summary": None
            }
            
            # Ensure open_time is timezone-naive for comparison
            if open_time and open_time.tzinfo:
                open_time = open_time.replace(tzinfo=None)
            
            with get_db() as session:
                # 1. Get latest market analysis for symbol before open_time
                market_analysis = session.exec(
                    select(MarketAnalysis)
                    .where(
                        MarketAnalysis.symbol == symbol,
                        MarketAnalysis.status == MarketAnalysisStatus.COMPLETED,
                        MarketAnalysis.created_at < open_time
                    )
                    .order_by(MarketAnalysis.created_at.desc())
                    .limit(1)
                ).first()
                
                if market_analysis:
                    # Get expert name
                    expert_name = "Unknown"
                    try:
                        expert_instance = session.get(ExpertInstance, market_analysis.expert_instance_id)
                        if expert_instance:
                            expert_name = expert_instance.expert
                    except Exception:
                        pass
                    
                    # Get summary
                    summary = f"Analysis for {symbol}"
                    try:
                        expert_inst = get_expert_instance_from_id(market_analysis.expert_instance_id)
                        if expert_inst and hasattr(expert_inst, 'get_analysis_summary'):
                            summary = expert_inst.get_analysis_summary(market_analysis.id)
                    except Exception as e:
                        logger.debug(f"Could not get summary for analysis {market_analysis.id}: {e}")
                    
                    result["market_analysis"] = {
                        "analysis_id": market_analysis.id,
                        "symbol": market_analysis.symbol,
                        "created_at": market_analysis.created_at.isoformat(),
                        "expert_name": expert_name,
                        "expert_instance_id": market_analysis.expert_instance_id,
                        "summary": summary
                    }
                    
                    # Get available outputs for this analysis
                    try:
                        outputs_info = self.get_analysis_outputs(market_analysis.id)
                        result["market_analysis_details"] = outputs_info
                    except Exception as e:
                        logger.debug(f"Could not get outputs for analysis {market_analysis.id}: {e}")
                
                # 2. Get latest Smart Risk Manager job before open_time
                srm_job = session.exec(
                    select(SmartRiskManagerJob)
                    .where(
                        SmartRiskManagerJob.account_id == self.account_id,
                        SmartRiskManagerJob.expert_instance_id == self.expert_instance_id,
                        SmartRiskManagerJob.status == "COMPLETED",
                        SmartRiskManagerJob.run_date < open_time
                    )
                    .order_by(SmartRiskManagerJob.run_date.desc())
                    .limit(1)
                ).first()
                
                if srm_job:
                    result["risk_manager_job"] = {
                        "job_id": srm_job.id,
                        "run_date": srm_job.run_date.isoformat(),
                        "model_used": srm_job.model_used,
                        "iteration_count": srm_job.iteration_count,
                        "actions_taken_count": srm_job.actions_taken_count,
                        "initial_equity": srm_job.initial_portfolio_equity,
                        "final_equity": srm_job.final_portfolio_equity
                    }
                    result["risk_manager_summary"] = srm_job.actions_summary
                
                logger.debug(f"Found market_analysis={result['market_analysis'] is not None}, "
                           f"srm_job={result['risk_manager_job'] is not None}")
                
                return result
                
        except Exception as e:
            logger.error(f"Error getting analysis at open time: {e}", exc_info=True)
            raise
    
    def get_last_risk_manager_summary(self) -> Dict[str, Any]:
        """
        Get the summary from the last completed Smart Risk Manager job for this expert.
        
        This provides historical context about what the risk manager analyzed and 
        recommended in the previous run, helping maintain continuity across runs.
        
        Returns:
            Dictionary with:
                - job_id: ID of the last completed job (or None)
                - run_date: When the job was executed
                - research_findings: Research node output from graph_state
                - final_summary: Finalization node output from graph_state
                - actions_summary: Summary of actions taken
                - actions_taken_count: Number of actions executed
                - iteration_count: Number of iterations
                - initial_equity: Portfolio value at start
                - final_equity: Portfolio value at end
        """
        try:
            logger.debug(f"Getting last risk manager summary for expert {self.expert_instance_id}")
            
            result = {
                "job_id": None,
                "run_date": None,
                "research_findings": None,
                "final_summary": None,
                "actions_summary": None,
                "actions_taken_count": 0,
                "iteration_count": 0,
                "initial_equity": None,
                "final_equity": None
            }
            
            with get_db() as session:
                # Get the most recent completed SRM job for this expert
                srm_job = session.exec(
                    select(SmartRiskManagerJob)
                    .where(
                        SmartRiskManagerJob.account_id == self.account_id,
                        SmartRiskManagerJob.expert_instance_id == self.expert_instance_id,
                        SmartRiskManagerJob.status == "COMPLETED"
                    )
                    .order_by(SmartRiskManagerJob.run_date.desc())
                    .limit(1)
                ).first()
                
                if srm_job:
                    result["job_id"] = srm_job.id
                    result["run_date"] = srm_job.run_date.isoformat() if srm_job.run_date else None
                    result["actions_summary"] = srm_job.actions_summary
                    result["actions_taken_count"] = srm_job.actions_taken_count or 0
                    result["iteration_count"] = srm_job.iteration_count or 0
                    result["initial_equity"] = srm_job.initial_portfolio_equity
                    result["final_equity"] = srm_job.final_portfolio_equity
                    
                    # Extract research findings and final summary from graph_state
                    if srm_job.graph_state:
                        result["research_findings"] = srm_job.graph_state.get("research_findings")
                        result["final_summary"] = srm_job.graph_state.get("final_summary")
                    
                    logger.debug(f"Found last SRM job {srm_job.id} from {result['run_date']}")
                else:
                    logger.debug("No previous SRM job found for this expert")
                
                return result
                
        except Exception as e:
            logger.error(f"Error getting last risk manager summary: {e}", exc_info=True)
            raise
    
    # ==================== Trading Action Tools ====================

    def close_position(
        self, 
        transaction_id: Annotated[int, "ID of Transaction to close"],
        reason: Annotated[str, "Explanation for closing the position"]
    ) -> Dict[str, Any]:
        """
        Close an open position completely.
        
        Args:
            transaction_id: ID of Transaction to close
            reason: Explanation for closure (logged)
            
        Returns:
            Result dict with success, message, order_id, transaction_id
        """
        try:
            logger.info(f"Closing position {transaction_id}. Reason: {reason}")
            
            # SECURITY CHECK: Validate transaction belongs to this expert
            try:
                transaction = self._validate_transaction_ownership(transaction_id)
            except ValueError as e:
                logger.error(f"Transaction ownership validation failed: {e}")
                return {
                    "success": False,
                    "message": str(e),
                    "order_id": None,
                    "transaction_id": transaction_id
                }
            
            with get_db() as session:
                # Refresh transaction in this session
                transaction = session.get(Transaction, transaction_id)
                
                if transaction.status != TransactionStatus.OPENED:
                    return {
                        "success": False,
                        "message": f"Transaction #{transaction_id} is not open (status: {transaction.status})",
                        "order_id": None,
                        "transaction_id": transaction_id
                    }
                
                # Close via account interface
                result = self.account.close_transaction(transaction_id)
                
                if result.get("success"):
                    logger.info(f"Successfully closed position {transaction_id}")
                else:
                    logger.error(f"Failed to close position {transaction_id}: {result.get('message')}")
                
                return result
                
        except Exception as e:
            logger.error(f"Error closing position {transaction_id}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "order_id": None,
                "transaction_id": transaction_id
            }

    def get_current_prices(
        self, 
        symbols: Annotated[List[str], "List of instrument symbols to get current prices for"]
    ) -> Dict[str, Any]:
        """
        Get current market prices for multiple symbols at once.
        
        This is more efficient than calling get_current_price multiple times
        as it batches the requests.
        
        Args:
            symbols: List of instrument symbols (e.g., ["AAPL", "MSFT", "GOOGL"])
                     Also accepts a single string symbol for convenience.
            
        Returns:
            Dict with "prices" dict mapping symbol to price, and "errors" list for any failures
        """
        # Handle case where model passes a single symbol string instead of list
        if isinstance(symbols, str):
            symbols = [symbols]

        results = {
            "prices": {},
            "errors": []
        }

        try:
            # Use bulk API: pass full list for a single API call
            price_map = self.account.get_instrument_current_price(symbols, price_type='bid')

            if isinstance(price_map, dict):
                for symbol, price in price_map.items():
                    if price is not None:
                        results["prices"][symbol] = float(price)
                    else:
                        results["errors"].append({"symbol": symbol, "error": "No price available"})
            else:
                # Fallback: if single symbol was passed and result is a float
                if len(symbols) == 1 and price_map is not None:
                    results["prices"][symbols[0]] = float(price_map)
                else:
                    results["errors"].append({"symbol": str(symbols), "error": "Unexpected response format"})
        except Exception as e:
            logger.error(f"Error in bulk price fetch for {symbols}: {e}", exc_info=True)
            # Fallback to individual fetches
            for symbol in symbols:
                try:
                    price = self.account.get_instrument_current_price(symbol, price_type='bid')
                    if price is not None and not isinstance(price, dict):
                        results["prices"][symbol] = float(price)
                    else:
                        results["errors"].append({"symbol": symbol, "error": "No price available"})
                except Exception as inner_e:
                    results["errors"].append({"symbol": symbol, "error": str(inner_e)})

        logger.info(f"Got prices for {len(results['prices'])} symbols, {len(results['errors'])} errors")
        return results

    def get_current_price(
        self, 
        symbol: Annotated[str, "Instrument symbol to get current price for"]
    ) -> float:
        """
        Get current market price for a single symbol.
        
        For multiple symbols, use get_current_prices() instead for efficiency.
        
        Args:
            symbol: Instrument symbol
            
        Returns:
            Current price as float
        """
        try:
            logger.debug(f"Getting current price for {symbol}")
            price = self.account.get_instrument_current_price(symbol, price_type='bid')
            
            # Handle case where method might return dict instead of float
            if isinstance(price, dict):
                logger.error(f"get_instrument_current_price returned dict instead of float for {symbol}: {price}")
                raise ValueError(f"Expected float price for {symbol}, got dict: {price}")
            
            if price is None:
                logger.error(f"get_instrument_current_price returned None for {symbol}")
                raise ValueError(f"No price available for {symbol}")
            
            logger.debug(f"Current price for {symbol}: {price}")
            return float(price)
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            raise

    def adjust_quantity(
        self,
        transaction_id: Annotated[int, "ID of the position to adjust"],
        new_quantity: Annotated[float, "New absolute quantity for the position"],
        reason: Annotated[str, "Reason for the adjustment"]
    ) -> Dict[str, Any]:
        """
        Adjust position size (partial close or add to position).
        
        This method handles Alpaca's "held_for_orders" constraint by properly sequencing
        TP/SL order cancellations and new order creation.
        
        Args:
            transaction_id: ID of Transaction to adjust
            new_quantity: New total quantity (can be < or > current)
            reason: Explanation for adjustment
            
        Returns:
            Result dict with success, message, order_id, old_quantity, new_quantity
        """
        try:
            logger.info(f"Adjusting quantity for transaction {transaction_id} to {new_quantity}. Reason: {reason}")
            
            # SECURITY CHECK: Validate transaction belongs to this expert
            try:
                self._validate_transaction_ownership(transaction_id)
            except ValueError as e:
                logger.error(f"Transaction ownership validation failed: {e}")
                return {
                    "success": False,
                    "message": str(e),
                    "order_id": None,
                    "old_quantity": None,
                    "new_quantity": new_quantity
                }
            
            with get_db() as session:
                transaction = session.get(Transaction, transaction_id)
                if not transaction:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} not found",
                        "order_id": None,
                        "old_quantity": None,
                        "new_quantity": new_quantity
                    }
                
                if transaction.status != TransactionStatus.OPENED:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not OPENED (status: {transaction.status.value}). Can only adjust quantity for filled positions.",
                        "order_id": None,
                        "old_quantity": transaction.quantity,
                        "new_quantity": new_quantity
                    }
                
                old_quantity = transaction.quantity
                
                if new_quantity <= 0:
                    return {
                        "success": False,
                        "message": "New quantity must be greater than 0",
                        "order_id": None,
                        "old_quantity": old_quantity,
                        "new_quantity": new_quantity
                    }
                
                if new_quantity == old_quantity:
                    return {
                        "success": True,
                        "message": "No change in quantity",
                        "order_id": None,
                        "old_quantity": old_quantity,
                        "new_quantity": new_quantity
                    }
                
                # Calculate quantity delta
                qty_change = new_quantity - old_quantity  # negative for decrease, positive for increase
                
                # Check if adding to position is allowed based on expert settings
                if qty_change > 0:
                    # Get transaction direction
                    if not transaction.trading_orders:
                        return {
                            "success": False,
                            "message": "Transaction has no orders - cannot determine direction",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    
                    entry_direction = transaction.trading_orders[0].side
                    
                    if entry_direction == OrderDirection.BUY and not self.expert.get_setting_with_interface_default("enable_buy"):
                        return {
                            "success": False,
                            "message": "Cannot add to long position: BUY orders are disabled in expert settings",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    if entry_direction == OrderDirection.SELL and not self.expert.get_setting_with_interface_default("enable_sell"):
                        return {
                            "success": False,
                            "message": "Cannot add to short position: SELL orders are disabled in expert settings",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                
                # Use TransactionHelper to handle TP/SL order sequencing
                result = TransactionHelper.adjust_quantity_with_tpsl(
                    account=self.account,
                    transaction=transaction,
                    qty_change=qty_change,
                    expert_id=self.expert_instance_id
                )
                
                if result["success"]:
                    logger.info(f"Successfully adjusted position from {old_quantity} to {new_quantity}")
                    return {
                        "success": True,
                        "message": result["message"],
                        "order_id": result["orders_created"][0] if result["orders_created"] else None,
                        "old_quantity": old_quantity,
                        "new_quantity": new_quantity,
                        "orders_created": result["orders_created"],
                        "orders_canceled": result["orders_canceled"]
                    }
                else:
                    return {
                        "success": False,
                        "message": result["message"],
                        "order_id": None,
                        "old_quantity": old_quantity,
                        "new_quantity": new_quantity
                    }
                    
        except Exception as e:
            logger.error(f"Error adjusting quantity for transaction {transaction_id}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "order_id": None,
                "old_quantity": None,
                "new_quantity": new_quantity
            }

    def update_stop_loss(
        self,
        transaction_id: Annotated[int, "ID of the position to update stop loss for"],
        new_sl_price: Annotated[float, "New stop loss price"],
        reason: Annotated[str, "Reason for updating the stop loss"]
    ) -> Dict[str, Any]:
        """
        Update stop loss order for a position.
        
        Supports both WAITING and OPENED transactions:
        - WAITING: Updates SL in database only (entry order not submitted to broker yet)
        - OPENED: Modifies actual broker orders via account interface
        
        Args:
            transaction_id: ID of Transaction
            new_sl_price: New stop loss price
            reason: Explanation for change
            
        Returns:
            Result dict with success, message, order_id, old_sl_price, new_sl_price
        """
        try:
            logger.info(f"Updating stop loss for transaction {transaction_id} to {new_sl_price}. Reason: {reason}")
            
            # SECURITY CHECK: Validate transaction belongs to this expert
            try:
                self._validate_transaction_ownership(transaction_id)
            except ValueError as e:
                logger.error(f"Transaction ownership validation failed: {e}")
                return {
                    "success": False,
                    "message": str(e),
                    "order_id": None,
                    "old_sl_price": None,
                    "new_sl_price": new_sl_price
                }
            
            with get_db() as session:
                transaction = session.get(Transaction, transaction_id)
                if not transaction:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} not found",
                        "order_id": None,
                        "old_sl_price": None,
                        "new_sl_price": new_sl_price
                    }
                
                # Allow modification for WAITING or OPENED transactions
                if transaction.status not in [TransactionStatus.WAITING, TransactionStatus.OPENED]:
                    return {
                        "success": False,
                        "message": f"Cannot modify SL for {transaction.status} transaction. Only WAITING or OPENED allowed.",
                        "order_id": None,
                        "old_sl_price": None,
                        "new_sl_price": new_sl_price
                    }
                
                # Get entry order
                if not transaction.trading_orders:
                    return {
                        "success": False,
                        "message": "Transaction has no orders - cannot update SL",
                        "order_id": None,
                        "old_sl_price": None,
                        "new_sl_price": new_sl_price
                    }
                
                entry_order = transaction.trading_orders[0]
                old_sl_price = transaction.stop_loss
                
                # Validate new SL price
                current_price = self.account.get_instrument_current_price(transaction.symbol)
                if current_price is None:
                    return {
                        "success": False,
                        "message": f"Could not get current price for {transaction.symbol}",
                        "order_id": None,
                        "old_sl_price": old_sl_price,
                        "new_sl_price": new_sl_price
                    }
                
                if entry_order.side == OrderDirection.BUY:
                    # For long positions, SL must be below current price
                    if new_sl_price >= current_price:
                        return {
                            "success": False,
                            "message": f"Stop loss price {new_sl_price:.2f} must be below current price {current_price:.2f} for long position",
                            "order_id": None,
                            "old_sl_price": old_sl_price,
                            "new_sl_price": new_sl_price
                        }
                else:  # SELL
                    # For short positions, SL must be above current price
                    if new_sl_price <= current_price:
                        return {
                            "success": False,
                            "message": f"Stop loss price {new_sl_price:.2f} must be above current price {current_price:.2f} for short position",
                            "order_id": None,
                            "old_sl_price": old_sl_price,
                            "new_sl_price": new_sl_price
                        }
                
                # Use AccountInterface's adjust_sl method - stateless operation that determines action based on current state
                try:
                    success = self.account.adjust_sl(transaction, new_sl_price, source="smart_risk_manager")

                    if success:
                        logger.info(f"Successfully adjusted stop loss from {old_sl_price} to {new_sl_price}")
                        return {
                            "success": True,
                            "message": f"Successfully adjusted stop loss to {new_sl_price:.2f}",
                            "order_id": None,  # adjust_sl doesn't return order ID
                            "old_sl_price": old_sl_price,
                            "new_sl_price": new_sl_price
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Failed to adjust SL",
                            "order_id": None,
                            "old_sl_price": old_sl_price,
                            "new_sl_price": new_sl_price
                        }
                except Exception as e:
                    logger.error(f"Error calling account.adjust_sl: {e}", exc_info=True)
                    return {
                        "success": False,
                        "message": f"Error updating SL: {str(e)}",
                        "order_id": None,
                        "old_sl_price": old_sl_price,
                        "new_sl_price": new_sl_price
                    }
                    
        except Exception as e:
            logger.error(f"Error updating stop loss for transaction {transaction_id}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "order_id": None,
                "old_sl_price": None,
                "new_sl_price": new_sl_price
            }

    def update_take_profit(
        self,
        transaction_id: Annotated[int, "ID of the position to update take profit for"],
        new_tp_price: Annotated[float, "New take profit price"],
        reason: Annotated[str, "Reason for updating the take profit"]
    ) -> Dict[str, Any]:
        """
        Update take profit order for a position.
        
        Supports both WAITING and OPENED transactions:
        - WAITING: Updates TP in database only (entry order not submitted to broker yet)
        - OPENED: Modifies actual broker orders via account interface
        
        Args:
            transaction_id: ID of Transaction
            new_tp_price: New take profit price
            reason: Explanation for change
            
        Returns:
            Result dict with success, message, order_id, old_tp_price, new_tp_price
        """
        try:
            logger.info(f"Updating take profit for transaction {transaction_id} to {new_tp_price}. Reason: {reason}")
            
            # SECURITY CHECK: Validate transaction belongs to this expert
            try:
                self._validate_transaction_ownership(transaction_id)
            except ValueError as e:
                logger.error(f"Transaction ownership validation failed: {e}")
                return {
                    "success": False,
                    "message": str(e),
                    "order_id": None,
                    "old_tp_price": None,
                    "new_tp_price": new_tp_price
                }
            
            with get_db() as session:
                transaction = session.get(Transaction, transaction_id)
                if not transaction:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} not found",
                        "order_id": None,
                        "old_tp_price": None,
                        "new_tp_price": new_tp_price
                    }
                
                # Allow modification for WAITING or OPENED transactions
                if transaction.status not in [TransactionStatus.WAITING, TransactionStatus.OPENED]:
                    return {
                        "success": False,
                        "message": f"Cannot modify TP for {transaction.status} transaction. Only WAITING or OPENED allowed.",
                        "order_id": None,
                        "old_tp_price": None,
                        "new_tp_price": new_tp_price
                    }
                
                # Get entry order
                if not transaction.trading_orders:
                    return {
                        "success": False,
                        "message": "Transaction has no orders - cannot update TP",
                        "order_id": None,
                        "old_tp_price": None,
                        "new_tp_price": new_tp_price
                    }
                
                entry_order = transaction.trading_orders[0]
                old_tp_price = transaction.take_profit
                
                # Validate new TP price
                current_price = self.account.get_instrument_current_price(transaction.symbol)
                if current_price is None:
                    return {
                        "success": False,
                        "message": f"Could not get current price for {transaction.symbol}",
                        "order_id": None,
                        "old_tp_price": old_tp_price,
                        "new_tp_price": new_tp_price
                    }
                
                if entry_order.side == OrderDirection.BUY:
                    # For long positions, TP must be above current price
                    if new_tp_price <= current_price:
                        return {
                            "success": False,
                            "message": f"Take profit price {new_tp_price:.2f} must be above current price {current_price:.2f} for long position",
                            "order_id": None,
                            "old_tp_price": old_tp_price,
                            "new_tp_price": new_tp_price
                        }
                else:  # SELL
                    # For short positions, TP must be below current price
                    if new_tp_price >= current_price:
                        return {
                            "success": False,
                            "message": f"Take profit price {new_tp_price:.2f} must be below current price {current_price:.2f} for short position",
                            "order_id": None,
                            "old_tp_price": old_tp_price,
                            "new_tp_price": new_tp_price
                        }
                
                # Use AccountInterface's adjust_tp method - stateless operation that determines action based on current state
                try:
                    success = self.account.adjust_tp(transaction, new_tp_price, source="smart_risk_manager")

                    if success:
                        logger.info(f"Successfully adjusted take profit from {old_tp_price} to {new_tp_price}")
                        return {
                            "success": True,
                            "message": f"Successfully adjusted take profit to {new_tp_price:.2f}",
                            "order_id": None,  # adjust_tp doesn't return order ID
                            "old_tp_price": old_tp_price,
                            "new_tp_price": new_tp_price
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Failed to adjust TP",
                            "order_id": None,
                            "old_tp_price": old_tp_price,
                            "new_tp_price": new_tp_price
                        }
                except Exception as e:
                    logger.error(f"Error calling account.adjust_tp: {e}", exc_info=True)
                    return {
                        "success": False,
                        "message": f"Error updating TP: {str(e)}",
                        "order_id": None,
                        "old_tp_price": old_tp_price,
                        "new_tp_price": new_tp_price
                    }
                    
        except Exception as e:
            logger.error(f"Error updating take profit for transaction {transaction_id}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "order_id": None,
                "old_tp_price": None,
                "new_tp_price": new_tp_price
            }

    def open_buy_position(
        self,
        symbol: Annotated[str, "Instrument symbol to buy"],
        quantity: Annotated[float, "Number of shares/units to buy"],
        tp_price: Annotated[Optional[float], "Optional take profit price (must be above entry price)"] = None,
        sl_price: Annotated[Optional[float], "Optional stop loss price (must be below entry price)"] = None,
        reason: Annotated[str, "Reason for opening this long position"] = ""
    ) -> Dict[str, Any]:
        """
        Open a new LONG (BUY) position.
        
        Args:
            symbol: Instrument symbol to buy
            quantity: Number of shares to buy
            tp_price: Take profit price (optional, must be above entry)
            sl_price: Stop loss price (optional, must be below entry)
            reason: Explanation for opening this long position
            
        Returns:
            Result dict with success, message, transaction_id, order_id, symbol, quantity, direction
        """
        return self._open_position_internal(symbol, OrderDirection.BUY, quantity, tp_price, sl_price, reason)
    
    def open_sell_position(
        self,
        symbol: Annotated[str, "Instrument symbol to sell short"],
        quantity: Annotated[float, "Number of shares/units to sell short"],
        tp_price: Annotated[Optional[float], "Optional take profit price (must be below entry price)"] = None,
        sl_price: Annotated[Optional[float], "Optional stop loss price (must be above entry price)"] = None,
        reason: Annotated[str, "Reason for opening this short position"] = ""
    ) -> Dict[str, Any]:
        """
        Open a new SHORT (SELL) position.
        
        Args:
            symbol: Instrument symbol to sell short
            quantity: Number of shares to sell short
            tp_price: Take profit price (optional, must be below entry)
            sl_price: Stop loss price (optional, must be above entry)
            reason: Explanation for opening this short position
            
        Returns:
            Result dict with success, message, transaction_id, order_id, symbol, quantity, direction
        """
        return self._open_position_internal(symbol, OrderDirection.SELL, quantity, tp_price, sl_price, reason)
    
    def _open_position_internal(
        self,
        symbol: str,
        order_direction: OrderDirection,
        quantity: float,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        reason: str = ""
    ) -> Dict[str, Any]:
        """
        Internal method to open a new trading position.
        
        Args:
            symbol: Instrument symbol
            order_direction: OrderDirection.BUY or OrderDirection.SELL
            quantity: Position size
            tp_price: Take profit price (optional)
            sl_price: Stop loss price (optional)
            reason: Explanation for opening position
            
        Returns:
            Result dict with success, message, transaction_id, order_id, symbol, quantity, direction
        """
        try:
            direction = order_direction.value
            
            # Validate quantity is greater than 0
            if quantity <= 0:
                return {
                    "success": False,
                    "message": f"Invalid quantity: {quantity}. Quantity must be greater than 0",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            logger.info(f"Opening new {direction} position for {symbol}, quantity={quantity}. Reason: {reason}")
            
            # Check for existing open transaction on this symbol
            with get_db() as session:
                existing_transaction = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id == self.expert_instance_id)
                    .where(Transaction.symbol == symbol)
                    .where(Transaction.status == TransactionStatus.OPENED)
                ).first()
                
                if existing_transaction:
                    # Check if it's the same direction
                    existing_direction = existing_transaction.side
                    if existing_direction == order_direction:
                        return {
                            "success": False,
                            "message": f"Cannot open new position: An open {existing_direction.value} position already exists for {symbol} (transaction_id={existing_transaction.id}). Use adjust_quantity to modify the existing position instead.",
                            "transaction_id": existing_transaction.id,
                            "order_id": None,
                            "symbol": symbol,
                            "quantity": quantity,
                            "direction": direction
                        }
                    else:
                        # Opposite direction - check if hedging is allowed
                        allow_hedging = self.expert.get_setting_with_interface_default("allow_hedging")
                        if not allow_hedging:
                            return {
                                "success": False,
                                "message": f"Cannot open {direction} position: An open {existing_direction.value} position already exists for {symbol} (transaction_id={existing_transaction.id}). Hedging is disabled. Enable 'Allow hedging' in expert settings or close the existing position first.",
                                "transaction_id": existing_transaction.id,
                                "order_id": None,
                                "symbol": symbol,
                                "quantity": quantity,
                                "direction": direction
                            }
                        # Hedging is allowed - continue with position opening
                        self.logger.info(f"Hedging enabled: Opening {direction} position while {existing_direction.value} position exists for {symbol}")
            
            # Check if symbol is enabled in expert settings
            # Skip check for dynamic/expert instrument selection modes
            enabled_instruments = self.expert.get_enabled_instruments()
            if enabled_instruments not in [["DYNAMIC"], ["EXPERT"]] and symbol not in enabled_instruments:
                return {
                    "success": False,
                    "message": f"Symbol {symbol} is not enabled in expert settings",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            # Check enable_buy/enable_sell settings
            settings = self.expert.settings
            if order_direction == OrderDirection.BUY and not self.expert.get_setting_with_interface_default("enable_buy"):
                return {
                    "success": False,
                    "message": "Buy orders are disabled in expert settings",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            if order_direction == OrderDirection.SELL and not self.expert.get_setting_with_interface_default("enable_sell"):
                return {
                    "success": False,
                    "message": "Sell orders are disabled in expert settings",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            # Check expert's available balance (virtual equity minus used balance)
            available_balance = self.expert.get_available_balance()
            if available_balance is None:
                return {
                    "success": False,
                    "message": "Could not calculate expert's available balance",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            # Get expert's virtual equity (percentage of total account equity)
            virtual_equity = self.expert.get_virtual_balance()
            if virtual_equity is None:
                return {
                    "success": False,
                    "message": "Could not calculate expert's virtual balance",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            current_price = self.account.get_instrument_current_price(symbol)
            if current_price is None:
                return {
                    "success": False,
                    "message": f"Could not get current price for {symbol}",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            position_value = current_price * quantity
            
            # Check if expert has enough available balance for this position
            if position_value > available_balance:
                return {
                    "success": False,
                    "message": f"Insufficient expert balance: position value ${position_value:.2f} > available ${available_balance:.2f}. Close existing positions or increase virtual equity %.",
                    "transaction_id": None,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            # Check position size limits and adjust if necessary
            settings = self.expert.settings
            max_position_pct = self.expert.get_setting_with_interface_default("max_virtual_equity_per_instrument_percent")
            max_position_value = virtual_equity * (max_position_pct / 100.0)
            
            original_quantity = quantity
            quantity_was_adjusted = False
            
            if position_value > max_position_value:
                # Calculate maximum allowed quantity
                max_allowed_quantity = max_position_value / current_price
                
                # Enforce minimum quantity of 1 if stock price is less than 50% of virtual equity
                if current_price < (virtual_equity * 0.5) and max_allowed_quantity < 1:
                    max_allowed_quantity = 1
                    logger.warning(f"Position size would be less than 1 share, but stock price ${current_price:.2f} is less than 50% of equity ${virtual_equity:.2f}. Setting minimum quantity to 1.")
                
                # Round down to avoid exceeding limit
                adjusted_quantity = int(max_allowed_quantity)
                
                if adjusted_quantity < 1:
                    return {
                        "success": False,
                        "message": f"Position size {position_value:.2f} exceeds max allowed {max_position_value:.2f} ({max_position_pct}% of equity). Cannot reduce to minimum 1 share without exceeding limit (stock price ${current_price:.2f} too high).",
                        "transaction_id": None,
                        "order_id": None,
                        "symbol": symbol,
                        "quantity": quantity,
                        "direction": direction
                    }
                
                quantity = adjusted_quantity
                position_value = current_price * quantity
                quantity_was_adjusted = True
                logger.info(f"Automatically adjusted quantity from {original_quantity} to {quantity} to respect max position size limit ({max_position_pct}% of equity = ${max_position_value:.2f})")
            
            # Create transaction BEFORE submitting orders
            # Safety check: ensure quantity is positive
            if quantity < 0:
                logger.error(
                    f"NEGATIVE QUANTITY DETECTED in SmartRiskManager._open_position_internal: {quantity} "
                    f"for {symbol} {order_direction}. Using abs() as safety measure.",
                    exc_info=True
                )
                quantity = abs(quantity)
            
            transaction = Transaction(
                symbol=symbol,
                quantity=abs(float(quantity)),  # Always positive, with abs() safety
                side=order_direction,  # BUY for LONG, SELL for SHORT
                open_price=current_price,  # Estimated open price
                status=TransactionStatus.WAITING,
                created_at=datetime.now(timezone.utc),
                expert_id=self.expert_instance_id  # Link to Smart Risk Manager expert
            )
            
            transaction_id = add_instance(transaction)
            logger.info(f"Created transaction {transaction_id} for {symbol} {direction} position (expert_id={self.expert_instance_id})")
            
            # Create and submit market order linked to transaction
            entry_order = self._create_trading_order(
                symbol=symbol,
                quantity=quantity,
                side=order_direction,
                order_type=OrderType.MARKET,
                transaction_id=transaction_id,
                comment=f"New position: {reason}"
            )
            
            # Submit order with TP/SL as bracket order if provided
            try:
                # Pass TP/SL prices to submit_order for bracket order creation
                submitted_order = self.account.submit_order(entry_order, tp_price=tp_price, sl_price=sl_price)
                
                if not submitted_order or not submitted_order.id:
                    # Mark transaction as FAILED if order submission failed
                    with get_db() as session:
                        trans = session.get(Transaction, transaction_id)
                        if trans:
                            trans.status = TransactionStatus.FAILED
                            session.add(trans)
                            session.commit()
                    
                    # Get actual error from order comment if available
                    error_detail = "Failed to submit entry order"
                    if entry_order.id:
                        fresh_order = get_instance(TradingOrder, entry_order.id)
                        if fresh_order and fresh_order.comment:
                            error_detail = fresh_order.comment
                    
                    # Log failed transaction creation
                    from .utils import log_transaction_created_activity
                    log_transaction_created_activity(
                        trading_order=entry_order,
                        account_id=self.account_id,
                        transaction_id=transaction_id,
                        expert_id=self.expert_instance_id,
                        current_price=current_price,
                        success=False,
                        error_message=error_detail
                    )
                    
                    return {
                        "success": False,
                        "message": f"Failed to submit entry order for {symbol}",
                        "transaction_id": transaction_id,
                        "order_id": None,
                        "symbol": symbol,
                        "quantity": quantity,
                        "direction": direction
                    }
                
                order_id = submitted_order.id
                logger.info(f"Successfully opened position: transaction_id={transaction_id}, order_id={order_id}")
                
                # Log successful transaction creation
                from .utils import log_transaction_created_activity
                log_transaction_created_activity(
                    trading_order=entry_order,
                    account_id=self.account_id,
                    transaction_id=transaction_id,
                    expert_id=self.expert_instance_id,
                    current_price=current_price,
                    success=True
                )
                
            except Exception as e:
                # Mark transaction as FAILED if order submission raised exception
                with get_db() as session:
                    trans = session.get(Transaction, transaction_id)
                    if trans:
                        trans.status = TransactionStatus.FAILED
                        session.add(trans)
                        session.commit()
                
                # Log failed transaction creation
                from .utils import log_transaction_created_activity
                log_transaction_created_activity(
                    trading_order=entry_order,
                    account_id=self.account_id,
                    transaction_id=transaction_id,
                    expert_id=self.expert_instance_id,
                    current_price=current_price,
                    success=False,
                    error_message=str(e)
                )
                
                logger.error(f"Error submitting entry order for {symbol}: {e}", exc_info=True)
                return {
                    "success": False,
                    "message": f"Error submitting entry order for {symbol}: {str(e)}",
                    "transaction_id": transaction_id,
                    "order_id": None,
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": direction
                }
            
            # Use AccountInterface's built-in TP/SL adjustment methods (stateless, handles all order states)
            tp_created = False
            sl_created = False
            tp_warning = None
            sl_warning = None
            
            if transaction_id and submitted_order:
                # Validate TP/SL prices before attempting to set them
                tp_valid = True
                sl_valid = True
                
                if tp_price:
                    if order_direction == OrderDirection.BUY and tp_price <= current_price:
                        tp_valid = False
                        tp_warning = f"TP price {tp_price:.2f} is not above current price {current_price:.2f} for BUY order"
                    elif order_direction == OrderDirection.SELL and tp_price >= current_price:
                        tp_valid = False
                        tp_warning = f"TP price {tp_price:.2f} is not below current price {current_price:.2f} for SELL order"
                
                if sl_price:
                    if order_direction == OrderDirection.BUY and sl_price >= current_price:
                        sl_valid = False
                        sl_warning = f"SL price {sl_price:.2f} is not below current price {current_price:.2f} for BUY order"
                    elif order_direction == OrderDirection.SELL and sl_price <= current_price:
                        sl_valid = False
                        sl_warning = f"SL price {sl_price:.2f} is not above current price {current_price:.2f} for SELL order"
                
                # Get fresh transaction object for adjustment methods
                with get_db() as session:
                    transaction = session.get(Transaction, transaction_id)
                
                # Use AccountInterface adjustment methods (stateless, transaction-based)
                try:
                    if tp_price and tp_valid and sl_price and sl_valid:
                        # Set both TP and SL together
                        logger.info(f"Adjusting TP to {tp_price:.2f} and SL to {sl_price:.2f} using account.adjust_tp_sl()")
                        success = self.account.adjust_tp_sl(transaction, tp_price, sl_price, source="smart_risk_manager")
                        if success:
                            tp_created = True
                            sl_created = True
                        else:
                            error_msg = "adjust_tp_sl() returned False - likely entry order not yet filled"
                            logger.warning(error_msg)
                            tp_warning = error_msg
                            sl_warning = error_msg
                    elif tp_price and tp_valid:
                        # Set TP only
                        logger.info(f"Adjusting TP to {tp_price:.2f} using account.adjust_tp()")
                        success = self.account.adjust_tp(transaction, tp_price, source="smart_risk_manager")
                        if success:
                            tp_created = True
                        else:
                            tp_warning = "adjust_tp() returned False - likely entry order not yet filled"
                            logger.warning(tp_warning)
                    elif sl_price and sl_valid:
                        # Set SL only
                        logger.info(f"Adjusting SL to {sl_price:.2f} using account.adjust_sl()")
                        success = self.account.adjust_sl(transaction, sl_price, source="smart_risk_manager")
                        if success:
                            sl_created = True
                        else:
                            sl_warning = "adjust_sl() returned False - likely entry order not yet filled"
                            logger.warning(sl_warning)
                except Exception as e:
                    error_msg = f"Failed to adjust TP/SL: {str(e)}"
                    logger.error(error_msg, exc_info=True)
                    if tp_price and not tp_warning:
                        tp_warning = error_msg
                    if sl_price and not sl_warning:
                        sl_warning = error_msg
            
            # Build detailed success message
            tp_sl_info = []
            warnings = []
            if tp_created:
                tp_sl_info.append(f"TP@{tp_price:.2f}")
            elif tp_price and tp_warning:
                warnings.append(f"TP not created: {tp_warning}")
            
            if sl_created:
                tp_sl_info.append(f"SL@{sl_price:.2f}")
            elif sl_price and sl_warning:
                warnings.append(f"SL not created: {sl_warning}")
            
            tp_sl_text = f" with {', '.join(tp_sl_info)}" if tp_sl_info else ""
            warning_text = f" Warnings: {'; '.join(warnings)}" if warnings else ""
            
            # Add quantity adjustment notice if quantity was changed
            quantity_adjustment_text = ""
            if quantity_was_adjusted:
                quantity_adjustment_text = f" NOTE: Quantity automatically reduced from {original_quantity} to {quantity} to comply with max position size limit ({max_position_pct}% of equity = ${max_position_value:.2f})."
            
            message = f"Opened {direction} position: {quantity} shares of {symbol} @ ${current_price:.2f}{tp_sl_text} (transaction_id={transaction_id}, order_id={order_id}){warning_text}{quantity_adjustment_text}"
            if warnings:
                message += " - You can manually adjust TP/SL using update_take_profit() or update_stop_loss() tools."
            
            return {
                "success": True,
                "message": message,
                "transaction_id": transaction_id,
                "order_id": order_id,
                "symbol": symbol,
                "quantity": quantity,
                "direction": direction,
                "entry_price": current_price,
                "tp_price": tp_price if tp_created else None,
                "sl_price": sl_price if sl_created else None,
                "warnings": warnings if warnings else None
            }
            
        except Exception as e:
            logger.error(f"Error opening new position for {symbol}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error opening position for {symbol}: {str(e)}",
                "transaction_id": None,
                "order_id": None,
                "symbol": symbol,
                "quantity": quantity,
                "direction": direction
            }

    def calculate_position_metrics(
        self,
        entry_price: Annotated[float, "Entry price of the position"],
        current_price: Annotated[float, "Current market price"],
        quantity: Annotated[float, "Position size (number of shares/units)"],
        direction: Annotated[str, "Position direction: 'buy' or 'sell' (case-insensitive)"]
    ) -> Dict[str, float]:
        """
        Calculate position metrics without modifying anything.
        
        Args:
            entry_price: Entry price
            current_price: Current market price
            quantity: Position size
            direction: "BUY" or "SELL" (case-insensitive)
            
        Returns:
            Dict with unrealized_pnl, unrealized_pnl_pct, position_value
        """
        try:
            # Normalize direction to uppercase for case-insensitive handling
            direction = direction.upper()
            
            # Calculate P&L
            if direction == "BUY":
                unrealized_pnl = (current_price - entry_price) * quantity
            else:  # SELL
                unrealized_pnl = (entry_price - current_price) * quantity
            
            # Calculate percentage
            position_cost = entry_price * quantity
            unrealized_pnl_pct = (unrealized_pnl / position_cost * 100) if position_cost > 0 else 0.0
            
            # Calculate current value
            position_value = current_price * quantity
            
            return {
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "position_value": round(position_value, 2)
            }
            
        except Exception as e:
            logger.error(f"Error calculating position metrics: {e}", exc_info=True)
            raise
    
    def get_trade_summary_by_symbol(self) -> Dict[str, Dict[str, float]]:
        """
        Get aggregated buy/sell quantities per symbol across ALL experts on the account.
        
        This provides visibility into overall market exposure and helps identify:
        - Excessive one-directional exposure when hedging is disabled
        - Potential hedging conflicts when hedging is enabled
        - Overall portfolio bias (long vs short)
        
        Includes both FILLED positions and PENDING orders (not yet filled).
        
        Returns:
            Dictionary mapping symbol to buy/sell quantities:
            {
                "AAPL": {"buy_qty": 150.0, "sell_qty": 50.0},
                "TSLA": {"buy_qty": 0.0, "sell_qty": 200.0}
            }
        """
        try:
            logger.debug(f"Getting trade summary by symbol for account {self.account_id}")
            
            with get_db() as session:
                # Query all experts on this account
                experts = session.exec(
                    select(ExpertInstance)
                    .where(ExpertInstance.account_id == self.account_id)
                ).all()
                
                expert_ids = [expert.id for expert in experts]
                logger.debug(f"Found {len(expert_ids)} experts on account {self.account_id}")
                
                # Query all OPENED transactions across all experts
                opened_transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id.in_(expert_ids))
                    .where(Transaction.status == TransactionStatus.OPENED)
                ).all()
                
                # Query all WAITING transactions (pending orders)
                waiting_transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id.in_(expert_ids))
                    .where(Transaction.status == TransactionStatus.WAITING)
                ).all()
                
                logger.debug(f"Found {len(opened_transactions)} opened and {len(waiting_transactions)} waiting transactions")
                
                # Aggregate quantities by symbol and direction
                summary: Dict[str, Dict[str, float]] = {}
                
                # Process opened transactions
                for trans in opened_transactions:
                    symbol = trans.symbol
                    # abs() because get_current_open_qty() returns signed qty (negative for shorts)
                    quantity = abs(trans.get_current_open_qty())

                    if quantity == 0:
                        continue

                    # Determine direction from first order
                    direction = None
                    if trans.trading_orders:
                        first_order = sorted(trans.trading_orders, key=lambda o: o.created_at)[0]
                        direction = first_order.side

                    if not direction:
                        continue

                    if symbol not in summary:
                        summary[symbol] = {"buy_qty": 0.0, "sell_qty": 0.0}

                    if direction == OrderDirection.BUY:
                        summary[symbol]["buy_qty"] += quantity
                    else:  # SELL
                        summary[symbol]["sell_qty"] += quantity
                
                # Process waiting (pending) transactions
                for trans in waiting_transactions:
                    symbol = trans.symbol
                    pending_qty = trans.get_pending_open_qty()
                    
                    if pending_qty == 0:
                        continue
                    
                    # Determine direction from pending entry orders
                    direction = None
                    if trans.trading_orders:
                        for order in trans.trading_orders:
                            if order.status in OrderStatus.get_unfilled_statuses() and order.depends_on_order is None:
                                direction = order.side
                                break
                    
                    if not direction:
                        continue
                    
                    if symbol not in summary:
                        summary[symbol] = {"buy_qty": 0.0, "sell_qty": 0.0}
                    
                    if direction == OrderDirection.BUY:
                        summary[symbol]["buy_qty"] += abs(pending_qty)
                    else:  # SELL
                        summary[symbol]["sell_qty"] += abs(pending_qty)
                
                logger.debug(f"Trade summary: {len(summary)} symbols with exposure")
                return summary
                
        except Exception as e:
            logger.error(f"Error getting trade summary by symbol: {e}", exc_info=True)
            return {}
    
    def get_tools(self) -> List:
        """
        Get all tools as a list for LangChain agent.
        
        Returns:
            List of LangChain tool objects (all 15 tools)
        """
        return [
            # Portfolio & Analysis Tools (8)
            self.get_portfolio_status,
            self.get_recent_analyses,
            self.get_analysis_outputs,
            self.get_analysis_output_detail,
            self.get_analysis_outputs_batch,
            self.get_historical_analyses,
            self.get_analysis_at_open_time,
            self.get_last_risk_manager_summary,
            # Trading Action Tools (7)
            self.close_position,
            self.adjust_quantity,
            self.update_stop_loss,
            self.update_take_profit,
            self.open_buy_position,
            self.open_sell_position,
            self.get_current_price,
            self.calculate_position_metrics
        ]
