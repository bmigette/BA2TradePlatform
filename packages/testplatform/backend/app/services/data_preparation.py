"""
Data Preparation Service with Buffered Normalization

Provides normalization with configurable buffer for live trading use.
Parameters are exportable for applying the same normalization to live data.
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)


class DataPreparationService:
    """
    Data preparation with exportable normalization parameters.

    Supports buffered MinMax normalization that adds headroom for future
    price movements beyond the training data range.
    """

    def __init__(self, buffer_pct: float = 0.35):
        """
        Initialize data preparation service.

        Args:
            buffer_pct: Extra room above/below observed min/max (default 35%)
        """
        self.buffer_pct = buffer_pct
        self.normalization_params: Dict[str, Dict[str, Any]] = {}
        self.dropped_columns: List[str] = []  # Zero-variance columns excluded from features
        self.valid_columns: List[str] = []  # Columns with variance, in order

    def fit_transform(
        self,
        df: pd.DataFrame,
        columns: List[str],
        method: str = "minmax_buffered"
    ) -> pd.DataFrame:
        """
        Fit normalization and transform data.

        Args:
            df: DataFrame to normalize
            columns: Columns to normalize
            method: Normalization method:
                - "minmax_buffered": MinMax with buffer (default, recommended for prices)
                - "minmax": Standard MinMax (0-1)
                - "zscore": Z-score normalization (mean=0, std=1)
                - "log_returns": Log returns (scale-invariant)
                - "pct_change": Percentage change

        Returns:
            Normalized DataFrame
        """
        result = df.copy()

        for col in columns:
            if col not in df.columns:
                logger.warning(f"Column {col} not found in DataFrame, skipping")
                continue

            if method == "minmax_buffered":
                min_val = df[col].min()
                max_val = df[col].max()
                range_val = max_val - min_val

                if range_val == 0:
                    # Zero-variance column - drop from features (no predictive value)
                    self.dropped_columns.append(col)
                    self.normalization_params[col] = {
                        "method": "minmax_buffered",
                        "observed_min": float(min_val),
                        "observed_max": float(max_val),
                        "dropped": True,
                        "drop_reason": "zero_variance"
                    }
                    continue

                # Add buffer for future price movement
                buffered_min = min_val - (range_val * self.buffer_pct)
                buffered_max = max_val + (range_val * self.buffer_pct)

                # Normalize to 0-1 using buffered range
                result[col] = (df[col] - buffered_min) / (buffered_max - buffered_min)

                self.normalization_params[col] = {
                    "method": "minmax_buffered",
                    "observed_min": float(min_val),
                    "observed_max": float(max_val),
                    "buffered_min": float(buffered_min),
                    "buffered_max": float(buffered_max),
                    "buffer_pct": self.buffer_pct
                }

            elif method == "minmax":
                min_val = df[col].min()
                max_val = df[col].max()
                range_val = max_val - min_val

                if range_val == 0:
                    result[col] = 0.5
                else:
                    result[col] = (df[col] - min_val) / range_val

                self.normalization_params[col] = {
                    "method": "minmax",
                    "min": float(min_val),
                    "max": float(max_val)
                }

            elif method == "zscore":
                mean_val = df[col].mean()
                std_val = df[col].std()

                if std_val == 0:
                    result[col] = 0
                else:
                    result[col] = (df[col] - mean_val) / std_val

                self.normalization_params[col] = {
                    "method": "zscore",
                    "mean": float(mean_val),
                    "std": float(std_val)
                }

            elif method == "log_returns":
                # Log returns are naturally scale-invariant
                result[col] = np.log(df[col] / df[col].shift(1))

                self.normalization_params[col] = {
                    "method": "log_returns",
                    "reference_price": float(df[col].iloc[0])
                }

            elif method == "pct_change":
                # Percentage change - scale invariant
                result[col] = df[col].pct_change()

                self.normalization_params[col] = {
                    "method": "pct_change"
                }
            else:
                raise ValueError(f"Unknown normalization method: {method}")

        # Drop NaN rows created by log_returns or pct_change
        if method in ["log_returns", "pct_change"]:
            result = result.dropna()

        # Track valid columns (those with variance, in order)
        self.valid_columns = [col for col in columns if col not in self.dropped_columns]

        # Log warning about dropped columns
        if self.dropped_columns:
            logger.warning(
                f"Dropped {len(self.dropped_columns)} zero-variance columns from features: "
                f"{self.dropped_columns}"
            )

        return result

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply saved normalization to new data (live data).

        Uses the parameters fitted during fit_transform.

        Args:
            df: DataFrame to normalize

        Returns:
            Normalized DataFrame
        """
        if not self.normalization_params:
            raise ValueError("No normalization parameters fitted. Call fit_transform first.")

        result = df.copy()

        for col, params in self.normalization_params.items():
            if col not in df.columns:
                continue

            method = params["method"]

            if method == "minmax_buffered":
                # Skip dropped columns (zero-variance)
                if params.get("dropped", False):
                    continue

                buffered_min = params["buffered_min"]
                buffered_max = params["buffered_max"]
                result[col] = (df[col] - buffered_min) / (buffered_max - buffered_min)

                # Clip to valid range if price exceeds buffer (expected behavior for live data)
                clipped = result[col].clip(0, 1)
                # Check if any non-NaN values were clipped (NaN != NaN is True, so exclude NaN)
                non_nan_mask = ~result[col].isna()
                if ((clipped[non_nan_mask] != result[col][non_nan_mask]).any()):
                    # Get actual data range for debugging
                    actual_min = df[col].min()
                    actual_max = df[col].max()
                    logger.debug(
                        f"Column {col}: Values exceeded normalization buffer. "
                        f"Data range: {actual_min:.2f} - {actual_max:.2f}, "
                        f"Buffer range: {buffered_min:.2f} - {buffered_max:.2f}"
                    )
                result[col] = clipped

            elif method == "minmax":
                min_val = params["min"]
                max_val = params["max"]
                result[col] = (df[col] - min_val) / (max_val - min_val)
                result[col] = result[col].clip(0, 1)

            elif method == "zscore":
                mean_val = params["mean"]
                std_val = params["std"]
                if std_val > 0:
                    result[col] = (df[col] - mean_val) / std_val
                else:
                    result[col] = 0

            elif method == "log_returns":
                result[col] = np.log(df[col] / df[col].shift(1))

            elif method == "pct_change":
                result[col] = df[col].pct_change()

        return result

    def inverse_transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Denormalize predictions back to original scale.

        Args:
            df: Normalized DataFrame

        Returns:
            Denormalized DataFrame
        """
        if not self.normalization_params:
            raise ValueError("No normalization parameters fitted. Call fit_transform first.")

        result = df.copy()

        for col, params in self.normalization_params.items():
            if col not in df.columns:
                continue

            method = params["method"]

            if method == "minmax_buffered":
                buffered_min = params["buffered_min"]
                buffered_max = params["buffered_max"]
                result[col] = df[col] * (buffered_max - buffered_min) + buffered_min

            elif method == "minmax":
                min_val = params["min"]
                max_val = params["max"]
                result[col] = df[col] * (max_val - min_val) + min_val

            elif method == "zscore":
                mean_val = params["mean"]
                std_val = params["std"]
                result[col] = df[col] * std_val + mean_val

            elif method == "log_returns":
                # Can't easily inverse log returns without cumulative sum
                logger.warning(f"Column {col}: Log returns cannot be directly inverse transformed")

            elif method == "pct_change":
                logger.warning(f"Column {col}: Percentage change cannot be directly inverse transformed")

        return result

    def export_params(self) -> Dict[str, Any]:
        """
        Export normalization parameters for live use.

        Returns:
            Dictionary with all normalization parameters
        """
        return {
            "version": "1.1",
            "buffer_pct": self.buffer_pct,
            "columns": self.normalization_params,
            "valid_columns": self.valid_columns,  # Feature columns with variance, in order
            "dropped_columns": self.dropped_columns,  # Zero-variance columns excluded
            "created_at": datetime.now().isoformat(),
            "usage": {
                "python": "prep_service.load_params(this_json); normalized = prep_service.transform(live_df)",
                "inverse": "original = prep_service.inverse_transform(predictions)"
            }
        }

    def load_params(self, params: Dict[str, Any]) -> None:
        """
        Load normalization parameters from export.

        Args:
            params: Dictionary from export_params()
        """
        self.buffer_pct = params.get("buffer_pct", 0.35)
        self.normalization_params = params.get("columns", {})
        self.valid_columns = params.get("valid_columns", [])
        self.dropped_columns = params.get("dropped_columns", [])
        logger.info(
            f"Loaded normalization params: {len(self.valid_columns)} valid columns, "
            f"{len(self.dropped_columns)} dropped columns"
        )

    def save_params(self, filepath: str) -> None:
        """
        Save normalization parameters to a JSON file.

        Args:
            filepath: Path to save the JSON file
        """
        params = self.export_params()
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, 'w') as f:
            json.dump(params, f, indent=2)

        logger.info(f"Saved normalization params to {filepath}")

    def load_params_from_file(self, filepath: str) -> None:
        """
        Load normalization parameters from a JSON file.

        Args:
            filepath: Path to the JSON file
        """
        with open(filepath, 'r') as f:
            params = json.load(f)

        self.load_params(params)

    def get_column_info(self, column: str) -> Optional[Dict[str, Any]]:
        """
        Get normalization info for a specific column.

        Args:
            column: Column name

        Returns:
            Dictionary with column normalization parameters or None
        """
        return self.normalization_params.get(column)

    def get_price_range(self, column: str) -> Optional[tuple]:
        """
        Get the valid price range for a column after buffering.

        Args:
            column: Column name

        Returns:
            Tuple of (min, max) or None if column not found
        """
        params = self.normalization_params.get(column)
        if not params:
            return None

        if params["method"] == "minmax_buffered":
            return (params["buffered_min"], params["buffered_max"])
        elif params["method"] == "minmax":
            return (params["min"], params["max"])
        elif params["method"] == "zscore":
            # For z-score, return 3 standard deviations
            mean = params["mean"]
            std = params["std"]
            return (mean - 3 * std, mean + 3 * std)

        return None

    def get_valid_columns(self) -> List[str]:
        """
        Get list of valid feature columns (those with variance).

        Returns:
            List of column names in order, excluding dropped zero-variance columns
        """
        return self.valid_columns.copy()

    def get_dropped_columns(self) -> List[str]:
        """
        Get list of dropped zero-variance columns.

        Returns:
            List of column names that were dropped due to zero variance
        """
        return self.dropped_columns.copy()
