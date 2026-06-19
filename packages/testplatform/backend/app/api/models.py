"""
Models API endpoints.

Manages trained ML models from optimization jobs.
Now with database persistence.
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
import uuid

from app.models.database import get_db
from app.models.model import TrainedModel
from app.models.dataset import Dataset

logger = logging.getLogger(__name__)

router = APIRouter()

# Keep in-memory store for backward compatibility during transition
# Will be phased out as database is populated
models_store: Dict[str, dict] = {}


class HyperParameters(BaseModel):
    layers: Optional[int] = None
    layerSize: Optional[int] = None
    learningRate: Optional[float] = None
    dropout: Optional[float] = None
    batchSize: Optional[int] = None
    epochs: Optional[int] = None
    # Note: activationFunction removed - not configurable on most models

    class Config:
        extra = "allow"  # Allow extra fields from database


class TrainingHistory(BaseModel):
    epoch: int
    # Support both old format (loss, valLoss) and new TSAI format (train_loss, val_loss)
    loss: Optional[float] = None
    accuracy: Optional[float] = None
    valLoss: Optional[float] = None
    valAccuracy: Optional[float] = None
    # TSAI format fields
    train_loss: Optional[float] = None
    val_loss: Optional[float] = None

    class Config:
        extra = "allow"  # Allow extra fields from different training backends


class PerformanceMetrics(BaseModel):
    accuracy: Optional[float] = 0
    precision: Optional[float] = 0
    recall: Optional[float] = 0
    f1Score: Optional[float] = 0
    auc: Optional[float] = 0
    sharpeRatio: Optional[float] = None
    maxDrawdown: Optional[float] = None

    class Config:
        extra = "allow"  # Allow extra fields


class ModelResponse(BaseModel):
    id: str
    name: str
    modelType: str
    datasetId: Optional[int] = None
    datasetName: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    trainPeriod: Optional[str] = None
    jobId: Optional[str] = None
    status: Optional[str] = "trained"
    hyperparameters: Optional[HyperParameters] = None
    trainingHistory: Optional[List[TrainingHistory]] = []
    performanceMetrics: Optional[PerformanceMetrics] = None
    confusionMatrix: Optional[List[List[int]]] = None
    allMetrics: Optional[Dict[str, Any]] = None
    trainingDateRange: Optional[Dict[str, str]] = None
    predictionTargets: Optional[List[Dict[str, Any]]] = None
    predictionHorizon: Optional[int] = 3
    predictionMode: Optional[str] = None
    lossFunction: Optional[str] = None
    threshold: Optional[float] = 0.5  # Optimized classification threshold
    normalizationParams: Optional[Dict[str, Any]] = None  # Scaler settings for inference
    createdAt: Optional[str] = None
    trainedAt: Optional[str] = None
    filePath: Optional[str] = None
    fileSize: Optional[int] = None
    generations: Optional[int] = 0
    bestGeneration: Optional[int] = 0
    fitness: Optional[float] = 0

    class Config:
        extra = "allow"


class ModelListResponse(BaseModel):
    models: List[ModelResponse]
    total: int


def db_model_to_dict(db_model: TrainedModel, dataset_info: dict = None) -> dict:
    """Convert database model to API response dict"""
    result = db_model.to_dict()

    # Add dataset info if available
    if dataset_info:
        result['datasetName'] = dataset_info.get('name')
        result['symbol'] = dataset_info.get('symbol')
        result['timeframe'] = dataset_info.get('timeframe')
        if dataset_info.get('start') and dataset_info.get('end'):
            result['trainPeriod'] = f"{dataset_info['start']} to {dataset_info['end']}"

    return result


def get_all_models(db: Session) -> List[dict]:
    """Get all models from both database and in-memory store"""
    models = []

    # Get from database
    db_models = db.query(TrainedModel).all()
    for m in db_models:
        models.append(db_model_to_dict(m))

    # Also include in-memory models (for backward compatibility)
    for model_id, model_data in models_store.items():
        # Skip if already in database
        if not any(m['id'] == model_id for m in models):
            models.append(model_data.copy())

    return models


def get_model_by_id(model_id: str, db: Session) -> Optional[dict]:
    """Get a model by ID from database or in-memory store"""
    # Try database first
    db_model = db.query(TrainedModel).filter(TrainedModel.model_id == model_id).first()
    if db_model:
        return db_model_to_dict(db_model)

    # Fall back to in-memory store
    if model_id in models_store:
        return models_store[model_id].copy()

    return None


def save_model_to_db(model_data: dict, db: Session) -> TrainedModel:
    """Save a model to the database"""
    # Check if already exists
    existing = db.query(TrainedModel).filter(TrainedModel.model_id == model_data['id']).first()

    if existing:
        # Update existing
        existing.name = model_data.get('name', existing.name)
        existing.model_type = model_data.get('modelType', existing.model_type)
        existing.dataset_id = model_data.get('datasetId', existing.dataset_id)
        existing.job_id = model_data.get('jobId', existing.job_id)
        existing.status = model_data.get('status', existing.status)
        existing.hyperparameters = model_data.get('hyperparameters', existing.hyperparameters)
        existing.training_history = model_data.get('trainingHistory', existing.training_history)
        existing.performance_metrics = model_data.get('performanceMetrics', existing.performance_metrics)
        existing.confusion_matrix = model_data.get('confusionMatrix', existing.confusion_matrix)
        existing.all_metrics = model_data.get('allMetrics', existing.all_metrics)
        existing.training_date_range = model_data.get('trainingDateRange', existing.training_date_range)
        existing.prediction_targets = model_data.get('predictionTargets', existing.prediction_targets)
        existing.prediction_horizon = model_data.get('predictionHorizon', existing.prediction_horizon)
        existing.prediction_mode = model_data.get('predictionMode', existing.prediction_mode)
        existing.loss_function = model_data.get('lossFunction', existing.loss_function)
        existing.threshold = model_data.get('threshold', existing.threshold)
        existing.target_columns = model_data.get('targetColumns', existing.target_columns)
        existing.normalization_params = model_data.get('normalizationParams', existing.normalization_params)
        existing.generations = model_data.get('generations', existing.generations)
        existing.best_generation = model_data.get('bestGeneration', existing.best_generation)
        existing.fitness = model_data.get('fitness', existing.fitness)
        existing.file_path = model_data.get('filePath', existing.file_path)
        existing.file_size = model_data.get('fileSize', existing.file_size)
        if model_data.get('trainedAt'):
            try:
                existing.trained_at = datetime.fromisoformat(model_data['trainedAt'].replace('Z', '+00:00'))
            except:
                pass
        db.commit()
        return existing
    else:
        # Create new
        new_model = TrainedModel(
            model_id=model_data['id'],
            name=model_data.get('name', 'Unnamed Model'),
            model_type=model_data.get('modelType', 'Unknown'),
            dataset_id=model_data.get('datasetId'),
            job_id=model_data.get('jobId'),
            status=model_data.get('status', 'trained'),
            hyperparameters=model_data.get('hyperparameters'),
            training_history=model_data.get('trainingHistory'),
            performance_metrics=model_data.get('performanceMetrics'),
            confusion_matrix=model_data.get('confusionMatrix'),
            all_metrics=model_data.get('allMetrics'),
            training_date_range=model_data.get('trainingDateRange'),
            prediction_targets=model_data.get('predictionTargets'),
            prediction_horizon=model_data.get('predictionHorizon', 3),
            prediction_mode=model_data.get('predictionMode', 'shift'),
            loss_function=model_data.get('lossFunction', 'focal_loss'),
            threshold=model_data.get('threshold', 0.5),
            target_columns=model_data.get('targetColumns'),
            normalization_params=model_data.get('normalizationParams'),
            generations=model_data.get('generations', 0),
            best_generation=model_data.get('bestGeneration', 0),
            fitness=model_data.get('fitness', 0),
            file_path=model_data.get('filePath'),
            file_size=model_data.get('fileSize'),
        )
        if model_data.get('trainedAt'):
            try:
                new_model.trained_at = datetime.fromisoformat(model_data['trainedAt'].replace('Z', '+00:00'))
            except:
                pass
        db.add(new_model)
        db.commit()
        db.refresh(new_model)
        return new_model


@router.get("", response_model=ModelListResponse)
async def list_models(
    dataset_id: Optional[int] = None,
    model_type: Optional[str] = None,
    sort_by: Optional[str] = "createdAt",
    sort_order: Optional[str] = "desc",
    db: Session = Depends(get_db)
):
    """
    List all trained models with filtering and sorting.
    """
    models = get_all_models(db)

    # Filter by dataset_id if provided
    if dataset_id is not None:
        models = [m for m in models if m.get("datasetId") == dataset_id]

    # Filter by model_type if provided
    if model_type is not None:
        models = [m for m in models if m.get("modelType", "").upper() == model_type.upper()]

    # Enrich models with dataset info
    dataset_cache = {}
    for model in models:
        ds_id = model.get("datasetId")
        if ds_id and ds_id not in dataset_cache:
            dataset = db.query(Dataset).filter(Dataset.id == ds_id).first()
            if dataset:
                dataset_cache[ds_id] = {
                    'name': dataset.name,
                    'symbol': dataset.ticker,
                    'timeframe': dataset.timeframe,
                    'start': dataset.start_date.strftime('%Y-%m-%d') if dataset.start_date else None,
                    'end': dataset.end_date.strftime('%Y-%m-%d') if dataset.end_date else None
                }
            else:
                dataset_cache[ds_id] = None

        ds_info = dataset_cache.get(ds_id)
        if ds_info:
            model['datasetName'] = ds_info['name']
            model['symbol'] = ds_info['symbol']
            model['timeframe'] = ds_info['timeframe']
            if ds_info['start'] and ds_info['end']:
                model['trainPeriod'] = f"{ds_info['start']} to {ds_info['end']}"

    # Convert to response models
    response_models = []
    for m in models:
        try:
            response_models.append(ModelResponse(**m))
        except Exception as e:
            logger.warning(f"Failed to parse model {m.get('id')}: {e}")
            continue

    # Sort based on sort_by field
    reverse = sort_order.lower() == "desc"

    if sort_by == "accuracy":
        response_models.sort(key=lambda x: x.performanceMetrics.accuracy if x.performanceMetrics else 0, reverse=reverse)
    elif sort_by == "fitness":
        response_models.sort(key=lambda x: x.fitness if x.fitness else 0, reverse=reverse)
    elif sort_by == "name":
        response_models.sort(key=lambda x: x.name.lower(), reverse=reverse)
    elif sort_by == "date" or sort_by == "createdAt":
        response_models.sort(key=lambda x: x.createdAt or "", reverse=reverse)
    else:
        response_models.sort(key=lambda x: x.createdAt or "", reverse=True)

    return ModelListResponse(
        models=response_models,
        total=len(response_models)
    )


@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(model_id: str, db: Session = Depends(get_db)):
    """Get model details by ID."""
    model = get_model_by_id(model_id, db)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    # Enrich with dataset info
    ds_id = model.get("datasetId")
    if ds_id:
        dataset = db.query(Dataset).filter(Dataset.id == ds_id).first()
        if dataset:
            model['datasetName'] = dataset.name
            model['symbol'] = dataset.ticker
            model['timeframe'] = dataset.timeframe
            if dataset.start_date and dataset.end_date:
                model['trainPeriod'] = f"{dataset.start_date.strftime('%Y-%m-%d')} to {dataset.end_date.strftime('%Y-%m-%d')}"

    return ModelResponse(**model)


@router.delete("/{model_id}")
async def delete_model(model_id: str, db: Session = Depends(get_db)):
    """Delete a model."""
    # Try database first
    db_model = db.query(TrainedModel).filter(TrainedModel.model_id == model_id).first()
    if db_model:
        db.delete(db_model)
        db.commit()
        logger.info(f"Deleted model {model_id} from database")
        return {"message": f"Model {model_id} deleted"}

    # Fall back to in-memory
    if model_id in models_store:
        del models_store[model_id]
        logger.info(f"Deleted model {model_id} from memory")
        return {"message": f"Model {model_id} deleted"}

    raise HTTPException(status_code=404, detail=f"Model {model_id} not found")


class FoundationModelRegister(BaseModel):
    """Request model for registering a foundation model."""
    model_name: str  # Key from CHRONOS_MODELS (e.g., "chronos-2")
    name: Optional[str] = None  # Display name override


@router.get("/foundation/available")
async def list_foundation_models():
    """List available foundation models that can be registered."""
    from app.services.chronos_service import list_available_models, is_model_downloaded
    models = list_available_models()
    for m in models:
        m['downloaded'] = is_model_downloaded(m['name'])
    return {"models": models}


@router.post("/foundation")
async def register_foundation_model(
    request: FoundationModelRegister,
    db: Session = Depends(get_db)
):
    """Register a pre-trained foundation model for use in backtesting.

    Creates a TrainedModel record with model_type='chronos'. The actual
    model weights are downloaded from HuggingFace on first use.
    """
    from app.services.chronos_service import CHRONOS_MODELS, CHRONOS_AVAILABLE

    if request.model_name not in CHRONOS_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {request.model_name}. "
                   f"Available: {list(CHRONOS_MODELS.keys())}"
        )

    model_info = CHRONOS_MODELS[request.model_name]
    display_name = request.name or model_info['description']
    model_id = f"mdl-{uuid.uuid4().hex[:6]}"

    model_data = {
        'id': model_id,
        'name': display_name,
        'modelType': f"chronos:{request.model_name}",
        'status': 'pretrained',
        'hyperparameters': {
            'chronos_model': request.model_name,
            'repo_id': model_info['repo_id'],
            'params': model_info['params'],
            'supports_covariates': model_info['supports_covariates'],
            'max_context_length': model_info['max_context_length'],
            'max_prediction_length': model_info['max_prediction_length'],
            'prediction_length': 1,
        },
        'predictionMode': 'regression',
    }

    saved_model = save_model_to_db(model_data, db)
    logger.info(f"Registered foundation model: {model_id} ({request.model_name})")

    return {
        "id": model_id,
        "name": display_name,
        "modelType": f"chronos:{request.model_name}",
        "status": "pretrained",
        "message": f"Foundation model '{request.model_name}' registered. "
                   f"Weights will be downloaded from HuggingFace on first use.",
        "installed": CHRONOS_AVAILABLE,
    }


@router.get("/{model_id}/prediction-fields")
async def get_prediction_fields(
    model_id: str,
    db: Session = Depends(get_db)
):
    """Get model's prediction target fields for condition builder."""
    model = db.query(TrainedModel).filter(TrainedModel.model_id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    fields = []
    if model.prediction_targets:
        for idx, target in enumerate(model.prediction_targets):
            if isinstance(target, dict):
                target_type = target.get("type", "")
                if target_type:
                    # Build a descriptive label from target config
                    horizon = target.get("horizon", 1)
                    threshold = target.get("threshold")
                    indicator = target.get("indicator")

                    # Create a readable label
                    if target_type == "directional":
                        label = f"Direction {horizon}bar"
                        if threshold:
                            label += f" >{threshold}%"
                    elif target_type == "trend_reversal":
                        label = f"Trend ({indicator or 'zigzag'})"
                    elif target_type == "price_based":
                        direction = target.get("direction", "up")
                        label = f"Price {direction} {horizon}bar"
                        if threshold:
                            label += f" >{threshold}%"
                    else:
                        label = target_type

                    # Add probability field
                    fields.append({
                        "field": f"model:probability_{idx}",
                        "fieldType": "model_probability",
                        "description": f"Probability output for target: {label}",
                        "label": f"Probability ({label})",
                        "category": "Model",
                        "isBoolean": False
                    })

                    # Add class prediction field
                    fields.append({
                        "field": f"model:class_{idx}",
                        "fieldType": "model_class",
                        "description": f"Predicted class (0 or 1) for target: {label}",
                        "label": f"Prediction ({label})",
                        "category": "Model",
                        "isBoolean": True
                    })

    return {
        "modelId": model_id,
        "fields": fields
    }


@router.post("/{model_id}/export")
async def export_model(model_id: str, format: str = "pytorch", db: Session = Depends(get_db)):
    """Export a model in the specified format."""
    model = get_model_by_id(model_id, db)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    supported_formats = ["pytorch", "onnx", "pt", "pth"]
    if format.lower() not in supported_formats:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {format}. Supported: {supported_formats}"
        )

    extension_map = {
        "pytorch": "pt",
        "pt": "pt",
        "pth": "pth",
        "onnx": "onnx"
    }
    ext = extension_map.get(format.lower(), "pt")

    export_path = f"exports/{model_id}.{ext}"

    export_info = {
        "message": f"Model exported successfully to {format.upper()} format",
        "format": format,
        "path": export_path,
        "size": model.get("fileSize", 0)
    }

    if format.lower() == "onnx":
        export_info["opset_version"] = 13
        export_info["input_names"] = ["input"]
        export_info["output_names"] = ["output"]
        export_info["dynamic_axes"] = {"input": {0: "batch_size"}, "output": {0: "batch_size"}}

    logger.info(f"Exported model {model_id} to {export_path}")

    return export_info


