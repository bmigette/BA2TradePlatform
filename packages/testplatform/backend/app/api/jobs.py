"""
Optimization Jobs API endpoints

Uses TaskQueueService for background job processing with real ML training.
"""

from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import logging
import uuid
import asyncio
import json

from sqlalchemy.orm import Session
from app.services.task_queue import get_task_queue
from app.models.database import SessionLocal, get_db
from app.models.dataset import Dataset
from app.models.task_queue import TaskQueue

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory job store for quick access (synced with task queue)
jobs_store: dict = {}

# Flag to track if we've loaded jobs from DB
_jobs_loaded_from_db = False

# Training progress data (metrics over time)
job_progress_data: Dict[str, Dict[str, Any]] = {}

# Constants for memory management
MAX_LOGS_PER_JOB = 1000  # Maximum logs to keep in memory per job
MAX_INDIVIDUALS_IN_PROGRESS = 2000  # Maximum individuals to track in real-time


class PredictionTarget(BaseModel):
    """Prediction target format supporting both legacy and new target types.

    Supports legacy format (profitPercent, maxDrawdownPercent, timePeriodDays)
    and new target types (trend_reversal, directional, triple_barrier, etc.).

    Uses extra='allow' to preserve all target-specific fields like 'indicator',
    'direction', 'threshold', 'indicatorParams', etc. without losing them.
    """
    profitPercent: Optional[float] = None
    maxDrawdownPercent: Optional[float] = None
    timePeriodDays: Optional[int] = None
    # New target format fields
    type: Optional[str] = None  # price_based, directional, triple_barrier, trend_reversal, volatility
    category: Optional[str] = None  # binary_classification, multiclass_classification, regression
    # Type-specific fields passed through as dict (alternative to extra fields)
    config: Optional[Dict[str, Any]] = None

    class Config:
        extra = 'allow'  # Preserve target-specific fields like indicator, direction, etc.


class ParameterRanges(BaseModel):
    layersMin: int
    layersMax: int
    layersStep: int = 1
    layerSizeMin: int
    layerSizeMax: int
    layerSizeStep: int = 64
    learningRateMin: float
    learningRateMax: float
    learningRateStep: float = 0.001
    dropoutMin: float = 0.0
    dropoutMax: float = 0.5
    dropoutStep: float = 0.1
    seqLen: Optional[int] = None  # Sequence length for classification models (fixed value)
    # SeqLen optimization support
    optimizeSeqLen: Optional[bool] = False  # Whether to optimize sequence length via GA
    seqLenMin: Optional[int] = 24  # Minimum seq_len when optimizing
    seqLenMax: Optional[int] = 48  # Maximum seq_len when optimizing
    seqLenStep: Optional[int] = 12  # Step size for seq_len optimization
    # Note: activationFunctions removed - not configurable on most tsai models


class GeneticConfig(BaseModel):
    """Genetic algorithm optimization configuration"""
    populationSize: int = 20
    generations: int = 50
    elitismPercent: float = 10.0  # Percentage of best individuals to keep
    crossoverProb: float = 0.7
    mutationProb: float = 0.2
    earlyStoppingGenerations: int = 5  # Stop if no improvement for N generations
    trainingEpochs: int = 10  # Number of epochs for training each model


class MetricsConfig(BaseModel):
    """Metrics configuration for model optimization"""
    optimizeMetric: str = "f1_score"  # f1_score, accuracy, balanced_accuracy, precision, recall, auc_roc, mcc
    classificationMetric: Optional[str] = "f1_score"  # For classification targets
    regressionMetric: Optional[str] = "rmse"  # For regression targets
    lossFunction: Optional[str] = "focal_loss"  # focal_loss, weighted_cross_entropy, cross_entropy, mse
    # Multi-loss function support
    lossFunctions: Optional[List[str]] = None  # Multiple loss functions for GA optimization
    optimizeLossFunction: Optional[bool] = False  # Whether to optimize loss function as GA parameter
    # Threshold optimization settings
    thresholdMin: Optional[float] = 0.3
    thresholdMax: Optional[float] = 0.6
    thresholdStep: Optional[float] = 0.1


class CrossValidationConfig(BaseModel):
    enabled: bool = False
    mode: str = 'manual'  # 'manual' or 'kfold'
    testDatasetIds: Optional[List[int]] = None  # For manual mode
    folds: int = 5
    useDatasetAsFold: bool = True  # Use each dataset as a fold


class TrainingDateRange(BaseModel):
    """Training date range for subset training"""
    startDate: Optional[str] = None  # YYYY-MM-DD format
    endDate: Optional[str] = None  # YYYY-MM-DD format


class JobCreate(BaseModel):
    jobType: str = "classification"  # "classification" or "regression"
    datasetId: Optional[int] = None  # Single dataset (backwards compatible)
    datasetIds: Optional[List[int]] = None  # Multiple datasets
    selectedModels: List[str]
    parameterRanges: ParameterRanges
    predictionTargets: List[Any]  # Can be PredictionTarget or TargetConfig
    trainTestSplit: int
    predictionHorizon: int = 3  # Number of bars to predict ahead
    predictionModes: Optional[List[str]] = None  # ["shift", "multistep"] for classification
    crossValidation: Optional[CrossValidationConfig] = None
    geneticConfig: Optional[GeneticConfig] = None
    metricsConfig: Optional[MetricsConfig] = None
    trainingDateRange: Optional[TrainingDateRange] = None  # Subset of dataset dates to use for training


class DatasetProgress(BaseModel):
    datasetId: int
    datasetName: str
    ticker: str
    status: str  # 'pending', 'processing', 'completed'
    progress: float = 0.0
    rowsProcessed: int = 0
    totalRows: int = 0


class JobResponse(BaseModel):
    id: str
    datasetId: Optional[int] = None  # Single dataset (backwards compatible)
    datasetIds: Optional[List[int]] = None  # Multiple datasets
    datasetNames: Optional[List[str]] = None  # Dataset names for display
    selectedModels: List[str]
    parameterRanges: ParameterRanges
    predictionTargets: List[Any]  # Can be PredictionTarget or TargetConfig
    trainTestSplit: int
    predictionHorizon: int = 3  # Number of bars to predict ahead
    crossValidation: Optional[CrossValidationConfig] = None
    geneticConfig: Optional[GeneticConfig] = None
    metricsConfig: Optional[MetricsConfig] = None
    status: str  # 'queued', 'running', 'paused', 'completed', 'failed', 'cancelled'
    progress: float  # 0-100
    createdAt: str
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    error: Optional[str] = None
    # Training metrics
    currentGeneration: int = 0
    totalGenerations: int = 50
    currentLoss: Optional[float] = None
    currentAccuracy: Optional[float] = None
    bestFitness: Optional[float] = None
    gpuUtilization: Optional[float] = None
    estimatedTimeRemaining: Optional[str] = None
    optimizeMetric: Optional[str] = None  # The metric being optimized
    lossFunction: Optional[str] = None  # The loss function being used
    lossFunctions: Optional[List[str]] = None  # Multiple loss functions for optimization
    optimizeLossFunction: Optional[bool] = None  # Whether loss function is being optimized
    # Training progress details
    currentEpoch: Optional[int] = None
    totalEpochs: Optional[int] = None
    currentIndividual: Optional[int] = None
    populationSize: Optional[int] = None
    currentModelType: Optional[str] = None
    currentModelParams: Optional[Dict[str, Any]] = None  # Current model hyperparameters
    epochHistory: Optional[List[Dict[str, Any]]] = None  # Epoch-level loss history
    errorCount: Optional[int] = None  # Number of training errors
    successCount: Optional[int] = None  # Number of successful trainings
    # Multi-dataset progress
    datasetProgress: Optional[List[DatasetProgress]] = None
    currentDatasetId: Optional[int] = None
    # Cross-validation results
    foldResults: Optional[List[Dict[str, Any]]] = None
    # Parameter combinations count
    totalCombinations: Optional[int] = None
    # Dataset statistics
    trainRows: Optional[int] = None
    testRows: Optional[int] = None
    targetColumn: Optional[str] = None
    trainPositives: Optional[int] = None
    testPositives: Optional[int] = None
    trainPositivesPct: Optional[float] = None
    testPositivesPct: Optional[float] = None
    # Training date range
    trainingDateRange: Optional[TrainingDateRange] = None
    # Retrain metadata
    isRetrain: Optional[bool] = None
    sourceModelId: Optional[str] = None
    retrainMode: Optional[str] = None


