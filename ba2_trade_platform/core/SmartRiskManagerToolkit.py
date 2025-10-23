"""
Smart Risk Manager Toolkit

Provides LangChain-compatible tools for the Smart Risk Manager agent graph.
All tools are wrappers around existing platform functionality.
"""

from typing import Dict, Any, List, Optional, Annotated
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select

from ..logger import logger
from .models import (
    Transaction, TradingOrder, MarketAnalysis, ExpertInstance,
    SmartRiskManagerJob, SmartRiskManagerJobAnalysis
)
from .types import TransactionStatus, OrderStatus, OrderType, OrderDirection, MarketAnalysisStatus
from .db import get_db, get_instance
from .utils import get_expert_instance_from_id, get_account_instance_from_id


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
        comment: Optional[str] = None
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
            status=OrderStatus.PENDING  # Will be updated by broker
        )
        
        logger.debug(f"Created TradingOrder: {symbol} {side.value} {quantity} @ {order_type.value}")
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
                transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.expert_id == self.expert_instance_id)
                    .where(Transaction.status == TransactionStatus.OPENED)
                ).all()
                
                open_positions = []
                total_unrealized_pnl = 0.0
                total_position_value = 0.0
                largest_position_value = 0.0
                
                for trans in transactions:
                    # Get current price
                    try:
                        current_price = self.account.get_instrument_current_price(trans.symbol)
                    except Exception as e:
                        logger.error(f"Failed to get current price for {trans.symbol}: {e}")
                        current_price = trans.open_price  # Fallback
                    
                    # Get actual quantity from filled orders
                    quantity = trans.get_current_open_qty()
                    
                    # Infer direction from first order
                    direction = None
                    if trans.trading_orders:
                        first_order = sorted(trans.trading_orders, key=lambda o: o.created_at)[0]
                        direction = first_order.side
                    
                    if not direction or not trans.open_price or quantity == 0:
                        logger.warning(f"Skipping transaction {trans.id} - missing direction or price or zero quantity")
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
                
                # Calculate risk metrics using virtual_balance from expert
                balance_pct_available = (available_balance / virtual_balance * 100) if virtual_balance > 0 else 0.0
                largest_position_pct = (largest_position_value / virtual_balance * 100) if virtual_balance > 0 else 0.0
                
                result = {
                    "account_virtual_equity": round(virtual_balance, 2),
                    "account_available_balance": round(available_balance, 2),
                    "account_balance_pct_available": round(balance_pct_available, 2),
                    "open_positions": open_positions,
                    "total_unrealized_pnl": round(total_unrealized_pnl, 2),
                    "total_position_value": round(total_position_value, 2),
                    "risk_metrics": {
                        "largest_position_pct": round(largest_position_pct, 2),
                        "num_positions": len(open_positions)
                    }
                }
                
                logger.debug(f"Portfolio status: {len(open_positions)} positions, virtual_balance=${virtual_balance:,.2f}, available_balance=${available_balance:,.2f}, unrealized_pnl={total_unrealized_pnl}")
                return result
                
        except Exception as e:
            logger.error(f"Error getting portfolio status: {e}", exc_info=True)
            raise

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
                    
                    results.append({
                        "analysis_id": analysis.id,
                        "symbol": analysis.symbol,
                        "timestamp": analysis.created_at.isoformat(),
                        "age_hours": round(age_hours, 1),
                        "expert_name": expert_name,
                        "expert_instance_id": analysis.expert_instance_id,
                        "status": analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status),
                        "summary": summary
                    })
                
                # Sort results by timestamp DESC
                results.sort(key=lambda x: x["timestamp"], reverse=True)
                
                logger.debug(f"Found {len(results)} completed recent analyses")
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
                
                # Call get_output_detail
                if hasattr(expert_inst, 'get_output_detail'):
                    detail = expert_inst.get_output_detail(analysis_id, output_key)
                    logger.debug(f"Retrieved output detail (length: {len(detail)} chars)")
                    return detail
                else:
                    raise ValueError("Expert does not implement get_output_detail()")
                    
        except Exception as e:
            logger.error(f"Error getting analysis output detail: {e}", exc_info=True)
            raise

    def get_analysis_outputs_batch(
        self,
        requests: Annotated[
            List[Dict[str, Any]], 
            "List of dicts with 'analysis_id' and 'output_keys' (list of keys to fetch)"
        ],
        max_tokens: Annotated[int, "Maximum tokens in response (approximate, using 4 chars/token)"] = 100000
    ) -> Dict[str, Any]:
        """
        Fetch multiple analysis outputs in a single call with automatic truncation.
        
        This method allows efficient batch fetching of analysis outputs while preventing
        token limit overflow. If the combined output exceeds max_tokens, it will truncate
        and report which items were skipped.
        
        Args:
            requests: List of dicts, each with:
                - analysis_id (int): MarketAnalysis ID
                - output_keys (List[str]): List of output keys to fetch
            max_tokens: Maximum tokens in response (default 100K, ~400K chars)
            
        Returns:
            Dict with:
                - outputs: List of dicts with analysis_id, output_key, content
                - truncated: bool - whether truncation occurred
                - skipped_items: List of (analysis_id, output_key) tuples that were skipped
                - total_chars: Total characters in response
                - total_tokens_estimate: Estimated tokens (chars / 4)
                
        Example:
            requests = [
                {"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]},
                {"analysis_id": 124, "output_keys": ["news_report"]}
            ]
            result = toolkit.get_analysis_outputs_batch(requests)
        """
        try:
            max_chars = max_tokens * 4  # Approximate: 1 token ≈ 4 chars
            
            logger.debug(f"Fetching batch outputs: {len(requests)} requests, max_chars={max_chars:,}")
            
            outputs = []
            skipped_items = []
            total_chars = 0
            truncated = False
            
            # Process each request by calling get_analysis_output_detail
            for req in requests:
                analysis_id = req.get("analysis_id")
                output_keys = req.get("output_keys", [])
                
                if not analysis_id:
                    logger.warning(f"Skipping request with missing analysis_id: {req}")
                    continue
                
                if not output_keys:
                    logger.warning(f"Skipping request with empty output_keys for analysis {analysis_id}")
                    continue
                
                # Fetch each output key using get_analysis_output_detail
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
                        
                        # Check if the result indicates an error
                        if result.startswith("Error:") or result.startswith("Analysis") and "not found" in result:
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
                    
                    results.append({
                        "analysis_id": analysis.id,
                        "symbol": analysis.symbol,
                        "timestamp": analysis.created_at.isoformat(),
                        "age_hours": round(age_hours, 1),
                        "expert_name": expert_name,
                        "expert_instance_id": analysis.expert_instance_id,
                        "status": analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status),
                        "summary": summary
                    })
                
                logger.debug(f"Found {len(results)} completed historical analyses")
                return results
                
        except Exception as e:
            logger.error(f"Error getting historical analyses: {e}", exc_info=True)
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
            
            with get_db() as session:
                transaction = session.get(Transaction, transaction_id)
                if not transaction:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} not found",
                        "order_id": None,
                        "transaction_id": transaction_id
                    }
                
                if transaction.status != TransactionStatus.OPEN:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not open (status: {transaction.status})",
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

    def get_current_price(
        self, 
        symbol: Annotated[str, "Instrument symbol to get current price for"]
    ) -> float:
        """
        Get current market price for a symbol.
        
        Args:
            symbol: Instrument symbol
            
        Returns:
            Current price as float
        """
        try:
            logger.debug(f"Getting current price for {symbol}")
            price = self.account.get_instrument_current_price(symbol)
            logger.debug(f"Current price for {symbol}: {price}")
            return price
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
        
        Args:
            transaction_id: ID of Transaction to adjust
            new_quantity: New total quantity (can be < or > current)
            reason: Explanation for adjustment
            
        Returns:
            Result dict with success, message, order_id, old_quantity, new_quantity
        """
        try:
            logger.info(f"Adjusting quantity for transaction {transaction_id} to {new_quantity}. Reason: {reason}")
            
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
                
                if transaction.status != TransactionStatus.OPEN:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not open (status: {transaction.status})",
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
                quantity_delta = abs(new_quantity - old_quantity)
                
                if new_quantity < old_quantity:
                    # Partial close
                    logger.info(f"Partial close: reducing from {old_quantity} to {new_quantity}")
                    
                    # Get transaction direction to determine close direction
                    if not transaction.trading_orders:
                        return {
                            "success": False,
                            "message": "Transaction has no orders - cannot determine direction",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    
                    # Direction from first trading order
                    entry_direction = transaction.trading_orders[0].side
                    # Close order is opposite direction
                    close_direction = OrderDirection.SELL if entry_direction == OrderDirection.BUY else OrderDirection.BUY
                    
                    # Create close order
                    close_order = self._create_trading_order(
                        symbol=transaction.symbol,
                        quantity=quantity_delta,
                        side=close_direction,
                        order_type=OrderType.MARKET,
                        transaction_id=transaction_id,
                        comment=f"Partial close: {reason}"
                    )
                    
                    # Submit order
                    submitted_order = self.account.submit_order(close_order)
                    
                    if submitted_order and submitted_order.id:
                        # Note: Don't manually update transaction.quantity - account interface handles this
                        logger.info(f"Successfully reduced position from {old_quantity} to {new_quantity}")
                        
                        return {
                            "success": True,
                            "message": f"Successfully reduced position from {old_quantity} to {new_quantity}",
                            "order_id": submitted_order.id,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Failed to submit partial close order",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                else:
                    # Add to position
                    logger.info(f"Adding to position: increasing from {old_quantity} to {new_quantity}")
                    
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
                    
                    # Check if adding to position is allowed based on expert settings
                    settings = self.expert.settings
                    if entry_direction == OrderDirection.BUY and not settings.get("enable_buy", True):
                        return {
                            "success": False,
                            "message": "Cannot add to long position: BUY orders are disabled in expert settings",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    if entry_direction == OrderDirection.SELL and not settings.get("enable_sell", True):
                        return {
                            "success": False,
                            "message": "Cannot add to short position: SELL orders are disabled in expert settings",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    
                    # Create add-to-position order (same direction as entry)
                    add_order = self._create_trading_order(
                        symbol=transaction.symbol,
                        quantity=quantity_delta,
                        side=entry_direction,
                        order_type=OrderType.MARKET,
                        transaction_id=transaction_id,
                        comment=f"Add to position: {reason}"
                    )
                    
                    # Submit order
                    submitted_order = self.account.submit_order(add_order)
                    
                    if submitted_order and submitted_order.id:
                        # Note: Account interface should handle updating transaction with new average price
                        logger.info(f"Successfully increased position from {old_quantity} to {new_quantity}")
                        
                        return {
                            "success": True,
                            "message": f"Successfully increased position from {old_quantity} to {new_quantity}",
                            "order_id": submitted_order.id,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Failed to submit add-to-position order",
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
        
        Args:
            transaction_id: ID of Transaction
            new_sl_price: New stop loss price
            reason: Explanation for change
            
        Returns:
            Result dict with success, message, order_id, old_sl_price, new_sl_price
        """
        try:
            logger.info(f"Updating stop loss for transaction {transaction_id} to {new_sl_price}. Reason: {reason}")
            
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
                
                if transaction.status != TransactionStatus.OPENED:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not open (status: {transaction.status})",
                        "order_id": None,
                        "old_sl_price": None,
                        "new_sl_price": new_sl_price
                    }
                
                # Get transaction direction
                if not transaction.trading_orders:
                    return {
                        "success": False,
                        "message": "Transaction has no orders - cannot determine direction",
                        "order_id": None,
                        "old_sl_price": None,
                        "new_sl_price": new_sl_price
                    }
                
                entry_direction = transaction.trading_orders[0].side
                entry_order_id = transaction.trading_orders[0].id
                
                # Get existing SL order (SELL_STOP for long, BUY_STOP for short)
                sl_order_type = OrderType.SELL_STOP if entry_direction == OrderDirection.BUY else OrderType.BUY_STOP
                sl_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == transaction_id)
                    .where(TradingOrder.order_type == sl_order_type)
                    .where(TradingOrder.status.not_in(OrderStatus.get_terminal_statuses()))
                ).first()
                
                old_sl_price = sl_order.stop_price if sl_order else None
                
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
                
                if entry_direction == OrderDirection.BUY:
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
                
                # Cancel existing SL order if exists
                if sl_order:
                    try:
                        self.account.cancel_order(sl_order.id)
                        logger.info(f"Cancelled existing SL order {sl_order.id}")
                    except Exception as e:
                        logger.error(f"Failed to cancel existing SL order {sl_order.id}: {e}")
                        # Continue anyway - try to create new order
                
                # Create new SL order
                sl_direction = OrderDirection.SELL if entry_direction == OrderDirection.BUY else OrderDirection.BUY
                
                # Get current position quantity
                current_qty = transaction.get_current_open_qty()
                if current_qty <= 0:
                    return {
                        "success": False,
                        "message": f"Transaction has no open quantity",
                        "order_id": None,
                        "old_sl_price": old_sl_price,
                        "new_sl_price": new_sl_price
                    }
                
                sl_order_obj = self._create_trading_order(
                    symbol=transaction.symbol,
                    quantity=current_qty,
                    side=sl_direction,
                    order_type=sl_order_type,
                    transaction_id=transaction_id,
                    stop_price=new_sl_price,
                    depends_on_order=entry_order_id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    comment=f"Updated SL: {reason}"
                )
                
                submitted_order = self.account.submit_order(sl_order_obj)
                
                if submitted_order and submitted_order.id:
                    logger.info(f"Successfully updated stop loss from {old_sl_price} to {new_sl_price}")
                    return {
                        "success": True,
                        "message": f"Successfully updated stop loss to {new_sl_price:.2f}",
                        "order_id": submitted_order.id,
                        "old_sl_price": old_sl_price,
                        "new_sl_price": new_sl_price
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to create new SL order",
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
        
        Args:
            transaction_id: ID of Transaction
            new_tp_price: New take profit price
            reason: Explanation for change
            
        Returns:
            Result dict with success, message, order_id, old_tp_price, new_tp_price
        """
        try:
            logger.info(f"Updating take profit for transaction {transaction_id} to {new_tp_price}. Reason: {reason}")
            
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
                
                if transaction.status != TransactionStatus.OPENED:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not open (status: {transaction.status})",
                        "order_id": None,
                        "old_tp_price": None,
                        "new_tp_price": new_tp_price
                    }
                
                # Get transaction direction
                if not transaction.trading_orders:
                    return {
                        "success": False,
                        "message": "Transaction has no orders - cannot determine direction",
                        "order_id": None,
                        "old_tp_price": None,
                        "new_tp_price": new_tp_price
                    }
                
                entry_direction = transaction.trading_orders[0].side
                entry_order_id = transaction.trading_orders[0].id
                
                # Get existing TP order (SELL_LIMIT for long, BUY_LIMIT for short)
                tp_order_type = OrderType.SELL_LIMIT if entry_direction == OrderDirection.BUY else OrderType.BUY_LIMIT
                tp_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == transaction_id)
                    .where(TradingOrder.order_type == tp_order_type)
                    .where(TradingOrder.status.not_in(OrderStatus.get_terminal_statuses()))
                ).first()
                
                old_tp_price = tp_order.limit_price if tp_order else None
                
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
                
                if entry_direction == OrderDirection.BUY:
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
                
                # Cancel existing TP order if exists
                if tp_order:
                    try:
                        self.account.cancel_order(tp_order.id)
                        logger.info(f"Cancelled existing TP order {tp_order.id}")
                    except Exception as e:
                        logger.error(f"Failed to cancel existing TP order {tp_order.id}: {e}")
                        # Continue anyway - try to create new order
                
                # Create new TP order
                tp_direction = OrderDirection.SELL if entry_direction == OrderDirection.BUY else OrderDirection.BUY
                
                # Get current position quantity
                current_qty = transaction.get_current_open_qty()
                if current_qty <= 0:
                    return {
                        "success": False,
                        "message": f"Transaction has no open quantity",
                        "order_id": None,
                        "old_tp_price": old_tp_price,
                        "new_tp_price": new_tp_price
                    }
                
                tp_order_obj = self._create_trading_order(
                    symbol=transaction.symbol,
                    quantity=current_qty,
                    side=tp_direction,
                    order_type=tp_order_type,
                    transaction_id=transaction_id,
                    limit_price=new_tp_price,
                    depends_on_order=entry_order_id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    comment=f"Updated TP: {reason}"
                )
                
                submitted_order = self.account.submit_order(tp_order_obj)
                
                if submitted_order and submitted_order.id:
                    logger.info(f"Successfully updated take profit from {old_tp_price} to {new_tp_price}")
                    return {
                        "success": True,
                        "message": f"Successfully updated take profit to {new_tp_price:.2f}",
                        "order_id": submitted_order.id,
                        "old_tp_price": old_tp_price,
                        "new_tp_price": new_tp_price
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to create new TP order",
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

    def open_new_position(
        self,
        symbol: Annotated[str, "Instrument symbol to trade"],
        direction: Annotated[str, "Trade direction: 'buy' or 'sell'"],
        quantity: Annotated[float, "Number of shares/units to trade"],
        tp_price: Annotated[Optional[float], "Optional take profit price"] = None,
        sl_price: Annotated[Optional[float], "Optional stop loss price"] = None,
        reason: Annotated[str, "Reason for opening this position"] = ""
    ) -> Dict[str, Any]:
        """
        Open a new trading position.
        
        Args:
            symbol: Instrument symbol
            direction: "BUY" or "SELL"
            quantity: Position size
            tp_price: Take profit price (optional)
            sl_price: Stop loss price (optional)
            reason: Explanation for opening position
            
        Returns:
            Result dict with success, message, transaction_id, order_id
        """
        try:
            logger.info(f"Opening new {direction} position for {symbol}, quantity={quantity}. Reason: {reason}")
            
            # Validate direction
            try:
                order_direction = OrderDirection(direction)
            except ValueError:
                return {
                    "success": False,
                    "message": f"Invalid direction: {direction}. Must be 'BUY' or 'SELL'",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Check if symbol is enabled in expert settings
            enabled_instruments = self.expert.get_enabled_instruments()
            if symbol not in enabled_instruments:
                return {
                    "success": False,
                    "message": f"Symbol {symbol} is not enabled in expert settings",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Check enable_buy/enable_sell settings
            settings = self.expert.settings
            if direction == "BUY" and not settings.get("enable_buy", True):
                return {
                    "success": False,
                    "message": "Buy orders are disabled in expert settings",
                    "transaction_id": None,
                    "order_id": None
                }
            if direction == "SELL" and not settings.get("enable_sell", True):
                return {
                    "success": False,
                    "message": "Sell orders are disabled in expert settings",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Check account balance
            account_info = self.account.get_account_info()
            available_balance = float(account_info.cash) if account_info else 0.0
            virtual_equity = float(account_info.equity) if account_info else 0.0
            
            current_price = self.account.get_instrument_current_price(symbol)
            if current_price is None:
                return {
                    "success": False,
                    "message": f"Could not get current price for {symbol}",
                    "transaction_id": None,
                    "order_id": None
                }
            
            position_value = current_price * quantity
            
            if position_value > available_balance:
                return {
                    "success": False,
                    "message": f"Insufficient balance: position value {position_value:.2f} > available {available_balance:.2f}",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Check position size limits
            settings = self.expert.settings
            max_position_pct = settings.get("max_virtual_equity_per_instrument_percent", 100.0)
            max_position_value = virtual_equity * (max_position_pct / 100.0)
            
            if position_value > max_position_value:
                return {
                    "success": False,
                    "message": f"Position size {position_value:.2f} exceeds max allowed {max_position_value:.2f} ({max_position_pct}% of equity)",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Create and submit market order (transaction will be auto-created)
            entry_order = self._create_trading_order(
                symbol=symbol,
                quantity=quantity,
                side=order_direction,
                order_type=OrderType.MARKET,
                comment=f"New position: {reason}"
            )
            
            # Submit order - this will auto-create transaction for market orders
            submitted_order = self.account.submit_order(entry_order)
            
            if not submitted_order or not submitted_order.id:
                return {
                    "success": False,
                    "message": f"Failed to submit entry order",
                    "transaction_id": None,
                    "order_id": None
                }
            
            transaction_id = submitted_order.transaction_id
            order_id = submitted_order.id
            
            logger.info(f"Successfully opened position: transaction_id={transaction_id}, order_id={order_id}")
            
            # Submit TP/SL orders if provided and we have a transaction
            if transaction_id:
                # Take profit order (opposite direction limit order)
                if tp_price:
                    tp_side = OrderDirection.SELL if direction == "BUY" else OrderDirection.BUY
                    tp_type = OrderType.SELL_LIMIT if direction == "BUY" else OrderType.BUY_LIMIT
                    
                    try:
                        tp_order = self._create_trading_order(
                            symbol=symbol,
                            quantity=quantity,
                            side=tp_side,
                            order_type=tp_type,
                            transaction_id=transaction_id,
                            limit_price=tp_price,
                            depends_on_order=order_id,
                            depends_order_status_trigger=OrderStatus.FILLED,
                            comment=f"TP for transaction {transaction_id}"
                        )
                        submitted_tp = self.account.submit_order(tp_order)
                        if submitted_tp:
                            logger.info(f"Created take profit order at {tp_price}")
                    except Exception as e:
                        logger.error(f"Failed to create TP order: {e}")
                
                # Stop loss order (opposite direction stop order)
                if sl_price:
                    sl_side = OrderDirection.SELL if direction == "BUY" else OrderDirection.BUY
                    sl_type = OrderType.SELL_STOP if direction == "BUY" else OrderType.BUY_STOP
                    
                    try:
                        sl_order = self._create_trading_order(
                            symbol=symbol,
                            quantity=quantity,
                            side=sl_side,
                            order_type=sl_type,
                            transaction_id=transaction_id,
                            stop_price=sl_price,
                            depends_on_order=order_id,
                            depends_order_status_trigger=OrderStatus.FILLED,
                            comment=f"SL for transaction {transaction_id}"
                        )
                        submitted_sl = self.account.submit_order(sl_order)
                        if submitted_sl:
                            logger.info(f"Created stop loss order at {sl_price}")
                    except Exception as e:
                        logger.error(f"Failed to create SL order: {e}")
            
            return {
                "success": True,
                "message": f"Successfully opened {direction} position for {symbol}",
                "transaction_id": transaction_id,
                "order_id": order_id
            }
            
        except Exception as e:
            logger.error(f"Error opening new position for {symbol}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "transaction_id": None,
                "order_id": None
            }

    def calculate_position_metrics(
        self,
        entry_price: Annotated[float, "Entry price of the position"],
        current_price: Annotated[float, "Current market price"],
        quantity: Annotated[float, "Position size (number of shares/units)"],
        direction: Annotated[str, "Position direction: 'buy' or 'sell'"]
    ) -> Dict[str, float]:
        """
        Calculate position metrics without modifying anything.
        
        Args:
            entry_price: Entry price
            current_price: Current market price
            quantity: Position size
            direction: "BUY" or "SELL"
            
        Returns:
            Dict with unrealized_pnl, unrealized_pnl_pct, position_value
        """
        try:
            # Calculate P&L
            if direction.upper() == "BUY":
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
    
    def get_tools(self) -> List:
        """
        Get all tools as a list for LangChain agent.
        
        Returns:
            List of LangChain tool objects (all 13 tools)
        """
        return [
            # Portfolio & Analysis Tools (6)
            self.get_portfolio_status,
            self.get_recent_analyses,
            self.get_analysis_outputs,
            self.get_analysis_output_detail,
            self.get_analysis_outputs_batch,
            self.get_historical_analyses,
            # Trading Action Tools (7)
            self.close_position,
            self.adjust_quantity,
            self.update_stop_loss,
            self.update_take_profit,
            self.open_new_position,
            self.get_current_price,
            self.calculate_position_metrics
        ]