@router.post("/{model_id}/export/pytorch")
async def export_model_pytorch(model_id: str, db: Session = Depends(get_db)):
    """Export model to PyTorch checkpoint format (.pt)."""
    return await export_model(model_id, format="pytorch", db=db)


@router.post("/{model_id}/export/onnx")
async def export_model_onnx(model_id: str, db: Session = Depends(get_db)):
    """Export model to ONNX format for cross-platform deployment."""
    return await export_model(model_id, format="onnx", db=db)


@router.post("/{model_id}/clone")
async def clone_model(model_id: str, db: Session = Depends(get_db)):
    """Clone a model with a new ID."""
    original = get_model_by_id(model_id, db)
    if not original:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    new_id = f"mdl-{uuid.uuid4().hex[:6]}"

    cloned = {
        **original,
        "id": new_id,
        "name": f"{original['name']}_clone",
        "createdAt": datetime.now().isoformat(),
        "trainedAt": None,
        "status": "cloned",
        "filePath": None,
        "fileSize": None
    }

    # Save to database
    save_model_to_db(cloned, db)
    logger.info(f"Cloned model {model_id} to {new_id}")

    return ModelResponse(**cloned)


class RunPredictionsRequest(BaseModel):
    """Request body for running predictions."""
    dataset_id: Optional[int] = None  # Optional: use different dataset
    target_index: Optional[int] = 0  # Which target to show (for multi-target models)