class TrainingMetrics(BaseModel):
    generation: int
    loss: float
    accuracy: float
    valLoss: float
    valAccuracy: float
    fitness: float
    timestamp: str


class JobProgressResponse(BaseModel):
    job: JobResponse
    metrics: List[TrainingMetrics]
    logs: List[str]


class JobListResponse(BaseModel):
    jobs: List[JobResponse]
    total: int


def load_jobs_from_database():
    """
    Load jobs from database into jobs_store on startup or first access.
    This ensures jobs persist across app restarts.
    """
    global _jobs_loaded_from_db
    if _jobs_loaded_from_db:
        return

    db = SessionLocal()
    try:
        # Load all training_job tasks from database
        tasks = db.query(TaskQueue).filter(
            TaskQueue.task_type == 'training_job'
        ).order_by(TaskQueue.created_at.desc()).limit(100).all()

        for task in tasks:
            if task.task_id not in jobs_store:
                # Reconstruct job from task payload and status
                payload = task.payload or {}

                # Get dataset info
                dataset_ids = payload.get('dataset_ids', [])
                if not dataset_ids and payload.get('dataset_id'):
                    dataset_ids = [payload['dataset_id']]

                dataset_names = []
                dataset_progress = []
                for ds_id in dataset_ids:
                    ds_info = get_dataset_info(ds_id)
                    dataset_names.append(ds_info['datasetName'])
                    dataset_progress.append(ds_info)

                # Get genetic config with defaults
                genetic_config = payload.get('genetic_config', {})
                metrics_config = payload.get('metrics_config', {})
                param_ranges = payload.get('parameter_ranges', {})

                job_data = {
                    'id': task.task_id,
                    'datasetId': dataset_ids[0] if len(dataset_ids) == 1 else None,
                    'datasetIds': dataset_ids if len(dataset_ids) > 1 else None,
                    'datasetNames': dataset_names if len(dataset_ids) > 1 else None,
                    'selectedModels': payload.get('selected_models', []),
                    'parameterRanges': param_ranges,
                    'predictionTargets': payload.get('prediction_targets', []),
                    'trainTestSplit': payload.get('train_test_split', 80),
                    'predictionHorizon': payload.get('prediction_horizon', 3),
                    'crossValidation': payload.get('cross_validation'),
                    'geneticConfig': genetic_config,
                    'metricsConfig': metrics_config,
                    'status': task.status,
                    'progress': task.progress or 0.0,
                    'createdAt': task.created_at.isoformat() if task.created_at else datetime.now().isoformat(),
                    'startedAt': task.started_at.isoformat() if task.started_at else None,
                    'completedAt': task.completed_at.isoformat() if task.completed_at else None,
                    'error': task.error_message,
                    'currentGeneration': 0,
                    'totalGenerations': genetic_config.get('generations', 50),
                    'currentLoss': None,
                    'currentAccuracy': None,
                    'bestFitness': None,
                    'gpuUtilization': None,
                    'estimatedTimeRemaining': None,
                    'optimizeMetric': metrics_config.get('optimizeMetric', 'f1_score'),
                    'lossFunction': metrics_config.get('lossFunction', 'focal_loss'),
                    'lossFunctions': metrics_config.get('lossFunctions'),
                    'optimizeLossFunction': metrics_config.get('optimizeLossFunction', False),
                    'datasetProgress': dataset_progress if len(dataset_ids) > 1 else None,
                    'currentDatasetId': None,
                    'foldResults': None,
                    'totalCombinations': None
                }

                # Extract result data if available
                if task.result:
                    result = task.result
                    if result.get('best_model'):
                        best = result['best_model']
                        job_data['bestFitness'] = best.get('best_fitness')
                        if best.get('metrics'):
                            job_data['currentAccuracy'] = best['metrics'].get('fitness')

                jobs_store[task.task_id] = job_data

                # Initialize progress data
                if task.task_id not in job_progress_data:
                    job_progress_data[task.task_id] = {
                        "metrics": [],
                        "logs": [f"[{task.created_at.isoformat() if task.created_at else datetime.now().isoformat()}] Job loaded from database"]
                    }

        _jobs_loaded_from_db = True
        logger.info(f"Loaded {len(tasks)} jobs from database")

    except Exception as e:
        logger.error(f"Failed to load jobs from database: {e}")
    finally:
        db.close()


def get_dataset_info(dataset_id: int) -> Dict[str, Any]:
    """Get dataset information from database."""
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if dataset:
            return {
                "datasetId": dataset.id,
                "datasetName": dataset.name,
                "ticker": dataset.ticker or "UNKNOWN",
                "status": "pending",
                "progress": 0.0,
                "rowsProcessed": 0,
                "totalRows": dataset.rows_count or 0
            }
        return {
            "datasetId": dataset_id,
            "datasetName": f"Dataset_{dataset_id}",
            "ticker": f"TICKER{dataset_id}",
            "status": "pending",
            "progress": 0.0,
            "rowsProcessed": 0,
            "totalRows": 1000
        }
    finally:
        db.close()


