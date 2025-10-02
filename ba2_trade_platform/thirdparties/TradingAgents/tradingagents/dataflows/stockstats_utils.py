import pandas as pd
from stockstats import wrap
from typing import Annotated
import os
from datetime import datetime, timedelta
from .config import get_config
from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
from ba2_trade_platform.config import CACHE_FOLDER


class StockstatsUtils:
    # Class-level data provider instance (singleton pattern)
    _data_provider = None
    
    @classmethod
    def _get_data_provider(cls):
        """Get or create the data provider instance."""
        if cls._data_provider is None:
            cls._data_provider = YFinanceDataProvider(CACHE_FOLDER)
        return cls._data_provider
    
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
        data_dir: Annotated[
            str,
            "directory where the stock data is stored.",
        ],
        online: Annotated[
            bool,
            "whether to use online tools to fetch data or offline tools. If True, will use online tools.",
        ] = False,
        interval: Annotated[
            str,
            "Data interval (1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo). Only used when online=True.",
        ] = "1d",
    ):
        df = None
        data = None

        if not online:
            try:
                data = pd.read_csv(
                    os.path.join(
                        data_dir,
                        f"{symbol}-YFin-data-2015-01-01-2025-03-25.csv",
                    )
                )
                df = wrap(data)
            except FileNotFoundError:
                raise Exception("Stockstats fail: Yahoo Finance data not fetched yet!")
        else:
            # Use the new data provider with smart caching
            provider = StockstatsUtils._get_data_provider()
            
            # Parse current date
            curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
            
            # Get config for lookback period
            config = get_config()
            lookback_days = config.get("market_history_days", 90)
            
            # Add 1 year buffer for technical indicator calculation (need history for moving averages, etc.)
            total_lookback_days = lookback_days + 365
            
            # Yahoo Finance intraday data limit: 730 days for intervals < 1d
            # Check if interval is intraday and limit accordingly
            if interval in ['1m', '5m', '15m', '30m', '1h'] and total_lookback_days > 730:
                total_lookback_days = 730
                from .. import logger as ta_logger
                ta_logger.warning(f"Limiting lookback to 730 days for {interval} interval (Yahoo Finance limit)")
            
            # Get data via data provider (uses smart cache)
            data = provider.get_dataframe(
                symbol=symbol,
                start_date=curr_date_dt - timedelta(days=total_lookback_days),
                end_date=datetime.now(),
                interval=interval
            )
            
            df = wrap(data)
            df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
    
    @staticmethod
    def get_stock_stats_range(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[str, "technical indicator to calculate"],
        start_date: Annotated[str, "start date for range, YYYY-mm-dd"],
        end_date: Annotated[str, "end date for range, YYYY-mm-dd"],
        data_dir: Annotated[str, "directory where the stock data is stored"],
        online: Annotated[bool, "whether to use online or offline data"] = False,
        interval: Annotated[str, "Data interval (1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo)"] = "1d",
    ) -> pd.DataFrame:
        """
        Get indicator values for a date range efficiently.
        Returns DataFrame with Date and indicator value columns.
        """
        df = None
        data = None

        if not online:
            try:
                data = pd.read_csv(
                    os.path.join(
                        data_dir,
                        f"{symbol}-YFin-data-2015-01-01-2025-03-25.csv",
                    )
                )
                df = wrap(data)
            except FileNotFoundError:
                raise Exception("Stockstats fail: Yahoo Finance data not fetched yet!")
        else:
            # Use the new data provider with smart caching
            provider = StockstatsUtils._get_data_provider()
            
            # Parse dates
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Get config for lookback period
            config = get_config()
            lookback_days = config.get("market_history_days", 90)
            
            # Add 1 year buffer for technical indicator calculation
            total_lookback_days = lookback_days + 365
            
            # Yahoo Finance intraday data limit: 730 days for intervals < 1d
            if interval in ['1m', '5m', '15m', '30m', '1h'] and total_lookback_days > 730:
                total_lookback_days = 730
                from .. import logger as ta_logger
                ta_logger.warning(f"Limiting lookback to 730 days for {interval} interval (Yahoo Finance limit)")
            
            # Fetch more history for indicator calculation
            fetch_start = start_dt - timedelta(days=total_lookback_days)
            
            data = provider.get_dataframe(
                symbol=symbol,
                start_date=fetch_start,
                end_date=datetime.now(),
                interval=interval
            )
            
            df = wrap(data)
            df["Date"] = pd.to_datetime(df["Date"])
        
        # Calculate indicator for all dates
        df[indicator]  # trigger stockstats to calculate
        
        # Filter to requested date range
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        # Ensure Date column is datetime
        if df["Date"].dtype == object:
            df["Date"] = pd.to_datetime(df["Date"])
        
        mask = (df["Date"] >= start_dt) & (df["Date"] <= end_dt)
        filtered_df = df.loc[mask, ["Date", indicator]].copy()
        
        # Format date as string for consistency
        filtered_df["Date"] = filtered_df["Date"].dt.strftime("%Y-%m-%d")
        
        # Rename indicator column to 'value' for consistency
        filtered_df = filtered_df.rename(columns={indicator: "value"})
        
        return filtered_df