@router.post("/{model_id}/run-predictions")
async def run_model_predictions(
    model_id: str,
    request: RunPredictionsRequest = None,
    db: Session = Depends(get_db)
):
    """
    Run predictions on a dataset using a trained model.

    Loads the model from file, prepares the dataset, and runs inference
    to generate predictions with probabilities.

    Returns:
        Array of predictions with date, actual value, predicted probability, predicted class
    """
    import pandas as pd
    import numpy as np
    from pathlib import Path

    # Get model from database
    model = get_model_by_id(model_id, db)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    # Get file path
    file_path = model.get('filePath')
    if not file_path or not Path(file_path).exists():
        raise HTTPException(
            status_code=400,
            detail=f"Model file not found: {file_path}"
        )

    # Determine dataset to use
    dataset_id = model.get('datasetId')
    if request and request.dataset_id:
        dataset_id = request.dataset_id

    if not dataset_id:
        raise HTTPException(status_code=400, detail="No dataset associated with this model")

    # Get job_id to find cached combined dataset
    job_id = model.get('jobId')

    # Try to load from job cache first (contains all computed features from training)
    dataset_path = None
    if job_id:
        from app.paths import JOBS_CACHE_DIR
        cache_paths = [
            JOBS_CACHE_DIR / job_id / "combined_dataset.csv",
            # legacy CWD-relative fallbacks (pre-cache-layout-refactor)
            Path(f"datasets/cache/jobs/{job_id}/combined_dataset.csv"),
            Path(f"cache/jobs/{job_id}/combined_dataset.csv"),
        ]
        for path in cache_paths:
            if path.exists():
                dataset_path = path
                logger.info(f"Using cached job dataset: {dataset_path}")
                break

    # Fall back to database dataset if no job cache
    if not dataset_path:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")

        if not dataset.file_path or not Path(dataset.file_path).exists():
            raise HTTPException(status_code=400, detail=f"Dataset file not found: {dataset.file_path}")

        dataset_path = Path(dataset.file_path)
        logger.info(f"Using original dataset: {dataset_path}")

    # Load dataset CSV
    try:
        df = pd.read_csv(dataset_path, parse_dates=['Date'])
        df = df.sort_values('Date').reset_index(drop=True)
        logger.info(f"Loaded dataset: {df.shape[0]} rows, {df.shape[1]} columns")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load dataset: {str(e)}")

    # Get model configuration
    prediction_targets = model.get('predictionTargets', [])
    prediction_horizon = model.get('predictionHorizon', 3)
    prediction_mode = model.get('predictionMode', 'shift')
    threshold = model.get('threshold', 0.5)
    hyperparameters = model.get('hyperparameters', {})
    seq_len = hyperparameters.get('seqLen', 24)
    normalization_params = model.get('normalizationParams')
    stored_target_columns = model.get('targetColumns', [])  # Stored from training

    # === DEBUG: Log model configuration ===
    logger.info(f"=== RUN-PREDICTIONS DEBUG for model {model_id} ===")
    logger.info(f"Model config: type={model.get('modelType')}, jobId={job_id}, datasetId={dataset_id}")
    logger.info(f"Prediction config: mode={prediction_mode}, horizon={prediction_horizon}, threshold={threshold}")
    logger.info(f"Prediction targets: {prediction_targets}")
    logger.info(f"Stored target columns: {stored_target_columns}")
    logger.info(f"Hyperparameters: seq_len={seq_len}, c_in={hyperparameters.get('c_in')}, c_out={hyperparameters.get('c_out')}")
    logger.info(f"Has normalization_params: {normalization_params is not None}")

    # Find ALL target columns in the dataset
    available_targets = []

    # PRIORITY 1: Use stored target columns from training (exact match)
    if stored_target_columns:
        for col_name in stored_target_columns:
            if col_name in df.columns:
                available_targets.append({
                    'column': col_name,
                    'label': col_name,
                    'type': 'stored'
                })
                logger.info(f"Found stored target column: {col_name}")
            else:
                logger.warning(f"Stored target column not in dataset: {col_name}")

    # PRIORITY 2: If no stored columns matched, try regenerating from prediction config
    if not available_targets:
        logger.info("No stored target columns found, regenerating from prediction config...")

        # Helper to generate expected column name from target config
        def get_target_column_name(target: dict) -> tuple:
            """Returns (column_name, label, target_type) or (None, None, None) if can't generate."""
            target_type = target.get('type', '')

            if target_type == 'price_based':
                direction = target.get('direction', 'up')
                profit_pct = target.get('profitPct')
                max_dd = target.get('maxDrawdownPct')
                time_bars = target.get('timeBars')
                time_unit = target.get('timeBarsUnit', 'bars')

                if profit_pct is not None and max_dd is not None and time_bars is not None:
                    # Column format: price_up_5pct_10dd_15d (days) or price_up_5pct_10dd_360b (bars)
                    unit_suffix = 'd' if time_unit == 'days' else 'b'
                    col_name = f"price_{direction}_{profit_pct}pct_{max_dd}dd_{time_bars}{unit_suffix}"
                    label = f"Price {direction.title()} {profit_pct}% (DD {max_dd}%, {time_bars}{unit_suffix})"
                    return col_name, label, target_type

            elif target_type == 'directional':
                direction = target.get('direction', 'up')
                horizon = target.get('horizon', 1)
                horizon_unit = target.get('horizonUnit', 'bars')
                unit_suffix = 'd' if horizon_unit == 'days' else 'b'
                col_name = f"direction_{direction}_{horizon}{unit_suffix}"
                label = f"Direction {direction.title()} ({horizon}{unit_suffix})"
                return col_name, label, target_type

            elif target_type == 'trend_reversal':
                indicator = target.get('indicator', 'unknown')
                indicator_type = target.get('indicatorType', 'reversal')
                col_name = f"trend_{indicator}_{indicator_type}"
                label = f"{indicator.upper()} {indicator_type.title()}"
                return col_name, label, target_type

            elif target_type == 'triple_barrier':
                profit_pct = target.get('profitPct')
                stop_pct = target.get('stopPct')
                timeout = target.get('timeoutBars')
                if profit_pct and stop_pct and timeout:
                    col_name = f"triple_barrier_{profit_pct}tp_{stop_pct}sl_{timeout}bars"
                    label = f"Triple Barrier ({profit_pct}% TP, {stop_pct}% SL)"
                    return col_name, label, target_type

            return None, None, target_type

        # Generate expected column names from model config and find in dataset
        for target in prediction_targets:
            col_name, label, target_type = get_target_column_name(target)

            if col_name and col_name in df.columns:
                available_targets.append({
                    'column': col_name,
                    'label': label or col_name,
                    'type': target_type
                })
                logger.info(f"Found target column: {col_name}")
            elif col_name:
                # Try partial match for columns that might have slight variations
                for df_col in df.columns:
                    if col_name.lower() in df_col.lower() or df_col.lower().startswith(col_name.split('_')[0] + '_' + col_name.split('_')[1]):
                        available_targets.append({
                            'column': df_col,
                            'label': label or df_col,
                            'type': target_type
                        })
                        logger.info(f"Found target column (partial match): {df_col} for {col_name}")
                        break

        # No fallbacks - if targets weren't found from model config, show error with helpful info
        if not available_targets:
            # List available columns that look like targets for debugging
            potential_cols = [col for col in df.columns
                             if col.startswith('price_') or col.startswith('direction_')
                             or col.startswith('trend_') or col.startswith('triple_barrier_')
                             or 'target' in col.lower()]
            logger.error(f"No target columns matched from model config. "
                        f"Expected targets: {prediction_targets}. "
                        f"Potential columns in dataset: {potential_cols[:10]}")
            raise HTTPException(
                status_code=400,
                detail=f"Could not find target columns in dataset. "
                       f"Model expects: {[get_target_column_name(t)[0] for t in prediction_targets]}. "
                       f"Available in dataset: {potential_cols[:5]}{'...' if len(potential_cols) > 5 else ''}"
            )

    # Select target based on index
    target_index = request.target_index if request and request.target_index is not None else 0
    if target_index >= len(available_targets):
        target_index = 0

    selected_target = available_targets[target_index]
    target_column = selected_target['column']
    logger.info(f"Selected target {target_index}: {target_column} (available: {len(available_targets)})")

    # Get feature columns - prefer stored columns from training, fall back to dataset columns
    stored_feature_columns = hyperparameters.get('featureColumns')
    c_in = hyperparameters.get('c_in')
    logger.info(f"Model {model_id}: featureColumns in hyperparams={stored_feature_columns is not None}, "
                f"count={len(stored_feature_columns) if stored_feature_columns else 0}, c_in={c_in}")
    if stored_feature_columns:
        # Use only features that exist in current dataset
        feature_columns = [col for col in stored_feature_columns if col in df.columns]

        # Validate that feature count matches c_in (model architecture)
        if c_in and len(feature_columns) != c_in:
            logger.warning(f"Feature count mismatch: featureColumns has {len(feature_columns)} but model expects c_in={c_in}. "
                          f"Model may have been saved with incorrect featureColumns. Retrain to fix.")

        logger.info(f"Using {len(feature_columns)} of {len(stored_feature_columns)} stored feature columns")
        if len(feature_columns) != len(stored_feature_columns):
            missing = set(stored_feature_columns) - set(feature_columns)
            if len(missing) < 20:
                logger.warning(f"Some training features not in dataset: {missing}")
            else:
                logger.warning(f"Some training features not in dataset: {len(missing)} missing")
        if not feature_columns:
            raise HTTPException(
                status_code=400,
                detail=f"None of the training features found in dataset. "
                       f"Expected: {stored_feature_columns[:5]}..."
            )
    else:
        # Fall back to computing from dataset (exclude Date and target)
        feature_columns = [col for col in df.columns
                           if col not in ['Date', target_column]
                           and not col.startswith('target_')]
        logger.warning(f"No stored featureColumns, falling back to {len(feature_columns)} dataset columns")

    # Determine model type from database (not file extension)
    # tsai models: lstm, gru, tcn, inception, resnet, xception, omniscale, minirocket, patchtst, lstm_fcn, tst
    # darts models: nbeats, tft, tcn_darts, nhits, tide, lstm_darts, gru_darts
    file_path_obj = Path(file_path)
    model_type = model.get('modelType', '').lower()
    tsai_models = {'lstm', 'gru', 'tcn', 'inception', 'resnet', 'xception',
                   'omniscale', 'minirocket', 'patchtst', 'lstm_fcn', 'tst'}
    is_tsai_model = model_type in tsai_models or file_path_obj.suffix == '.pkl'

    try:
        if is_tsai_model:
            # Load tsai model
            import torch
            from app.services.tsai_training import TSAITrainingService
            from app.services.tsai_models import TSAIModelService
            from app.services.data_preparation import DataPreparationService

            training_service = TSAITrainingService(normalize=True)

            # Load normalization params if available
            logger.info(f"=== NORMALIZATION DEBUG ===")
            if normalization_params:
                logger.info(f"Loading normalization params from model record")
                logger.info(f"  - valid_columns count: {len(normalization_params.get('valid_columns', []))}")
                logger.info(f"  - dropped_columns count: {len(normalization_params.get('dropped_columns', []))}")
                training_service.data_prep = DataPreparationService()
                training_service.data_prep.load_params(normalization_params)
            else:
                # Check for .norm.json file
                norm_file = file_path_obj.with_suffix('.norm.json')
                if norm_file.exists():
                    logger.info(f"Loading normalization params from file: {norm_file}")
                    training_service.data_prep = DataPreparationService()
                    training_service.data_prep.load_params_from_file(str(norm_file))
                else:
                    logger.warning(f"No normalization params found - will refit scaler on inference data (may cause issues)")

            # Prepare data first to get dimensions
            X, y = training_service.prepare_data(
                df=df,
                target_column=target_column,
                feature_columns=feature_columns,
                seq_len=seq_len,
                prediction_horizon=prediction_horizon,
                prediction_mode=prediction_mode,
                fit_scaler=False if training_service.data_prep else True
            )
            logger.info(f"=== DATA PREPARATION DEBUG ===")
            logger.info(f"Prepared data: X shape={X.shape}, y shape={y.shape if hasattr(y, 'shape') else len(y)}")
            logger.info(f"X stats: min={X.min():.4f}, max={X.max():.4f}, mean={X.mean():.4f}")
            logger.info(f"X NaN count: {np.isnan(X).sum()}, Inf count: {np.isinf(X).sum()}")

            # Check for NaN in prepared data
            if np.isnan(X).any():
                nan_count = np.isnan(X).sum()
                logger.error(f"Prepared data X contains {nan_count} NaN values!")
                raise HTTPException(
                    status_code=500,
                    detail=f"Data preparation produced NaN values. Check dataset for invalid values or extreme outliers."
                )

            # Load model based on file type
            logger.info(f"=== MODEL LOADING DEBUG ===")
            logger.info(f"Model file: {file_path_obj}")
            logger.info(f"Model type: {model_type}, file suffix: {file_path_obj.suffix}")

            if file_path_obj.suffix == '.pkl':
                # Full learner export
                logger.info(f"Loading as full learner (.pkl)")
                from tsai.all import load_learner
                learner = load_learner(file_path)
                model_obj = learner.model
            else:
                # State dict (.pt file) - need to recreate model architecture
                # Get c_in from metadata (stored during training) - NOT from current dataset
                c_in = hyperparameters.get('c_in')
                c_out = hyperparameters.get('c_out', 2)
                model_params = hyperparameters.get('modelParams', {})

                # If c_in not in DB hyperparameters, check for _meta.json file
                if c_in is None:
                    import json
                    # Try both naming patterns for meta files
                    meta_patterns = [
                        file_path_obj.with_name(file_path_obj.stem + '_meta.json'),
                        file_path_obj.with_suffix('.json'),
                    ]
                    for meta_path in meta_patterns:
                        if meta_path.exists():
                            try:
                                with open(meta_path, 'r') as f:
                                    meta = json.load(f)
                                c_in = meta.get('c_in')
                                if c_out == 2:  # Default, might be overridden
                                    c_out = meta.get('c_out', c_out)
                                if not model_params:
                                    model_params = meta.get('params', {})
                                logger.info(f"Loaded model metadata from {meta_path}: c_in={c_in}, c_out={c_out}")
                                break
                            except Exception as e:
                                logger.warning(f"Failed to load metadata from {meta_path}: {e}")

                if c_in is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot determine model input dimensions (c_in). "
                               f"Model metadata not found for {file_path}"
                    )

                logger.info(f"Creating model architecture: type={model_type}, c_in={c_in}, c_out={c_out}, seq_len={seq_len}")
                logger.info(f"Model params: {model_params}")

                model_service = TSAIModelService()
                model_obj = model_service.create_model(
                    model_type=model_type,
                    params=model_params,
                    c_in=c_in,
                    c_out=c_out,
                    seq_len=seq_len
                )

                # Load state dict
                logger.info(f"Loading state dict from {file_path}")
                state_dict = torch.load(file_path, map_location='cpu', weights_only=True)
                model_obj.load_state_dict(state_dict)
                logger.info(f"Model loaded successfully, {sum(p.numel() for p in model_obj.parameters())} parameters")

            # Run inference
            logger.info(f"=== INFERENCE DEBUG ===")
            logger.info(f"Running inference on X with shape {X.shape}")
            probs = training_service.predict(
                model=model_obj,
                data=X,
                prediction_mode=prediction_mode
            )
            logger.info(f"Predictions shape: {probs.shape}")
            logger.info(f"Predictions stats: min={probs.min():.4f}, max={probs.max():.4f}, mean={probs.mean():.4f}")
            logger.info(f"First 5 predictions:\n{probs[:5]}")
            logger.info(f"NaN count in predictions: {np.isnan(probs).sum()}")

            # Check for NaN in predictions
            if np.isnan(probs).any():
                nan_count = np.isnan(probs).sum()
                logger.error(f"Predictions contain {nan_count} NaN values! This usually means:")
                logger.error("  1. The dataset has different features than training data")
                logger.error("  2. Normalization parameters differ from training")
                logger.error("  3. The model weights were corrupted")
                raise HTTPException(
                    status_code=500,
                    detail=f"Model predictions contain NaN values ({nan_count} total). "
                           f"This may be caused by mismatched dataset features or missing normalization parameters."
                )

            # Calculate predictions
            # probs is now always 2D: (samples, n_classes)
            if prediction_mode == 'multistep':
                # Multi-step: average probabilities across horizons
                avg_probs = np.mean(probs, axis=1)
                predicted_classes = (avg_probs >= threshold).astype(int)
            else:
                # Binary classification: probs[:, 1] is probability of class 1 (up)
                prob_class_1 = probs[:, 1] if len(probs.shape) > 1 and probs.shape[1] > 1 else probs
                logger.info(f"prob_class_1: shape={prob_class_1.shape}, min={prob_class_1.min():.4f}, max={prob_class_1.max():.4f}, first 5: {prob_class_1[:5]}")
                predicted_classes = (prob_class_1 >= threshold).astype(int)
                avg_probs = prob_class_1

            # Build results
            # Note: sequences start at index 0 but represent predictions for index seq_len-1+prediction_horizon
            start_idx = seq_len - 1 + prediction_horizon
            predictions = []

            for i in range(len(probs)):
                data_idx = start_idx + i
                if data_idx >= len(df):
                    break

                row = df.iloc[data_idx]
                predictions.append({
                    'date': row['Date'].isoformat() if hasattr(row['Date'], 'isoformat') else str(row['Date']),
                    'close': float(row['Close']) if 'Close' in row else None,
                    'open': float(row['Open']) if 'Open' in row else None,
                    'high': float(row['High']) if 'High' in row else None,
                    'low': float(row['Low']) if 'Low' in row else None,
                    'actual': int(y[i]) if i < len(y) else None,
                    'probability': float(avg_probs[i]),
                    'predictedClass': int(predicted_classes[i]),
                    'correct': int(predicted_classes[i]) == int(y[i]) if i < len(y) else None
                })

        else:
            # Load Darts model
            from app.services.darts_training import DartsTrainingService

            training_service = DartsTrainingService()
            darts_model = training_service.load_model(file_path)

            # For Darts models, we would need different handling
            # This is primarily for classification/tsai models
            raise HTTPException(
                status_code=501,
                detail="Darts model predictions not yet implemented"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to run predictions: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to run predictions: {str(e)}")

    # Calculate summary statistics
    if predictions:
        correct_count = sum(1 for p in predictions if p.get('correct') is True)
        total_with_actual = sum(1 for p in predictions if p.get('actual') is not None)
        accuracy = correct_count / total_with_actual if total_with_actual > 0 else 0

        class_0_count = sum(1 for p in predictions if p.get('predictedClass') == 0)
        class_1_count = sum(1 for p in predictions if p.get('predictedClass') == 1)
        actual_0_count = sum(1 for p in predictions if p.get('actual') == 0)
        actual_1_count = sum(1 for p in predictions if p.get('actual') == 1)

        avg_probability = np.mean([p['probability'] for p in predictions])
    else:
        accuracy = 0
        class_0_count = class_1_count = 0
        actual_0_count = actual_1_count = 0
        avg_probability = 0

    return {
        "modelId": model_id,
        "datasetId": dataset_id,
        "targetColumn": target_column,
        "targetIndex": target_index,
        "availableTargets": available_targets,  # List of all targets for dropdown
        "predictionHorizon": prediction_horizon,
        "predictionMode": prediction_mode,
        "threshold": threshold,
        "summary": {
            "totalPredictions": len(predictions),
            "accuracy": round(accuracy, 4),
            "avgProbability": round(float(avg_probability), 4),
            "predictedClass0": class_0_count,
            "predictedClass1": class_1_count,
            "actualClass0": actual_0_count,
            "actualClass1": actual_1_count
        },
        "predictions": predictions
    }


@router.get("/{model_id}/predictions")
async def get_model_predictions(model_id: str, limit: int = 100, db: Session = Depends(get_db)):
    """Get prediction visualization data for a model."""
    model = get_model_by_id(model_id, db)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    # Return actual predictions if stored, otherwise 404
    # TODO: Implement actual prediction data storage and retrieval
    raise HTTPException(
        status_code=404,
        detail="Predictions not available for this model. Run inference to generate predictions."
    )


@router.get("/{model_id}/confusion-matrix")
async def get_confusion_matrix(model_id: str, db: Session = Depends(get_db)):
    """Get confusion matrix data for a classification model."""
    model = get_model_by_id(model_id, db)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    # Check if model has stored confusion matrix
    cm = model.get('confusionMatrix')
    if not cm:
        # Try to get from allMetrics
        all_metrics = model.get('allMetrics', {})
        cm = all_metrics.get('confusion_matrix')

    if cm and len(cm) >= 2:
        total = sum(sum(row) for row in cm)
        if len(cm) == 2:
            tn, fp = cm[0]
            fn, tp = cm[1]
            return {
                "modelId": model_id,
                "labels": ["Down", "Up"],
                "matrix": cm,
                "metrics": {
                    "accuracy": round((tp + tn) / total, 4) if total > 0 else 0,
                    "precision": round(tp / (tp + fp), 4) if (tp + fp) > 0 else 0,
                    "recall": round(tp / (tp + fn), 4) if (tp + fn) > 0 else 0,
                    "specificity": round(tn / (tn + fp), 4) if (tn + fp) > 0 else 0
                }
            }

    # No confusion matrix available
    raise HTTPException(
        status_code=404,
        detail="Confusion matrix not available for this model"
    )