def sync_job_from_task(job_id: str) -> Optional[Dict[str, Any]]:
    """Sync job data from task queue."""
    if job_id not in jobs_store:
        return None

    task_queue = get_task_queue()
    task_status = task_queue.get_task_status(job_id)

    if task_status:
        # Update local job store from task queue
        jobs_store[job_id]["status"] = task_status.get("status", "queued")
        jobs_store[job_id]["progress"] = task_status.get("progress", 0)

        if task_status.get("started_at"):
            jobs_store[job_id]["startedAt"] = task_status["started_at"]
        if task_status.get("completed_at"):
            jobs_store[job_id]["completedAt"] = task_status["completed_at"]
        if task_status.get("error_message"):
            jobs_store[job_id]["error"] = task_status["error_message"]
        if task_status.get("progress_message"):
            # Parse progress message for current generation info
            msg = task_status["progress_message"]
            if "Gen " in msg:
                try:
                    # Extract generation from message like "LSTM: Gen 5/50, Fitness: 0.85"
                    gen_part = msg.split("Gen ")[1].split(",")[0]
                    current, total = gen_part.split("/")
                    jobs_store[job_id]["currentGeneration"] = int(current)
                except (IndexError, ValueError):
                    pass

        # Get result data if completed or failed
        if task_status.get("result"):
            result = task_status["result"]

            # Check result status for failures
            if result.get("status") == "failed":
                jobs_store[job_id]["error"] = result.get("error", "Training failed")

            # Extract best model info if available
            if result.get("best_model"):
                best = result["best_model"]
                jobs_store[job_id]["bestFitness"] = best.get("best_fitness")
                if best.get("metrics"):
                    jobs_store[job_id]["currentAccuracy"] = best["metrics"].get("fitness")

            # Store models trained count
            if "models_trained" in result:
                jobs_store[job_id]["modelsTrained"] = result["models_trained"]
            if "total_models" in result:
                jobs_store[job_id]["totalModels"] = result["total_models"]

            # Store dataset statistics
            if "train_rows" in result:
                jobs_store[job_id]["trainRows"] = result["train_rows"]
            if "test_rows" in result:
                jobs_store[job_id]["testRows"] = result["test_rows"]
            if "target_column" in result:
                jobs_store[job_id]["targetColumn"] = result["target_column"]
            if "train_positives" in result:
                jobs_store[job_id]["trainPositives"] = result["train_positives"]
            if "test_positives" in result:
                jobs_store[job_id]["testPositives"] = result["test_positives"]
            if "train_positives_pct" in result:
                jobs_store[job_id]["trainPositivesPct"] = result["train_positives_pct"]
            if "test_positives_pct" in result:
                jobs_store[job_id]["testPositivesPct"] = result["test_positives_pct"]

    # Read training state from checkpoint_data (written by subprocess workers)
    try:
        from app.models.database import SessionLocal
        from app.models.task_queue import TaskQueue
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == job_id).first()
            if task and task.checkpoint_data:
                cp = task.checkpoint_data
                for key in ("currentGeneration", "totalGenerations", "currentIndividual",
                            "populationSize", "currentModelType", "currentEpoch",
                            "totalEpochs", "bestFitness", "errorCount", "successCount",
                            "trainRows", "testRows", "targetColumn",
                            "trainPositives", "testPositives", "trainPositivesPct", "testPositivesPct",
                            "epochHistory"):
                    if key in cp:
                        jobs_store[job_id][key] = cp[key]
        finally:
            db.close()
    except Exception:
        pass

    return jobs_store.get(job_id)


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(job_create: JobCreate):
    """
    Create a new optimization job.

    Supports both single dataset (datasetId) and multiple datasets (datasetIds).
    For multi-dataset training:
    - Datasets are combined chronologically
    - Ticker column is added to distinguish data from different tickers
    - Cross-validation can use each dataset as a fold

    Uses TaskQueueService for background ML training.

    Args:
        job_create: Job creation parameters

    Returns:
        Created job with ID and status
    """
    try:
        # Determine dataset IDs (backwards compatible)
        if job_create.datasetIds and len(job_create.datasetIds) > 0:
            dataset_ids = job_create.datasetIds
        elif job_create.datasetId:
            dataset_ids = [job_create.datasetId]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either datasetId or datasetIds must be provided"
            )

        logger.info(f"Creating optimization job for {len(dataset_ids)} dataset(s)")

        # Build dataset progress tracking from real database
        dataset_progress = []
        dataset_names = []
        for ds_id in dataset_ids:
            ds_info = get_dataset_info(ds_id)
            dataset_progress.append(ds_info)
            dataset_names.append(ds_info["datasetName"])

        # Calculate parameter combinations
        params = job_create.parameterRanges
        layers_count = max(1, (params.layersMax - params.layersMin) // params.layersStep + 1)
        layer_size_count = max(1, (params.layerSizeMax - params.layerSizeMin) // params.layerSizeStep + 1)
        lr_count = max(1, int((params.learningRateMax - params.learningRateMin) / params.learningRateStep) + 1)
        dropout_count = max(1, int((params.dropoutMax - params.dropoutMin) / params.dropoutStep) + 1)
        total_combinations = layers_count * layer_size_count * lr_count * dropout_count * len(job_create.selectedModels)

        # Get genetic config with defaults
        genetic_config = job_create.geneticConfig or GeneticConfig()
        metrics_config = job_create.metricsConfig or MetricsConfig()

        # Build payload for background task
        # Handle prediction targets - can be Pydantic models or plain dicts
        prediction_targets = []
        for pt in job_create.predictionTargets:
            if hasattr(pt, 'dict'):
                prediction_targets.append(pt.dict())
            elif isinstance(pt, dict):
                prediction_targets.append(pt)
            else:
                prediction_targets.append(pt)

        task_payload = {
            'job_type': job_create.jobType,
            'dataset_ids': dataset_ids,
            'selected_models': job_create.selectedModels,
            'parameter_ranges': params.dict(),
            'prediction_targets': prediction_targets,
            'prediction_horizon': job_create.predictionHorizon,
            'prediction_modes': job_create.predictionModes or ['shift'],
            'train_test_split': job_create.trainTestSplit,
            'cross_validation': job_create.crossValidation.dict() if job_create.crossValidation else None,
            'genetic_config': genetic_config.dict(),
            'metrics_config': metrics_config.dict(),
            'training_date_range': job_create.trainingDateRange.dict() if job_create.trainingDateRange else None
        }

        # Queue background training task
        task_queue = get_task_queue()
        task_id = task_queue.queue_task(
            task_type='training_job',
            name=f'Training job: {", ".join(job_create.selectedModels)} on {len(dataset_ids)} dataset(s)',
            payload=task_payload,
            description=f'Genetic optimization with {genetic_config.generations} generations'
        )

        # Use task_id as job_id
        job_id = task_id

        job = JobResponse(
            id=job_id,
            datasetId=dataset_ids[0] if len(dataset_ids) == 1 else None,
            datasetIds=dataset_ids if len(dataset_ids) > 1 else None,
            datasetNames=dataset_names if len(dataset_ids) > 1 else None,
            selectedModels=job_create.selectedModels,
            parameterRanges=job_create.parameterRanges,
            predictionTargets=job_create.predictionTargets,
            trainTestSplit=job_create.trainTestSplit,
            predictionHorizon=job_create.predictionHorizon,
            crossValidation=job_create.crossValidation,
            geneticConfig=genetic_config,
            metricsConfig=metrics_config,
            status="queued",
            progress=0.0,
            createdAt=datetime.now().isoformat(),
            totalGenerations=genetic_config.generations,
            optimizeMetric=metrics_config.optimizeMetric,
            lossFunction=metrics_config.lossFunction,
            lossFunctions=metrics_config.lossFunctions,
            optimizeLossFunction=metrics_config.optimizeLossFunction,
            totalCombinations=total_combinations,
            datasetProgress=dataset_progress if len(dataset_ids) > 1 else None,
            trainingDateRange=job_create.trainingDateRange,
        )

        # Store in memory for quick access
        jobs_store[job_id] = job.dict()

        # Initialize progress data
        job_progress_data[job_id] = {
            "metrics": [],
            "logs": [f"[{datetime.now().isoformat()}] Job queued for processing"]
        }

        logger.info(f"Created job {job_id} with {len(dataset_ids)} dataset(s) - queued for background processing")

        return job

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create job: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create job: {str(e)}"
        )


@router.get("", response_model=JobListResponse)
async def list_jobs():
    """
    List all optimization jobs.

    Loads jobs from database on first access, then syncs status from task queue.

    Returns:
        List of jobs with status
    """
    try:
        # Load jobs from database if not already loaded (for persistence across restarts)
        load_jobs_from_database()

        # Sync all jobs from task queue
        for job_id in list(jobs_store.keys()):
            sync_job_from_task(job_id)

        jobs = [JobResponse(**job) for job in jobs_store.values()]
        # Sort by createdAt descending
        jobs.sort(key=lambda x: x.createdAt, reverse=True)

        return JobListResponse(
            jobs=jobs,
            total=len(jobs)
        )

    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list jobs: {str(e)}"
        )


# ============================================================================
# Optimization Profiles (Database-backed)
# Must be defined BEFORE /{job_id} routes to avoid path parameter conflicts
# ============================================================================

from app.models.optimization_profile import OptimizationProfile as OptimizationProfileModel


class OptimizationProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    jobType: str = 'classification'
    selectedModels: List[str]
    parameterRanges: ParameterRanges
    predictionTargets: List[PredictionTarget]
    selectedTargetSetIds: Optional[List[int]] = None
    trainTestSplit: float = 80.0
    geneticConfig: Optional[GeneticConfig] = None
    metricsConfig: Optional[MetricsConfig] = None
    predictionHorizon: int = 3
    predictionModes: Optional[List[str]] = None


class ProfileResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    jobType: str = 'classification'
    selectedModels: List[str]
    parameterRanges: Dict[str, Any]
    predictionTargets: List[Dict[str, Any]]
    selectedTargetSetIds: Optional[List[int]] = None
    trainTestSplit: float
    geneticConfig: Optional[Dict[str, Any]] = None
    metricsConfig: Optional[Dict[str, Any]] = None
    predictionHorizon: int = 3
    predictionModes: Optional[List[str]] = None
    createdAt: str
    updatedAt: Optional[str] = None


def _profile_to_response(profile: OptimizationProfileModel) -> ProfileResponse:
    """Convert database model to response."""
    return ProfileResponse(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        jobType=profile.job_type or 'classification',
        selectedModels=profile.model_types or [],
        parameterRanges=profile.parameter_ranges or {},
        predictionTargets=profile.prediction_targets or [],
        selectedTargetSetIds=profile.selected_target_set_ids or [],
        trainTestSplit=profile.train_test_split or 80.0,
        geneticConfig=profile.genetic_config,
        metricsConfig=profile.metrics_config,
        predictionHorizon=profile.prediction_horizon or 3,
        predictionModes=profile.prediction_modes or ['shift'],
        createdAt=profile.created_at.isoformat() if profile.created_at else datetime.now().isoformat(),
        updatedAt=profile.updated_at.isoformat() if profile.updated_at else None
    )


@router.post("/profiles", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(profile: OptimizationProfileCreate, db: Session = Depends(get_db)):
    """
    Create a new optimization profile.

    Profiles save optimization settings that can be reused for multiple jobs.

    Args:
        profile: Profile settings

    Returns:
        Created profile with ID
    """
    try:
        db_profile = OptimizationProfileModel(
            name=profile.name,
            description=profile.description,
            job_type=profile.jobType,
            model_types=profile.selectedModels,
            parameter_ranges=profile.parameterRanges.dict() if profile.parameterRanges else {},
            prediction_targets=[t.dict() for t in profile.predictionTargets] if profile.predictionTargets else [],
            selected_target_set_ids=profile.selectedTargetSetIds,
            train_test_split=profile.trainTestSplit,
            genetic_config=profile.geneticConfig.dict() if profile.geneticConfig else None,
            metrics_config=profile.metricsConfig.dict() if profile.metricsConfig else None,
            prediction_horizon=profile.predictionHorizon,
            prediction_modes=profile.predictionModes
        )

        db.add(db_profile)
        db.commit()
        db.refresh(db_profile)

        logger.info(f"Created optimization profile: {profile.name} (id={db_profile.id})")
        return _profile_to_response(db_profile)

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create profile: {str(e)}"
        )


@router.get("/profiles")
async def list_profiles(db: Session = Depends(get_db)):
    """
    List all optimization profiles.

    Returns:
        List of profiles
    """
    profiles = db.query(OptimizationProfileModel).order_by(OptimizationProfileModel.created_at.desc()).all()

    return {
        "profiles": [_profile_to_response(p) for p in profiles],
        "total": len(profiles)
    }


@router.get("/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(profile_id: int, db: Session = Depends(get_db)):
    """
    Get a specific optimization profile.

    Args:
        profile_id: Profile ID

    Returns:
        Profile details
    """
    profile = db.query(OptimizationProfileModel).filter(OptimizationProfileModel.id == profile_id).first()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile {profile_id} not found"
        )

    return _profile_to_response(profile)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    """
    Delete an optimization profile.

    Args:
        profile_id: Profile ID
    """
    profile = db.query(OptimizationProfileModel).filter(OptimizationProfileModel.id == profile_id).first()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile {profile_id} not found"
        )

    db.delete(profile)
    db.commit()
    logger.info(f"Deleted profile {profile_id}")


@router.post("/profiles/{profile_id}/apply")
async def apply_profile_to_job(profile_id: int, dataset_id: int, db: Session = Depends(get_db)):
    """
    Create a new job using settings from a profile.

    Args:
        profile_id: Profile ID to apply
        dataset_id: Dataset ID for the new job

    Returns:
        Created job
    """
    profile = db.query(OptimizationProfileModel).filter(OptimizationProfileModel.id == profile_id).first()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile {profile_id} not found"
        )

    # Create job from profile
    job_create = JobCreate(
        datasetId=dataset_id,
        selectedModels=profile.model_types or [],
        parameterRanges=ParameterRanges(**(profile.parameter_ranges or {})),
        predictionTargets=[PredictionTarget(**t) for t in (profile.prediction_targets or [])],
        trainTestSplit=int(profile.train_test_split or 80),
        geneticConfig=GeneticConfig(**(profile.genetic_config or {})) if profile.genetic_config else None,
        metricsConfig=MetricsConfig(**(profile.metrics_config or {})) if profile.metrics_config else None
    )

    # Reuse create_job logic
    return await create_job(job_create)


@router.put("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(profile_id: int, profile: OptimizationProfileCreate, db: Session = Depends(get_db)):
    """
    Update an existing optimization profile.

    Args:
        profile_id: Profile ID
        profile: Updated profile settings

    Returns:
        Updated profile
    """
    db_profile = db.query(OptimizationProfileModel).filter(OptimizationProfileModel.id == profile_id).first()
    if not db_profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile {profile_id} not found"
        )

    db_profile.name = profile.name
    db_profile.description = profile.description
    db_profile.model_types = profile.selectedModels
    db_profile.parameter_ranges = profile.parameterRanges.dict() if profile.parameterRanges else {}
    db_profile.prediction_targets = [t.dict() for t in profile.predictionTargets] if profile.predictionTargets else []
    db_profile.train_test_split = profile.trainTestSplit
    db_profile.genetic_config = profile.geneticConfig.dict() if profile.geneticConfig else None
    db_profile.metrics_config = profile.metricsConfig.dict() if profile.metricsConfig else None
    db_profile.prediction_horizon = profile.predictionHorizon

    db.commit()
    db.refresh(db_profile)
    logger.info(f"Updated profile {profile_id}")

    return _profile_to_response(db_profile)


@router.get("/profiles/{profile_id}/export")
async def export_profile(profile_id: int, db: Session = Depends(get_db)):
    """
    Export optimization profile to JSON format.

    Args:
        profile_id: Profile ID

    Returns:
        JSON representation of the profile
    """
    profile = db.query(OptimizationProfileModel).filter(OptimizationProfileModel.id == profile_id).first()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile {profile_id} not found"
        )

    # Create exportable format (exclude internal IDs)
    export_data = {
        "name": profile.name,
        "description": profile.description,
        "selectedModels": profile.model_types or [],
        "parameterRanges": profile.parameter_ranges or {},
        "predictionTargets": profile.prediction_targets or [],
        "trainTestSplit": profile.train_test_split or 80,
        "geneticConfig": profile.genetic_config,
        "metricsConfig": profile.metrics_config,
        "predictionHorizon": profile.prediction_horizon or 3,
        "exportedAt": datetime.now().isoformat(),
        "version": "1.0"
    }

    return export_data


@router.post("/profiles/import", response_model=ProfileResponse)
async def import_profile(profile_data: Dict[str, Any], db: Session = Depends(get_db)):
    """
    Import optimization profile from JSON format.

    Args:
        profile_data: JSON profile data

    Returns:
        Created profile with new ID
    """
    try:
        # Validate required fields
        required_fields = ["name", "selectedModels", "parameterRanges", "predictionTargets", "trainTestSplit"]
        for field in required_fields:
            if field not in profile_data:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Missing required field: {field}"
                )

        # Create profile from import data
        profile = OptimizationProfileCreate(
            name=profile_data["name"],
            description=profile_data.get("description"),
            selectedModels=profile_data["selectedModels"],
            parameterRanges=ParameterRanges(**profile_data["parameterRanges"]),
            predictionTargets=[PredictionTarget(**t) for t in profile_data["predictionTargets"]],
            trainTestSplit=profile_data["trainTestSplit"],
            geneticConfig=GeneticConfig(**profile_data["geneticConfig"]) if profile_data.get("geneticConfig") else None,
            metricsConfig=MetricsConfig(**profile_data["metricsConfig"]) if profile_data.get("metricsConfig") else None,
            predictionHorizon=profile_data.get("predictionHorizon", 3)
        )

        # Create as new profile
        return await create_profile(profile, db)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to import profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid profile format: {str(e)}"
        )


