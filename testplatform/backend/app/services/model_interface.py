"""
Abstract interfaces for model and training services.

Provides abstraction layer allowing classification (tsai) and regression (Darts)
implementations to be used interchangeably.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd


class IModelService(ABC):
    """Abstract interface for model creation services."""

    @abstractmethod
    def create_model(
        self,
        model_type: str,
        params: Dict[str, Any],
        loss_fn: Any = None,
        epoch_callback: callable = None
    ) -> Any:
        """
        Create a model of the specified type.

        Args:
            model_type: Model architecture name (e.g., 'lstm', 'inception')
            params: Model hyperparameters
            loss_fn: Optional custom loss function
            epoch_callback: Optional callback for epoch progress

        Returns:
            Configured model ready for training
        """
        pass

    @abstractmethod
    def get_available_models(self) -> Dict[str, Dict]:
        """
        Get available model architectures with their configurations.

        Returns:
            Dictionary of model configs with default params and ranges
        """
        pass

    @abstractmethod
    def get_parameter_ranges(self, model_type: str) -> Dict[str, List]:
        """
        Get hyperparameter ranges for genetic optimization.

        Args:
            model_type: Model architecture name

        Returns:
            Dictionary mapping param names to valid value ranges
        """
        pass

    @abstractmethod
    def apply_layer_size_factor(self, params: Dict, factor: float) -> Dict:
        """
        Scale layer size parameters by a factor.

        Args:
            params: Original parameters
            factor: Scaling factor (e.g., 0.5, 1.0, 2.0)

        Returns:
            Scaled parameters
        """
        pass


class ITrainingService(ABC):
    """Abstract interface for training services."""

    @abstractmethod
    def prepare_data(
        self,
        df: pd.DataFrame,
        target_column: str,
        feature_columns: List[str],
        timeframe: str = 'daily'
    ) -> Tuple[Any, Any]:
        """
        Prepare data for model training.

        Args:
            df: DataFrame with features and target
            target_column: Name of target column
            feature_columns: List of feature column names
            timeframe: Data timeframe for frequency inference

        Returns:
            Tuple of (prepared_data, covariates/metadata)
        """
        pass

    @abstractmethod
    def prepare_data_split(
        self,
        df: pd.DataFrame,
        train_ratio: float,
        target_column: str,
        feature_columns: List[str],
        timeframe: str = 'daily'
    ) -> Tuple[Any, Any, Any, Any]:
        """
        Prepare and split data into train/test sets.

        Args:
            df: Full DataFrame
            train_ratio: Fraction for training (0.0-1.0)
            target_column: Name of target column
            feature_columns: List of feature column names
            timeframe: Data timeframe

        Returns:
            Tuple of (train_data, test_data, train_meta, test_meta)
        """
        pass

    @abstractmethod
    def train_model(
        self,
        model: Any,
        train_data: Any,
        val_data: Any = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Train a model on prepared data.

        Args:
            model: Model to train
            train_data: Training data
            val_data: Optional validation data
            **kwargs: Additional training options

        Returns:
            Training result with status, metrics, history
        """
        pass

    @abstractmethod
    def evaluate_model(
        self,
        model: Any,
        test_data: Any,
        metric: str = 'f1_score',
        threshold: float = 0.5,
        **kwargs
    ) -> Dict[str, float]:
        """
        Evaluate a trained model.

        Args:
            model: Trained model
            test_data: Test data
            metric: Primary metric to optimize
            threshold: Classification threshold (for classification)
            **kwargs: Additional evaluation options

        Returns:
            Dictionary of evaluation metrics
        """
        pass

    @abstractmethod
    def predict(
        self,
        model: Any,
        data: Any,
        **kwargs
    ) -> Any:
        """
        Generate predictions from a trained model.

        Args:
            model: Trained model
            data: Input data
            **kwargs: Additional prediction options

        Returns:
            Predictions
        """
        pass
