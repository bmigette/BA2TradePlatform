"""
Financial Modeling Prep (FMP) Insider Trading Provider

This provider uses the FMP API to retrieve insider trading data.

API Documentation: https://site.financialmodelingprep.com/developer/docs#insider-trading
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timezone

import fmpsdk

from ba2_trade_platform.core.interfaces import CompanyInsiderInterface
from ba2_trade_platform.core.provider_utils import calculate_date_range, log_provider_call
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class FMPInsiderProvider(CompanyInsiderInterface):
    """
    Financial Modeling Prep Insider Trading Provider.
    
    Provides access to insider trading data from FMP API, including:
    - Insider transactions (buys, sells, exercises, etc.)
    - Aggregated insider sentiment metrics
    - SEC Form 4 filing data
    
    Requires FMP API key (free tier available).
    """
    
    def __init__(self):
        """Initialize the FMP Insider Provider with API key."""
        super().__init__()
        
        # Get API key from settings
        self.api_key = get_app_setting("FMP_API_KEY")
        
        if not self.api_key:
            raise ValueError(
                "FMP API key not configured. "
                "Please set 'FMP_API_KEY' in AppSetting table."
            )
        
        logger.info("FMPInsiderProvider initialized successfully")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "fmp"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["insider_transactions", "insider_sentiment"]
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        return bool(self.api_key)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format insider data as dictionary."""
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """Format insider data as markdown."""
        if isinstance(data, str):
            return data
        if isinstance(data, dict) and "markdown" in data:
            return data["markdown"]
        return str(data)
    
    @log_provider_call
    def get_insider_transactions(
        self,
        symbol: str,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get insider trading transactions for a company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            Insider transaction data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate start_date if using lookback_days
        if lookback_days:
            actual_start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            actual_start_date = start_date
        
        logger.debug(
            f"Fetching FMP insider transactions for {symbol} from "
            f"{actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )
        
        try:
            # FMP insider_trading returns list of insider transactions
            insider_data = fmpsdk.insider_trading(
                apikey=self.api_key,
                symbol=symbol
            )
            
            if not insider_data:
                logger.warning(f"No insider data returned from FMP for {symbol}")
                return self._format_empty_transactions(symbol, actual_start_date, end_date, format_type)
            
            # Filter transactions by date range
            filtered_transactions = []
            total_purchase_value = 0.0
            total_sale_value = 0.0
            
            for transaction in insider_data:
                # Parse transaction date
                trans_date_str = transaction.get("transactionDate", "")
                if not trans_date_str:
                    continue
                    
                try:
                    trans_date = datetime.fromisoformat(trans_date_str.split("T")[0])
                    # Ensure all dates are timezone-aware for comparison
                    if trans_date.tzinfo is None:
                        trans_date = trans_date.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    continue
                
                # Check if in date range
                if actual_start_date <= trans_date <= end_date:
                    filtered_transactions.append(transaction)
                    
                    # Calculate transaction values
                    trans_type = transaction.get("transactionType", "").upper()
                    securities_transacted = float(transaction.get("securitiesTransacted", 0) or 0)
                    price = float(transaction.get("price", 0) or 0)
                    value = securities_transacted * price
                    
                    # Accumulate purchase/sale values
                    # P-Purchase, S-Sale, M-Option Exercise, A-Award, D-Disposition
                    if trans_type in ["P-PURCHASE", "M-EXEMPT"]:
                        total_purchase_value += abs(value)
                    elif trans_type in ["S-SALE", "D-RETURN"]:
                        total_sale_value += abs(value)
            
            net_value = total_purchase_value - total_sale_value
            
            # Build dict response
            dict_response = {
                "symbol": symbol,
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "transaction_count": len(filtered_transactions),
                "total_purchase_value": total_purchase_value,
                "total_sale_value": total_sale_value,
                "net_transaction_value": net_value,
                "transactions": [
                    {
                        "filing_date": t.get("filingDate", ""),
                        "transaction_date": t.get("transactionDate", ""),
                        "insider_name": t.get("reportingName", ""),
                        "title": t.get("typeOfOwner", ""),
                        "transaction_type": t.get("transactionType", ""),
                        "shares": float(t.get("securitiesTransacted", 0) or 0),
                        "price": float(t.get("price", 0) or 0),
                        "value": float(t.get("securitiesTransacted", 0) or 0) * float(t.get("price", 0) or 0),
                        "shares_owned_following": float(t.get("securitiesOwned", 0) or 0),
                        "form_type": t.get("formType", ""),
                        "sec_link": t.get("link", "")
                    }
                    for t in filtered_transactions
                ]
            }
            
            # Build markdown response
            markdown = f"# Insider Transactions for {symbol}\n\n"
            markdown += f"**Period:** {actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n"
            markdown += f"**Transactions:** {len(filtered_transactions)}\n\n"
            markdown += f"**Summary:**\n"
            markdown += f"- Total Purchases: ${total_purchase_value:,.2f}\n"
            markdown += f"- Total Sales: ${total_sale_value:,.2f}\n"
            markdown += f"- Net Activity: ${net_value:,.2f}\n\n"
            
            if filtered_transactions:
                markdown += "## Recent Transactions\n\n"
                markdown += "| Date | Insider | Type | Shares | Price | Value |\n"
                markdown += "|------|---------|------|--------|-------|-------|\n"
                
                for t in filtered_transactions[:20]:  # Limit to 20 most recent
                    trans_date = t.get("transactionDate", "")[:10]
                    insider = t.get("reportingName", "Unknown")[:30]
                    trans_type = t.get("transactionType", "")
                    shares = float(t.get("securitiesTransacted", 0) or 0)
                    price = float(t.get("price", 0) or 0)
                    value = shares * price
                    
                    markdown += f"| {trans_date} | {insider} | {trans_type} | {shares:,.0f} | ${price:.2f} | ${value:,.2f} |\n"
                
                if len(filtered_transactions) > 20:
                    markdown += f"\n*Showing 20 of {len(filtered_transactions)} transactions*\n"
            
            # Return based on format_type
            if format_type == "dict":
                return dict_response
            elif format_type == "both":
                return {
                    "text": markdown,
                    "data": dict_response
                }
            else:  # markdown
                return markdown
                
        except Exception as e:
            logger.error(f"Error fetching FMP insider transactions for {symbol}: {e}")
            return f"Error fetching insider transactions: {str(e)}"
    
    @log_provider_call
    def get_insider_sentiment(
        self,
        symbol: str,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get aggregated insider sentiment metrics for a company.
        
        This calculates sentiment based on insider transaction patterns.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            Insider sentiment data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        # Default to 90 days if neither start_date nor lookback_days provided
        if not start_date and not lookback_days:
            lookback_days = 90
            logger.warning(f"No date range provided for insider sentiment, defaulting to lookback_days=90")
        
        # Get transaction data first
        transactions_dict = self.get_insider_transactions(
            symbol=symbol,
            end_date=end_date,
            start_date=start_date,
            lookback_days=lookback_days,
            format_type="dict"
        )
        
        if isinstance(transactions_dict, str):
            # Error occurred
            return transactions_dict
        
        # Calculate sentiment metrics
        purchase_count = 0
        sale_count = 0
        total_shares_purchased = 0
        total_shares_sold = 0
        
        for trans in transactions_dict.get("transactions", []):
            trans_type = trans.get("transaction_type", "").upper()
            shares = abs(trans.get("shares", 0))
            
            if trans_type in ["P-PURCHASE", "M-EXEMPT"]:
                purchase_count += 1
                total_shares_purchased += shares
            elif trans_type in ["S-SALE", "D-RETURN"]:
                sale_count += 1
                total_shares_sold += shares
        
        # Calculate sentiment score (-1 to 1)
        total_transactions = purchase_count + sale_count
        if total_transactions > 0:
            sentiment_score = (purchase_count - sale_count) / total_transactions
        else:
            sentiment_score = 0.0
        
        # Determine sentiment label
        if sentiment_score > 0.3:
            sentiment_label = "bullish"
        elif sentiment_score < -0.3:
            sentiment_label = "bearish"
        else:
            sentiment_label = "neutral"
        
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate start_date if using lookback_days
        if lookback_days:
            actual_start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            actual_start_date = start_date
        
        # Build dict response
        dict_response = {
            "symbol": symbol,
            "start_date": actual_start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "sentiment_score": round(sentiment_score, 3),
            "sentiment_label": sentiment_label,
            "total_transactions": total_transactions,
            "purchase_transactions": purchase_count,
            "sale_transactions": sale_count,
            "total_shares_purchased": total_shares_purchased,
            "total_shares_sold": total_shares_sold,
            "net_shares": total_shares_purchased - total_shares_sold,
            "total_purchase_value": transactions_dict.get("total_purchase_value", 0),
            "total_sale_value": transactions_dict.get("total_sale_value", 0),
            "net_transaction_value": transactions_dict.get("net_transaction_value", 0)
        }
        
        # Build markdown response
        markdown = f"# Insider Sentiment for {symbol}\n\n"
        markdown += f"**Period:** {actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n\n"
        markdown += f"## Overall Sentiment: **{sentiment_label.upper()}** ({sentiment_score:+.2f})\n\n"
        markdown += f"**Transaction Summary:**\n"
        markdown += f"- Total Transactions: {total_transactions}\n"
        markdown += f"- Purchases: {purchase_count} ({total_shares_purchased:,.0f} shares)\n"
        markdown += f"- Sales: {sale_count} ({total_shares_sold:,.0f} shares)\n"
        markdown += f"- Net Shares: {total_shares_purchased - total_shares_sold:+,.0f}\n\n"
        markdown += f"**Value Summary:**\n"
        markdown += f"- Purchase Value: ${transactions_dict.get('total_purchase_value', 0):,.2f}\n"
        markdown += f"- Sale Value: ${transactions_dict.get('total_sale_value', 0):,.2f}\n"
        markdown += f"- Net Value: ${transactions_dict.get('net_transaction_value', 0):+,.2f}\n\n"
        
        # Return based on format_type
        if format_type == "dict":
            return dict_response
        elif format_type == "both":
            return {
                "text": markdown,
                "data": dict_response
            }
        else:  # markdown
            return markdown
    
    def _format_empty_transactions(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        format_type: Literal["dict", "markdown", "both"]
    ) -> Dict[str, Any] | str:
        """Format an empty response when no transactions are found."""
        if format_type == "dict":
            return {
                "symbol": symbol,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "transaction_count": 0,
                "total_purchase_value": 0.0,
                "total_sale_value": 0.0,
                "net_transaction_value": 0.0,
                "transactions": []
            }
        else:
            return f"# Insider Transactions for {symbol}\n\nNo insider transactions found for the specified period.\n"