# ============================================================================
# Job-specific Routes (must come AFTER /profiles routes)
# ============================================================================

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """
    Get a specific job by ID.

    Loads from database if needed, then syncs status from task queue.

    Args:
        job_id: Job ID

    Returns:
        Job details
    """
    # Load jobs from database first to ensure we have all jobs
    load_jobs_from_database()

    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Sync from task queue
    sync_job_from_task(job_id)

    return JobResponse(**jobs_store[job_id])


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str):
    """
    Delete a job by ID.

    Deletes from both in-memory store and database.

    Args:
        job_id: Job ID
    """
    # Load jobs from database first to ensure we have all jobs
    load_jobs_from_database()

    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Cancel task if running
    task_queue = get_task_queue()
    task_queue.cancel_task(job_id)

    # Delete from database
    db = SessionLocal()
    try:
        db.query(TaskQueue).filter(TaskQueue.task_id == job_id).delete()
        db.commit()
        logger.info(f"Deleted job {job_id} from database")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete job {job_id} from database: {e}")
    finally:
        db.close()

    # Delete from in-memory stores
    del jobs_store[job_id]
    if job_id in job_progress_data:
        del job_progress_data[job_id]
    logger.info(f"Deleted job {job_id}")


@router.get("/{job_id}/progress", response_model=JobProgressResponse)
async def get_job_progress(job_id: str):
    """
    Get detailed progress information for a job including metrics and logs.

    Syncs status from task queue before returning.

    Args:
        job_id: Job ID

    Returns:
        Job with progress data, metrics history, and logs
    """
    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Sync from task queue
    sync_job_from_task(job_id)

    # Get progress message from task queue
    task_queue = get_task_queue()
    task_progress = task_queue.get_task_progress(job_id)
    if task_progress and task_progress.get("progress_message"):
        # Add task progress message to logs if new
        progress_msg = task_progress["progress_message"]
        if job_id in job_progress_data:
            logs = job_progress_data[job_id].get("logs", [])
            if not logs or progress_msg not in logs[-1]:
                job_progress_data[job_id]["logs"].append(
                    f"[{datetime.now().isoformat()}] {progress_msg}"
                )
                # Trim logs if they exceed limit to prevent memory bloat
                if len(job_progress_data[job_id]["logs"]) > MAX_LOGS_PER_JOB:
                    job_progress_data[job_id]["logs"] = job_progress_data[job_id]["logs"][-MAX_LOGS_PER_JOB:]

    job = JobResponse(**jobs_store[job_id])
    progress_data = job_progress_data.get(job_id, {"metrics": [], "logs": []})

    # Convert metrics to TrainingMetrics objects
    metrics = [TrainingMetrics(**m) for m in progress_data.get("metrics", [])]

    # Limit logs to prevent memory bloat on frontend (500 most recent)
    all_logs = progress_data.get("logs", [])
    logs = all_logs[-500:] if len(all_logs) > 500 else all_logs

    return JobProgressResponse(
        job=job,
        metrics=metrics,
        logs=logs
    )


@router.post("/{job_id}/pause")
async def pause_job(job_id: str):
    """
    Pause a running job.

    Args:
        job_id: Job ID
    """
    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Sync and check status
    sync_job_from_task(job_id)
    job = jobs_store[job_id]

    if job["status"] != "running":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot pause job in status: {job['status']}"
        )

    # Pause via task queue
    task_queue = get_task_queue()
    if task_queue.pause_task(job_id):
        jobs_store[job_id]["status"] = "paused"
        if job_id in job_progress_data:
            job_progress_data[job_id]["logs"].append(
                f"[{datetime.now().isoformat()}] Job paused"
            )
        logger.info(f"Paused job {job_id}")
        return {"status": "paused", "message": f"Job {job_id} paused"}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to pause job"
        )


@router.post("/{job_id}/resume")
async def resume_job(job_id: str):
    """
    Resume a paused or stopped (crashed) job.

    Args:
        job_id: Job ID
    """
    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Sync and check status
    sync_job_from_task(job_id)
    job = jobs_store[job_id]

    # Allow resuming paused or stopped (crashed) jobs
    if job["status"] not in ["paused", "stopped"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resume job in status: {job['status']}"
        )

    # Resume via task queue
    task_queue = get_task_queue()
    if task_queue.resume_task(job_id):
        jobs_store[job_id]["status"] = "queued"  # Re-queued for processing
        if job_id in job_progress_data:
            job_progress_data[job_id]["logs"].append(
                f"[{datetime.now().isoformat()}] Job resumed"
            )
        logger.info(f"Resumed job {job_id}")
        return {"status": "running", "message": f"Job {job_id} resumed"}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to resume job"
        )


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    """
    Cancel a running or paused job.

    Args:
        job_id: Job ID
    """
    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Sync and check status
    sync_job_from_task(job_id)
    job = jobs_store[job_id]

    if job["status"] not in ["running", "paused", "queued"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job in status: {job['status']}"
        )

    # Cancel via task queue
    task_queue = get_task_queue()
    task_queue.cancel_task(job_id)

    jobs_store[job_id]["status"] = "cancelled"
    if job_id in job_progress_data:
        job_progress_data[job_id]["logs"].append(
            f"[{datetime.now().isoformat()}] Job cancelled by user"
        )

    logger.info(f"Cancelled job {job_id}")
    return {"status": "cancelled", "message": f"Job {job_id} cancelled"}


# ============================================================================
# SSE (Server-Sent Events) for Live Progress
# ============================================================================

async def generate_sse_events(job_id: str):
    """
    Generator for SSE events for job progress.

    Syncs with task queue for real-time status.

    Yields:
        SSE formatted events with job progress data
    """
    last_progress = -1

    while True:
        if job_id not in jobs_store:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
            break

        # Sync from task queue
        sync_job_from_task(job_id)

        job = jobs_store[job_id]
        current_progress = job.get("progress", 0)
        current_gen = job.get("currentGeneration", 0)

        # Send update if progress changed or status changed
        if current_progress != last_progress or job["status"] in ["completed", "cancelled", "failed"]:
            event_data = {
                "type": "progress",
                "job_id": job_id,
                "status": job["status"],
                "progress": current_progress,
                "currentGeneration": current_gen,
                "totalGenerations": job.get("totalGenerations", 50),
                "currentLoss": job.get("currentLoss"),
                "currentAccuracy": job.get("currentAccuracy"),
                "bestFitness": job.get("bestFitness"),
                "gpuUtilization": job.get("gpuUtilization"),
                "estimatedTimeRemaining": job.get("estimatedTimeRemaining"),
                "timestamp": datetime.now().isoformat()
            }
            yield f"data: {json.dumps(event_data)}\n\n"
            last_progress = current_progress

        # Exit if job finished
        if job["status"] in ["completed", "cancelled", "failed"]:
            yield f"data: {json.dumps({'type': 'complete', 'status': job['status']})}\n\n"
            break

        await asyncio.sleep(0.5)  # Poll every 500ms


