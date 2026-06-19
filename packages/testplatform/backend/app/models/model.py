"""
Model model for storing trained model metadata
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float
from sqlalchemy.sql import func
from .database import Base


class TrainedModel(Base):
    """Trained Model stored in database for persistence"""

    __tablename__ = "trained_models"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(String(50), unique=True, index=True, nullable=False)  # e.g., "mdl-abc123"
    name = Column(String(200), nullable=False)
    model_type = Column(String(100), nullable=False)  # LSTM, N-BEATS, GRU, etc.
    dataset_id = Column(Integer, nullable=True)
    job_id = Column(String(100), nullable=True)  # Job ID string
    status = Column(String(50), default="trained")  # trained, failed, exported

    # Hyperparameters (JSON)
    hyperparameters = Column(JSON, nullable=True)

    # Training history (JSON array)
    training_history = Column(JSON, nullable=True)

    # Performance metrics (JSON)
    performance_metrics = Column(JSON, nullable=True)

    # Additional metrics
    confusion_matrix = Column(JSON, nullable=True)
    all_metrics = Column(JSON, nullable=True)

    # Training configuration
    training_date_range = Column(JSON, nullable=True)
    prediction_targets = Column(JSON, nullable=True)
    prediction_horizon = Column(Integer, default=3)
    prediction_mode = Column(String(20), default="shift")  # "shift" or "multistep"
    loss_function = Column(String(50), default="focal_loss")  # focal_loss, cross_entropy, weighted_cross_entropy
    threshold = Column(Float, default=0.5)  # Optimized classification threshold
    target_columns = Column(JSON, nullable=True)  # Actual column names generated during training

    # Data normalization parameters (for inference)
    # Stores the scaler settings so the same normalization can be applied to new data
    normalization_params = Column(JSON, nullable=True)

    # Generation info
    generations = Column(Integer, default=50)
    best_generation = Column(Integer, default=0)
    fitness = Column(Float, default=0.0)

    # File info
    file_path = Column(String(500), nullable=True)
    file_size = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    trained_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<TrainedModel(id={self.model_id}, name='{self.name}', type='{self.model_type}')>"

    def to_dict(self):
        """Convert to dictionary for API response"""
        return {
            "id": self.model_id,
            "name": self.name,
            "modelType": self.model_type,
            "datasetId": self.dataset_id,
            "jobId": self.job_id,
            "status": self.status,
            "hyperparameters": self.hyperparameters or {},
            "trainingHistory": self.training_history or [],
            "performanceMetrics": self.performance_metrics or {},
            "confusionMatrix": self.confusion_matrix,
            "allMetrics": self.all_metrics,
            "trainingDateRange": self.training_date_range,
            "predictionTargets": self.prediction_targets,
            "predictionHorizon": self.prediction_horizon,
            "predictionMode": self.prediction_mode,
            "lossFunction": self.loss_function,
            "threshold": self.threshold,
            "targetColumns": self.target_columns,
            "normalizationParams": self.normalization_params,
            "generations": self.generations,
            "bestGeneration": self.best_generation,
            "fitness": self.fitness,
            "filePath": self.file_path,
            "fileSize": self.file_size,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "trainedAt": self.trained_at.isoformat() if self.trained_at else None,
        }
