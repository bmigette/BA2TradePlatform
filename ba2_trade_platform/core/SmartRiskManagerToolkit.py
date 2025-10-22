"""
Smart Risk Manager Toolkit

Provides LangChain-compatible tools for the Smart Risk Manager agent graph.
All tools are wrappers around existing platform functionality.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select

from ..logger import logger
from .models import (
    Transaction, TradingOrder, MarketAnalysis, ExpertInstance,
    SmartRiskManagerJob, SmartRiskManagerJobAnalysis
)
from .types import TransactionStatus, OrderStatus, OrderType, OrderDirection
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
    
    # ==================== Portfolio & Account Tools ====================
    
    def get_portfolio_status(self) -> Dict[str, Any]:
        """
        Get current portfolio status including all open positions.
        
        Returns comprehensive portfolio data including equity, balance, open positions,
        unrealized P&L, and risk metrics.
        """
        try:
            logger.debug(f"Getting portfolio status for account {self.account_id}")
            
            # Get account info
            account_info = self.account.get_account_info()
            virtual_equity = account_info.get("virtual_equity", 0.0)
            available_balance = account_info.get("available_balance", 0.0)
            
            # Get open transactions
            with get_db() as session:
                transactions = session.exec(
                    select(Transaction)
                    .where(Transaction.account_id == self.account_id)
                    .where(Transaction.status == TransactionStatus.OPEN)
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
                        current_price = trans.entry_price  # Fallback
                    
                    # Calculate P&L
                    if trans.direction == OrderDirection.BUY:
                        unrealized_pnl = (current_price - trans.entry_price) * trans.quantity
                    else:  # SELL
                        unrealized_pnl = (trans.entry_price - current_price) * trans.quantity
                    
                    unrealized_pnl_pct = (unrealized_pnl / (trans.entry_price * trans.quantity)) * 100 if trans.entry_price > 0 else 0.0
                    position_value = current_price * trans.quantity
                    
                    # Get TP/SL orders
                    tp_order = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                        .where(TradingOrder.order_type == OrderType.TAKE_PROFIT)
                        .where(TradingOrder.status.in_([OrderStatus.ACTIVE, OrderStatus.PENDING]))
                    ).first()
                    
                    sl_order = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                        .where(TradingOrder.order_type == OrderType.STOP_LOSS)
                        .where(TradingOrder.status.in_([OrderStatus.ACTIVE, OrderStatus.PENDING]))
                    ).first()
                    
                    position_data = {
                        "transaction_id": trans.id,
                        "symbol": trans.symbol,
                        "direction": trans.direction.value,
                        "quantity": trans.quantity,
                        "entry_price": trans.entry_price,
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
                
                # Calculate risk metrics
                balance_pct_available = (available_balance / virtual_equity * 100) if virtual_equity > 0 else 0.0
                largest_position_pct = (largest_position_value / virtual_equity * 100) if virtual_equity > 0 else 0.0
                
                result = {
                    "account_virtual_equity": round(virtual_equity, 2),
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
                
                logger.debug(f"Portfolio status: {len(open_positions)} positions, equity={virtual_equity}, unrealized_pnl={total_unrealized_pnl}")
                return result
                
        except Exception as e:
            logger.error(f"Error getting portfolio status: {e}", exc_info=True)
            raise

    def get_recent_analyses(
        self,
        symbol: Optional[str] = None,
        max_age_hours: int = 24
    ) -> List[Dict[str, Any]]:
        """
        Get recent market analyses for open positions or a specific symbol.
        
        Args:
            symbol: Specific symbol to query (None = all symbols with open positions)
            max_age_hours: Maximum age of analyses to return (default 24 hours)
            
        Returns:
            List of analysis summaries with metadata, sorted by timestamp DESC
        """
        try:
            logger.debug(f"Getting recent analyses for symbol={symbol}, max_age={max_age_hours}h")
            
            with get_db() as session:
                # Get symbols to query
                if symbol:
                    symbols = [symbol]
                else:
                    # Get all symbols from open positions
                    transactions = session.exec(
                        select(Transaction.symbol)
                        .where(Transaction.account_id == self.account_id)
                        .where(Transaction.status == TransactionStatus.OPEN)
                        .distinct()
                    ).all()
                    symbols = list(transactions)
                
                if not symbols:
                    logger.debug("No symbols to query")
                    return []
                
                # Query market analyses
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
                
                analyses = session.exec(
                    select(MarketAnalysis)
                    .where(MarketAnalysis.symbol.in_(symbols))
                    .where(MarketAnalysis.analysis_timestamp >= cutoff_time)
                    .order_by(MarketAnalysis.analysis_timestamp.desc())
                ).all()
                
                results = []
                for analysis in analyses:
                    age_hours = (datetime.now(timezone.utc) - analysis.analysis_timestamp).total_seconds() / 3600
                    
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
                    
                    results.append({
                        "analysis_id": analysis.id,
                        "symbol": analysis.symbol,
                        "timestamp": analysis.analysis_timestamp.isoformat(),
                        "age_hours": round(age_hours, 1),
                        "expert_name": analysis.expert,
                        "expert_instance_id": analysis.expert_instance_id,
                        "status": analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status),
                        "summary": summary
                    })
                
                logger.debug(f"Found {len(results)} recent analyses")
                return results
                
        except Exception as e:
            logger.error(f"Error getting recent analyses: {e}", exc_info=True)
            raise

    def get_analysis_outputs(self, analysis_id: int) -> Dict[str, str]:
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

    def get_analysis_output_detail(self, analysis_id: int, output_key: str) -> str:
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

    def get_historical_analyses(
        self,
        symbol: str,
        limit: int = 10,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Get historical market analyses for deeper research.
        
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
                analyses = session.exec(
                    select(MarketAnalysis)
                    .where(MarketAnalysis.symbol == symbol)
                    .order_by(MarketAnalysis.analysis_timestamp.desc())
                    .offset(offset)
                    .limit(limit)
                ).all()
                
                results = []
                for analysis in analyses:
                    age_hours = (datetime.now(timezone.utc) - analysis.analysis_timestamp).total_seconds() / 3600
                    
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
                    
                    results.append({
                        "analysis_id": analysis.id,
                        "symbol": analysis.symbol,
                        "timestamp": analysis.analysis_timestamp.isoformat(),
                        "age_hours": round(age_hours, 1),
                        "expert_name": analysis.expert,
                        "expert_instance_id": analysis.expert_instance_id,
                        "status": analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status),
                        "summary": summary
                    })
                
                logger.debug(f"Found {len(results)} historical analyses")
                return results
                
        except Exception as e:
            logger.error(f"Error getting historical analyses: {e}", exc_info=True)
            raise
    
    # ==================== Trading Action Tools ====================

    def close_position(self, transaction_id: int, reason: str) -> Dict[str, Any]:
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

    def get_current_price(self, symbol: str) -> float:
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
        transaction_id: int,
        new_quantity: float,
        reason: str
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
                    
                    # Submit market order to reduce position
                    order_direction = OrderDirection.SELL if transaction.direction == OrderDirection.BUY else OrderDirection.BUY
                    
                    order_result = self.account.submit_order(
                        symbol=transaction.symbol,
                        quantity=quantity_delta,
                        direction=order_direction,
                        order_type=OrderType.MARKET,
                        note=f"Partial close: {reason}"
                    )
                    
                    if order_result.get("success"):
                        # Update transaction quantity
                        transaction.quantity = new_quantity
                        session.add(transaction)
                        session.commit()
                        
                        return {
                            "success": True,
                            "message": f"Successfully reduced position from {old_quantity} to {new_quantity}",
                            "order_id": order_result.get("order_id"),
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Failed to submit order: {order_result.get('message')}",
                            "order_id": None,
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                else:
                    # Add to position
                    logger.info(f"Adding to position: increasing from {old_quantity} to {new_quantity}")
                    
                    order_result = self.account.submit_order(
                        symbol=transaction.symbol,
                        quantity=quantity_delta,
                        direction=transaction.direction,
                        order_type=OrderType.MARKET,
                        note=f"Add to position: {reason}"
                    )
                    
                    if order_result.get("success"):
                        # Update transaction with new average entry price
                        current_price = self.account.get_instrument_current_price(transaction.symbol)
                        new_avg_price = ((transaction.entry_price * old_quantity) + (current_price * quantity_delta)) / new_quantity
                        
                        transaction.quantity = new_quantity
                        transaction.entry_price = new_avg_price
                        session.add(transaction)
                        session.commit()
                        
                        return {
                            "success": True,
                            "message": f"Successfully increased position from {old_quantity} to {new_quantity}",
                            "order_id": order_result.get("order_id"),
                            "old_quantity": old_quantity,
                            "new_quantity": new_quantity
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Failed to submit order: {order_result.get('message')}",
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
        transaction_id: int,
        new_sl_price: float,
        reason: str
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
                
                if transaction.status != TransactionStatus.OPEN:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not open (status: {transaction.status})",
                        "order_id": None,
                        "old_sl_price": None,
                        "new_sl_price": new_sl_price
                    }
                
                # Get existing SL order
                sl_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == transaction_id)
                    .where(TradingOrder.order_type == OrderType.STOP_LOSS)
                    .where(TradingOrder.status.in_([OrderStatus.ACTIVE, OrderStatus.PENDING]))
                ).first()
                
                old_sl_price = sl_order.stop_price if sl_order else None
                
                # Validate new SL price
                current_price = self.account.get_instrument_current_price(transaction.symbol)
                
                if transaction.direction == OrderDirection.BUY:
                    # For long positions, SL must be below current price
                    if new_sl_price >= current_price:
                        return {
                            "success": False,
                            "message": f"Stop loss price {new_sl_price} must be below current price {current_price} for long position",
                            "order_id": None,
                            "old_sl_price": old_sl_price,
                            "new_sl_price": new_sl_price
                        }
                else:  # SELL
                    # For short positions, SL must be above current price
                    if new_sl_price <= current_price:
                        return {
                            "success": False,
                            "message": f"Stop loss price {new_sl_price} must be above current price {current_price} for short position",
                            "order_id": None,
                            "old_sl_price": old_sl_price,
                            "new_sl_price": new_sl_price
                        }
                
                # Cancel existing SL order if exists
                if sl_order:
                    try:
                        self.account.cancel_order(sl_order.id)
                        sl_order.status = OrderStatus.CANCELLED
                        session.add(sl_order)
                        session.commit()
                    except Exception as e:
                        logger.error(f"Failed to cancel existing SL order {sl_order.id}: {e}")
                
                # Create new SL order
                sl_direction = OrderDirection.SELL if transaction.direction == OrderDirection.BUY else OrderDirection.BUY
                
                order_result = self.account.submit_order(
                    symbol=transaction.symbol,
                    quantity=transaction.quantity,
                    direction=sl_direction,
                    order_type=OrderType.STOP_LOSS,
                    stop_price=new_sl_price,
                    note=f"Updated SL: {reason}"
                )
                
                if order_result.get("success"):
                    logger.info(f"Successfully updated stop loss from {old_sl_price} to {new_sl_price}")
                    return {
                        "success": True,
                        "message": f"Successfully updated stop loss to {new_sl_price}",
                        "order_id": order_result.get("order_id"),
                        "old_sl_price": old_sl_price,
                        "new_sl_price": new_sl_price
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to create new SL order: {order_result.get('message')}",
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
        transaction_id: int,
        new_tp_price: float,
        reason: str
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
                
                if transaction.status != TransactionStatus.OPEN:
                    return {
                        "success": False,
                        "message": f"Transaction {transaction_id} is not open (status: {transaction.status})",
                        "order_id": None,
                        "old_tp_price": None,
                        "new_tp_price": new_tp_price
                    }
                
                # Get existing TP order
                tp_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == transaction_id)
                    .where(TradingOrder.order_type == OrderType.TAKE_PROFIT)
                    .where(TradingOrder.status.in_([OrderStatus.ACTIVE, OrderStatus.PENDING]))
                ).first()
                
                old_tp_price = tp_order.limit_price if tp_order else None
                
                # Validate new TP price
                current_price = self.account.get_instrument_current_price(transaction.symbol)
                
                if transaction.direction == OrderDirection.BUY:
                    # For long positions, TP must be above current price
                    if new_tp_price <= current_price:
                        return {
                            "success": False,
                            "message": f"Take profit price {new_tp_price} must be above current price {current_price} for long position",
                            "order_id": None,
                            "old_tp_price": old_tp_price,
                            "new_tp_price": new_tp_price
                        }
                else:  # SELL
                    # For short positions, TP must be below current price
                    if new_tp_price >= current_price:
                        return {
                            "success": False,
                            "message": f"Take profit price {new_tp_price} must be below current price {current_price} for short position",
                            "order_id": None,
                            "old_tp_price": old_tp_price,
                            "new_tp_price": new_tp_price
                        }
                
                # Cancel existing TP order if exists
                if tp_order:
                    try:
                        self.account.cancel_order(tp_order.id)
                        tp_order.status = OrderStatus.CANCELLED
                        session.add(tp_order)
                        session.commit()
                    except Exception as e:
                        logger.error(f"Failed to cancel existing TP order {tp_order.id}: {e}")
                
                # Create new TP order
                tp_direction = OrderDirection.SELL if transaction.direction == OrderDirection.BUY else OrderDirection.BUY
                
                order_result = self.account.submit_order(
                    symbol=transaction.symbol,
                    quantity=transaction.quantity,
                    direction=tp_direction,
                    order_type=OrderType.TAKE_PROFIT,
                    limit_price=new_tp_price,
                    note=f"Updated TP: {reason}"
                )
                
                if order_result.get("success"):
                    logger.info(f"Successfully updated take profit from {old_tp_price} to {new_tp_price}")
                    return {
                        "success": True,
                        "message": f"Successfully updated take profit to {new_tp_price}",
                        "order_id": order_result.get("order_id"),
                        "old_tp_price": old_tp_price,
                        "new_tp_price": new_tp_price
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to create new TP order: {order_result.get('message')}",
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
        symbol: str,
        direction: str,
        quantity: float,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        reason: str = ""
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
            available_balance = account_info.get("available_balance", 0.0)
            
            current_price = self.account.get_instrument_current_price(symbol)
            position_value = current_price * quantity
            
            if position_value > available_balance:
                return {
                    "success": False,
                    "message": f"Insufficient balance: position value {position_value} > available {available_balance}",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Check position size limits
            virtual_equity = account_info.get("virtual_equity", 0.0)
            max_position_pct = settings.get("max_virtual_equity_per_instrument_percent", 100.0)
            max_position_value = virtual_equity * (max_position_pct / 100.0)
            
            if position_value > max_position_value:
                return {
                    "success": False,
                    "message": f"Position size {position_value} exceeds max allowed {max_position_value} ({max_position_pct}% of equity)",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Submit market order
            order_result = self.account.submit_order(
                symbol=symbol,
                quantity=quantity,
                direction=order_direction,
                order_type=OrderType.MARKET,
                note=f"New position: {reason}"
            )
            
            if not order_result.get("success"):
                return {
                    "success": False,
                    "message": f"Failed to submit order: {order_result.get('message')}",
                    "transaction_id": None,
                    "order_id": None
                }
            
            # Create transaction record (simplified - actual implementation may differ)
            # Note: In real implementation, transaction creation is handled by account interface
            transaction_id = order_result.get("transaction_id")
            order_id = order_result.get("order_id")
            
            logger.info(f"Successfully opened position: transaction_id={transaction_id}, order_id={order_id}")
            
            # Submit TP/SL orders if provided
            if tp_price and transaction_id:
                tp_direction = OrderDirection.SELL if direction == "BUY" else OrderDirection.BUY
                tp_result = self.account.submit_order(
                    symbol=symbol,
                    quantity=quantity,
                    direction=tp_direction,
                    order_type=OrderType.TAKE_PROFIT,
                    limit_price=tp_price,
                    note=f"TP for transaction {transaction_id}"
                )
                if tp_result.get("success"):
                    logger.info(f"Created take profit order at {tp_price}")
            
            if sl_price and transaction_id:
                sl_direction = OrderDirection.SELL if direction == "BUY" else OrderDirection.BUY
                sl_result = self.account.submit_order(
                    symbol=symbol,
                    quantity=quantity,
                    direction=sl_direction,
                    order_type=OrderType.STOP_LOSS,
                    stop_price=sl_price,
                    note=f"SL for transaction {transaction_id}"
                )
                if sl_result.get("success"):
                    logger.info(f"Created stop loss order at {sl_price}")
            
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
        entry_price: float,
        current_price: float,
        quantity: float,
        direction: str
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
            List of LangChain tool objects (all 12 tools)
        """
        return [
            # Portfolio & Analysis Tools (5)
            self.get_portfolio_status,
            self.get_recent_analyses,
            self.get_analysis_outputs,
            self.get_analysis_output_detail,
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