@router.get("/{job_id}/sse")
async def get_job_progress_sse(job_id: str):
    """
    Get live job progress via Server-Sent Events (SSE).

    This endpoint streams real-time updates about job progress.
    Connect with EventSource in the browser.

    Args:
        job_id: Job ID

    Returns:
        SSE stream with progress events
    """
    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    return StreamingResponse(
        generate_sse_events(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/{job_id}/logs")
async def get_job_logs(job_id: str, limit: int = 100):
    """
    Get training logs for a job.

    Args:
        job_id: Job ID
        limit: Maximum number of log entries to return

    Returns:
        List of log entries
    """
    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    progress_data = job_progress_data.get(job_id, {"logs": []})
    logs = progress_data.get("logs", [])

    # Return last N logs
    return {
        "job_id": job_id,
        "logs": logs[-limit:],
        "total": len(logs)
    }


@router.get("/{job_id}/individuals")
async def get_job_individuals(job_id: str, generation: Optional[int] = None, model_type: Optional[str] = None):
    """
    Get all individuals evaluated during optimization for visualization.

    Returns data for each individual including model type, parameters, fitness, and metrics.
    Used to visualize optimization progress across generations and model types.

    Args:
        job_id: Job ID
        generation: Filter by specific generation (optional)
        model_type: Filter by model type (optional)

    Returns:
        List of individual evaluations with parameters and metrics
    """
    # Load jobs from database if needed
    load_jobs_from_database()

    if job_id not in jobs_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # First check jobs_store for real-time data during running jobs
    all_individuals = []
    if job_id in jobs_store and "allIndividuals" in jobs_store[job_id]:
        all_individuals = jobs_store[job_id]["allIndividuals"]

    # Check checkpoint_data (for subprocess mode — real-time during training)
    if not all_individuals:
        try:
            from app.models.database import SessionLocal
            from app.models.task_queue import TaskQueue
            db = SessionLocal()
            try:
                task = db.query(TaskQueue).filter(TaskQueue.task_id == job_id).first()
                if task and task.checkpoint_data:
                    all_individuals = task.checkpoint_data.get("allIndividuals", [])
            finally:
                db.close()
        except Exception:
            pass

    # If not in checkpoint_data, check task queue result (for completed jobs)
    if not all_individuals:
        task_queue = get_task_queue()
        task_status = task_queue.get_task_status(job_id)

        if task_status and task_status.get("result"):
            result = task_status["result"]
            all_individuals = result.get("all_individuals", [])

            # Also check in results array
            if not all_individuals and "results" in result:
                for r in result["results"]:
                    if "all_individuals" in r:
                        all_individuals.extend(r["all_individuals"])

    # Apply filters
    if generation is not None:
        all_individuals = [i for i in all_individuals if i.get("generation") == generation]
    if model_type:
        all_individuals = [i for i in all_individuals if i.get("model_type", "").lower() == model_type.lower()]

    # Calculate summary stats
    summary = {
        "total_individuals": len(all_individuals),
        "generations": sorted(set(i.get("generation", 0) for i in all_individuals)),
        "model_types": sorted(set(i.get("model_type", "unknown") for i in all_individuals)),
        "best_fitness": max((i.get("fitness", 0) for i in all_individuals), default=0),
        "avg_fitness": sum(i.get("fitness", 0) for i in all_individuals) / len(all_individuals) if all_individuals else 0
    }

    # Find best individual
    best_individual = None
    if all_individuals:
        best_individual = max(all_individuals, key=lambda x: x.get("fitness", 0))

    return {
        "job_id": job_id,
        "summary": summary,
        "best_individual": best_individual,
        "individuals": all_individuals
    }


@router.get("/{job_id}/generations")
async def get_job_generations(job_id: str):
    """
    Get generation-by-generation summary of optimization progress.

    Returns aggregated stats for each generation including best/avg fitness,
    model type distribution, and top individuals.

    Args:
        job_id: Job ID

    Returns:
        List of generation summaries
    """
    # Get all individuals
    individuals_response = await get_job_individuals(job_id)
    all_individuals = individuals_response["individuals"]

    # Group by generation
    generations_data = {}
    for ind in all_individuals:
        gen = ind.get("generation", 0)
        if gen not in generations_data:
            generations_data[gen] = {
                "generation": gen,
                "individuals": [],
                "model_types": {},
                "best_fitness": 0,
                "avg_fitness": 0
            }
        generations_data[gen]["individuals"].append(ind)

        # Count model types
        model_type = ind.get("model_type", "unknown")
        generations_data[gen]["model_types"][model_type] = \
            generations_data[gen]["model_types"].get(model_type, 0) + 1

    # Calculate stats per generation
    generations = []
    for gen, data in sorted(generations_data.items()):
        individuals = data["individuals"]
        fitnesses = [i.get("fitness", 0) for i in individuals]

        gen_summary = {
            "generation": gen,
            "individual_count": len(individuals),
            "best_fitness": max(fitnesses) if fitnesses else 0,
            "avg_fitness": sum(fitnesses) / len(fitnesses) if fitnesses else 0,
            "min_fitness": min(fitnesses) if fitnesses else 0,
            "model_types": data["model_types"],
            "best_individual": max(individuals, key=lambda x: x.get("fitness", 0)) if individuals else None
        }
        generations.append(gen_summary)

    return {
        "job_id": job_id,
        "total_generations": len(generations),
        "generations": generations
    }


class EliteModelResponse(BaseModel):
    rank: int
    model_type: str
    fitness: float
    file_path: str
    file_name: str
    metrics: Dict[str, Any]
    params: Dict[str, Any]


class SaveToInventoryRequest(BaseModel):
    name: Optional[str] = None


@router.get("/{job_id}/elite-models")
async def get_job_elite_models(job_id: str):
    """
    Get elite models saved for a completed job.

    Returns:
        List of elite model info with rank, model_type, fitness, metrics, etc.
    """
    from app.services.job_handler import get_elite_models

    elite_models = get_elite_models(job_id)

    return {
        "job_id": job_id,
        "elite_models": elite_models,
        "total": len(elite_models)
    }


@router.post("/{job_id}/elite-models/{rank}/save-to-inventory")
async def save_elite_to_inventory(
    job_id: str,
    rank: int,
    request: SaveToInventoryRequest,
    db: Session = Depends(get_db)
):
    """
    Save an elite model from a job to the model inventory.

    Args:
        job_id: Job ID
        rank: Elite model rank (1-based)
        request: Optional custom name

    Returns:
        Saved model info
    """
    from app.services.job_handler import get_elite_models, get_job_models_dir
    from app.api.models import models_store
    import uuid
    import json

    # Get elite models
    elite_models = get_elite_models(job_id)
    elite_model = next((m for m in elite_models if m['rank'] == rank), None)

    if not elite_model:
        raise HTTPException(status_code=404, detail=f"Elite model rank {rank} not found")

    # Get job info for dataset details
    load_jobs_from_database()
    job = jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Get dataset info
    dataset_id = job.get('datasetId')
    dataset_ids = job.get('datasetIds', [])
    if not dataset_id and dataset_ids:
        dataset_id = dataset_ids[0]

    # Generate model ID
    model_id = f"mdl-{uuid.uuid4().hex[:8]}"

    # Create model name
    model_type = elite_model['model_type'].upper()
    if request.name:
        model_name = request.name
    else:
        model_name = f"{model_type}_Job{job_id}_Rank{rank}"

    # Extract metrics
    metrics = elite_model.get('metrics', {})
    params = elite_model.get('params', {})

    # Create model entry
    model_entry = {
        "id": model_id,
        "name": model_name,
        "modelType": model_type,
        "datasetId": dataset_id,
        "jobId": job_id,
        "status": "trained",
        "hyperparameters": {
            "layers": params.get('n_rnn_layers', params.get('n_layers', 2)),
            "layerSize": params.get('hidden_dim', params.get('hidden_size', 64)) if isinstance(params.get('hidden_dim', params.get('hidden_size')), int) else 64,
            "learningRate": params.get('learning_rate', 0.001),
            "dropout": params.get('dropout', 0.1),
            "batchSize": params.get('batch_size', 32),
            "epochs": job.get('geneticConfig', {}).get('trainingEpochs', 10),
            # Critical for model loading - get from _meta.json via elite_model
            "c_in": elite_model.get('c_in'),
            "c_out": elite_model.get('c_out'),
            "seqLen": elite_model.get('seq_len') or job.get('parameterRanges', {}).get('seqLen'),
            # Model-specific params for recreation
            "modelParams": params,
            # Feature columns used during training - critical for prediction
            "featureColumns": elite_model.get('feature_columns')
        },
        "trainingHistory": elite_model.get('training_history', []),
        "performanceMetrics": {
            "accuracy": metrics.get('accuracy', 0),
            "precision": metrics.get('precision', 0),
            "recall": metrics.get('recall', 0),
            "f1Score": metrics.get('f1_score', 0),
            "auc": metrics.get('auc_roc', 0),
            "sharpeRatio": None,
            "maxDrawdown": None
        },
        "createdAt": datetime.now().isoformat(),
        "trainedAt": job.get('completedAt'),
        "filePath": elite_model['file_path'],
        "fileSize": None,
        "generations": job.get('totalGenerations', 50),
        "bestGeneration": elite_model.get('generation', job.get('currentGeneration', job.get('totalGenerations', 50))),
        "fitness": elite_model['fitness'],
        # Additional fields for new requirements
        "confusionMatrix": metrics.get('confusion_matrix'),
        "allMetrics": metrics,
        "allParams": params,
        # Training date range
        "trainingDateRange": job.get('trainingDateRange'),
        # Prediction targets and horizon - critical for model inference
        "predictionTargets": job.get('predictionTargets', []),
        "predictionHorizon": job.get('predictionHorizon', 3),
        # Actual target column names generated during training (for exact matching in predictions)
        "targetColumns": elite_model.get('target_columns') or job.get('targetColumns', []),
        # Normalization params - critical for inference to apply same transformation
        "normalizationParams": elite_model.get('normalization_params') or job.get('normalizationParams'),
        # Classification training params (from GA optimization)
        "predictionMode": elite_model.get('prediction_mode') or params.get('prediction_mode'),
        "lossFunction": elite_model.get('loss_function') or params.get('loss_function'),
        "threshold": elite_model.get('threshold') or params.get('threshold', 0.5)
    }

    # Save to database
    from app.models.database import SessionLocal
    from app.api.models import save_model_to_db
    db = SessionLocal()
    try:
        save_model_to_db(model_entry, db)
    finally:
        db.close()

    # Also keep in memory for backward compatibility
    models_store[model_id] = model_entry

    logger.info(f"Saved elite model rank {rank} from job {job_id} as {model_id}")

    return {
        "success": True,
        "modelId": model_id,
        "modelName": model_name,
        "message": f"Model saved to inventory as {model_name}"
    }


class RetrainJobCreate(BaseModel):
    """Request to create a retrain job for an existing model"""
    sourceModelId: str  # ID of the model to retrain
    datasetId: Optional[int] = None  # Optional different dataset
    trainingDateRange: Optional[TrainingDateRange] = None  # Subset of dataset
    retrainMode: str = "from_scratch"  # "load_weights" (continue training) or "from_scratch"
    epochs: int = 10  # Number of epochs for retraining


@router.post("/retrain", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_retrain_job(retrain_request: RetrainJobCreate):
    """
    Create a retrain job for an existing model.

    Args:
        retrain_request: Retrain configuration containing:
            - sourceModelId: ID of the model to retrain
            - datasetId: Optional different dataset (uses model's dataset if not specified)
            - trainingDateRange: Optional date range subset
            - retrainMode: 'load_weights' or 'from_scratch'
            - epochs: Number of training epochs

    Returns:
        Created retrain job with ID
    """
    from app.api.models import models_store

    try:
        # Get source model
        source_model = models_store.get(retrain_request.sourceModelId)
        if not source_model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Source model {retrain_request.sourceModelId} not found"
            )

        # Determine dataset to use
        dataset_id = retrain_request.datasetId or source_model.get('datasetId')
        if not dataset_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No dataset specified and source model has no associated dataset"
            )

        # Get dataset info
        ds_info = get_dataset_info(dataset_id)

        # Build job configuration based on source model
        model_type = source_model.get('modelType', 'lstm').lower()
        hyperparams = source_model.get('hyperparameters', {})
        all_params = source_model.get('allParams', {})

        # Create genetic config with single generation (just train the model)
        genetic_config = GeneticConfig(
            populationSize=1,
            generations=1,
            elitismPercent=100,
            crossoverProb=0,
            mutationProb=0,
            earlyStoppingGenerations=1,
            trainingEpochs=retrain_request.epochs
        )

        # Build parameter ranges from model params (fixed values)
        params = all_params if all_params else hyperparams
        parameter_ranges = ParameterRanges(
            layersMin=params.get('n_rnn_layers', params.get('n_layers', params.get('layers', 2))),
            layersMax=params.get('n_rnn_layers', params.get('n_layers', params.get('layers', 2))),
            layersStep=1,
            layerSizeMin=params.get('hidden_dim', params.get('hidden_size', params.get('layerSize', 64))),
            layerSizeMax=params.get('hidden_dim', params.get('hidden_size', params.get('layerSize', 64))),
            layerSizeStep=1,
            learningRateMin=params.get('learning_rate', params.get('learningRate', 0.001)),
            learningRateMax=params.get('learning_rate', params.get('learningRate', 0.001)),
            learningRateStep=0.001,
            dropoutMin=params.get('dropout', 0.1),
            dropoutMax=params.get('dropout', 0.1),
            dropoutStep=0.1
        )

        # Get prediction targets from original job if available
        original_job_id = source_model.get('jobId')
        prediction_targets = []
        if original_job_id and original_job_id in jobs_store:
            original_job = jobs_store[original_job_id]
            prediction_targets = original_job.get('predictionTargets', [])

        # Build payload for background task
        task_payload = {
            'dataset_ids': [dataset_id],
            'selected_models': [model_type],
            'parameter_ranges': parameter_ranges.dict(),
            'prediction_targets': prediction_targets if isinstance(prediction_targets, list) else [prediction_targets],
            'train_test_split': 80,
            'cross_validation': None,
            'genetic_config': genetic_config.dict(),
            'metrics_config': {'optimizeMetric': 'f1_score'},
            'training_date_range': retrain_request.trainingDateRange.dict() if retrain_request.trainingDateRange else None,
            # Retrain-specific fields
            'is_retrain': True,
            'source_model_id': retrain_request.sourceModelId,
            'source_model_path': source_model.get('filePath'),
            'retrain_mode': retrain_request.retrainMode  # load_weights or from_scratch
        }

        # Queue background training task
        task_queue = get_task_queue()
        task_id = task_queue.queue_task(
            task_type='training_job',
            name=f'Retrain {model_type.upper()} model on dataset {ds_info.get("name", dataset_id)}',
            payload=task_payload,
            description=f'Retrain model ({retrain_request.retrainMode}) for {retrain_request.epochs} epochs'
        )

        job = JobResponse(
            id=task_id,
            datasetId=dataset_id,
            selectedModels=[model_type],
            parameterRanges=parameter_ranges,
            predictionTargets=[PredictionTarget(**pt) if isinstance(pt, dict) else pt for pt in prediction_targets] if prediction_targets else [],
            trainTestSplit=80,
            geneticConfig=genetic_config,
            metricsConfig=MetricsConfig(optimizeMetric='f1_score', lossFunction='focal_loss'),
            status="queued",
            progress=0.0,
            createdAt=datetime.now().isoformat(),
            totalGenerations=1,
            optimizeMetric='f1_score',
            lossFunction='focal_loss',
            trainingDateRange=retrain_request.trainingDateRange,
        )

        # Store in memory with retrain metadata
        job_dict = job.dict()
        job_dict['isRetrain'] = True
        job_dict['sourceModelId'] = retrain_request.sourceModelId
        job_dict['retrainMode'] = retrain_request.retrainMode
        jobs_store[task_id] = job_dict

        # Initialize progress data
        job_progress_data[task_id] = {
            "metrics": [],
            "logs": [f"[{datetime.now().isoformat()}] Retrain job queued for processing"]
        }

        logger.info(f"Created retrain job {task_id} for model {retrain_request.sourceModelId}")

        return job

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create retrain job: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


class RetrainSaveRequest(BaseModel):
    """Request to save retrain results"""
    saveMode: str = "new"  # "update_original" or "new"
    newModelName: Optional[str] = None  # Only used when saveMode is "new"


@router.post("/{job_id}/retrain-save")
async def save_retrain_results(job_id: str, request: RetrainSaveRequest):
    """
    Save retrain job results - either update the original model or save as new.

    Args:
        job_id: Retrain job ID
        request: Save mode and optional new model name

    Returns:
        Updated/created model info
    """
    from app.api.models import models_store
    from app.services.job_handler import get_elite_models

    try:
        # Load jobs
        load_jobs_from_database()
        job = jobs_store.get(job_id)

        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        if not job.get('isRetrain'):
            raise HTTPException(status_code=400, detail="This is not a retrain job")

        if job.get('status') != 'completed':
            raise HTTPException(status_code=400, detail="Job is not completed yet")

        # Get elite models from the retrain job
        elite_models = get_elite_models(job_id)
        if not elite_models:
            raise HTTPException(status_code=404, detail="No trained models found for this job")

        best_model = elite_models[0]
        source_model_id = job.get('sourceModelId')

        if request.saveMode == "update_original":
            # Update the original model with new training results
            if not source_model_id or source_model_id not in models_store:
                raise HTTPException(status_code=404, detail=f"Original model {source_model_id} not found")

            original_model = models_store[source_model_id]

            # Update model with new metrics and path
            original_model['filePath'] = best_model['file_path']
            original_model['fitness'] = best_model['fitness']
            original_model['performanceMetrics'] = {
                "accuracy": best_model['metrics'].get('accuracy', 0),
                "precision": best_model['metrics'].get('precision', 0),
                "recall": best_model['metrics'].get('recall', 0),
                "f1Score": best_model['metrics'].get('f1_score', 0),
                "auc": best_model['metrics'].get('auc_roc', 0),
            }
            original_model['allMetrics'] = best_model['metrics']
            original_model['allParams'] = best_model['params']
            original_model['confusionMatrix'] = best_model['metrics'].get('confusion_matrix')
            original_model['trainedAt'] = datetime.now().isoformat()
            original_model['trainingDateRange'] = job.get('trainingDateRange')

            # Add retrain history
            retrain_history = original_model.get('retrainHistory', [])
            retrain_history.append({
                'jobId': job_id,
                'date': datetime.now().isoformat(),
                'mode': job.get('retrainMode'),
                'epochs': job.get('geneticConfig', {}).get('trainingEpochs', 10)
            })
            original_model['retrainHistory'] = retrain_history

            logger.info(f"Updated original model {source_model_id} with retrain results from job {job_id}")

            return {
                "success": True,
                "modelId": source_model_id,
                "message": f"Updated original model with new training results"
            }

        else:
            # Save as new model
            import uuid
            model_id = f"mdl-{uuid.uuid4().hex[:8]}"
            model_type = best_model['model_type'].upper()

            if request.newModelName:
                model_name = request.newModelName
            else:
                model_name = f"{model_type}_Retrain_{job_id[:8]}"

            # Get dataset info
            dataset_id = job.get('datasetId')

            model_entry = {
                "id": model_id,
                "name": model_name,
                "modelType": model_type,
                "datasetId": dataset_id,
                "jobId": job_id,
                "status": "trained",
                "hyperparameters": best_model['params'],
                "trainingHistory": best_model.get('training_history', []),
                "performanceMetrics": {
                    "accuracy": best_model['metrics'].get('accuracy', 0),
                    "precision": best_model['metrics'].get('precision', 0),
                    "recall": best_model['metrics'].get('recall', 0),
                    "f1Score": best_model['metrics'].get('f1_score', 0),
                    "auc": best_model['metrics'].get('auc_roc', 0),
                },
                "createdAt": datetime.now().isoformat(),
                "trainedAt": datetime.now().isoformat(),
                "filePath": best_model['file_path'],
                "generations": 1,
                "bestGeneration": 1,  # Retrain jobs are single-generation
                "fitness": best_model['fitness'],
                "confusionMatrix": best_model['metrics'].get('confusion_matrix'),
                "allMetrics": best_model['metrics'],
                "allParams": best_model['params'],
                "trainingDateRange": job.get('trainingDateRange'),
                # Source model reference
                "sourceModelId": source_model_id,
                "retrainMode": job.get('retrainMode'),
                # Normalization params for inference
                "normalizationParams": best_model.get('normalization_params') or job.get('normalizationParams'),
                # Classification training params
                "lossFunction": best_model.get('loss_function'),
                "threshold": best_model.get('threshold', 0.5)
            }

            # Save to database
            from app.api.models import save_model_to_db
            save_model_to_db(model_entry, db)

            # Also keep in memory for backward compatibility
            models_store[model_id] = model_entry

            logger.info(f"Saved retrain results as new model {model_id}")

            return {
                "success": True,
                "modelId": model_id,
                "modelName": model_name,
                "message": f"Saved as new model: {model_name}"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save retrain results: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


# ============================================================================
# Dataset Download Endpoints
# ============================================================================

@router.get("/jobs/{job_id}/datasets")
async def get_job_datasets(job_id: str):
    """
    Get information about cached datasets for a training job.

    Returns list of available dataset files with their sizes.
    """
    from app.services.job_handler import get_job_datasets as get_datasets

    datasets = get_datasets(job_id)

    if not datasets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cached datasets found for job {job_id}"
        )

    return datasets


@router.get("/jobs/{job_id}/datasets/{filename}")
async def download_job_dataset(job_id: str, filename: str):
    """
    Download a specific dataset file for a training job.

    Available files:
    - combined_dataset.csv: Full dataset with all features and targets (RNN format with shifted columns)
    - train_rnn.csv: Training data for LSTM/GRU (multiple shifted target columns)
    - test_rnn.csv: Test data for LSTM/GRU
    - train_multistep.csv: Training data for NBEATS/TCN/Transformer (single target)
    - test_multistep.csv: Test data for multi-step models
    - metadata.json: Dataset metadata including column info
    """
    from app.services.job_handler import get_dataset_file_path

    # Validate filename to prevent path traversal
    allowed_files = [
        'combined_dataset.csv',
        'train_rnn.csv', 'test_rnn.csv',
        'train_multistep.csv', 'test_multistep.csv',
        'train_dataset.csv', 'test_dataset.csv',  # Legacy format
        'metadata.json'
    ]
    if filename not in allowed_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid filename. Allowed files: {', '.join(allowed_files)}"
        )

    file_path = get_dataset_file_path(job_id, filename)

    if not file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset file {filename} not found for job {job_id}"
        )

    # Determine content type
    content_type = 'text/csv' if filename.endswith('.csv') else 'application/json'

    def file_iterator():
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                yield chunk

    return StreamingResponse(
        file_iterator(),
        media_type=content_type,
        headers={
            'Content-Disposition': f'attachment; filename="{job_id}_{filename}"'
        }
    )
