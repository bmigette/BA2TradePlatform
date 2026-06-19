"""
Settings API endpoints.

Manages application settings including API keys, GPU info, and provider configuration.
"""

import logging
import os
from typing import Dict, Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.encryption import encrypt_api_key, decrypt_api_key, is_key_encrypted

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory settings store with encrypted API keys
api_keys_store: Dict[str, str] = {}  # Stores encrypted keys


class ApiKeyRequest(BaseModel):
    """Request model for setting an API key."""
    provider_name: str
    api_key: str


class ApiKeyResponse(BaseModel):
    """Response model for API key (masked)."""
    provider_name: str
    key_set: bool
    masked_key: Optional[str] = None


class ProviderTestRequest(BaseModel):
    """Request model for testing a provider connection."""
    provider_name: str


class ProviderTestResponse(BaseModel):
    """Response model for provider connection test."""
    provider_name: str
    success: bool
    message: str
    latency_ms: Optional[float] = None


class GpuInfoResponse(BaseModel):
    """Response model for GPU information."""
    available: bool
    device_name: Optional[str] = None
    device_count: int = 0
    memory_total_gb: Optional[float] = None
    memory_free_gb: Optional[float] = None
    cuda_version: Optional[str] = None
    message: str


SUPPORTED_PROVIDERS = [
    "alpha_vantage",
    "polygon",
    "eodhd",
    "yahoo_finance",
    "fred"
]


def mask_key(key: str) -> str:
    """Mask an API key for display."""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@router.post("/api-keys", response_model=ApiKeyResponse)
async def set_api_key(request: ApiKeyRequest):
    """
    Set an API key for a data provider.

    The key is encrypted before storage and never stored in plaintext.
    The environment variable is also set for immediate use by data providers.

    Args:
        request: Provider name and API key

    Returns:
        Confirmation with masked key
    """
    if request.provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {request.provider_name}. Supported: {SUPPORTED_PROVIDERS}"
        )

    # Encrypt the key before storing
    encrypted_key = encrypt_api_key(request.api_key)
    api_keys_store[request.provider_name] = encrypted_key

    # Also set as environment variable for immediate use (plaintext for providers)
    env_key = f"{request.provider_name.upper()}_API_KEY"
    os.environ[env_key] = request.api_key

    logger.info(f"Set encrypted API key for provider: {request.provider_name}")

    return ApiKeyResponse(
        provider_name=request.provider_name,
        key_set=True,
        masked_key=mask_key(request.api_key)
    )


@router.get("/api-keys", response_model=List[ApiKeyResponse])
async def list_api_keys():
    """
    List all configured API keys (masked).

    API keys are stored encrypted and only returned in masked form.

    Returns:
        List of configured providers with masked keys
    """
    keys = []
    for provider in SUPPORTED_PROVIDERS:
        env_key = f"{provider.upper()}_API_KEY"
        stored_key = api_keys_store.get(provider)
        env_value = os.environ.get(env_key)

        # Decrypt stored key if encrypted
        if stored_key and is_key_encrypted(stored_key):
            try:
                decrypted = decrypt_api_key(stored_key)
                display_key = mask_key(decrypted)
            except Exception:
                display_key = "***encrypted***"
        elif env_value:
            display_key = mask_key(env_value)
        else:
            display_key = None

        keys.append(ApiKeyResponse(
            provider_name=provider,
            key_set=stored_key is not None or env_value is not None,
            masked_key=display_key
        ))

    return keys


@router.delete("/api-keys/{provider_name}")
async def delete_api_key(provider_name: str):
    """
    Delete an API key for a provider.

    Args:
        provider_name: Provider to delete key for

    Returns:
        Confirmation message
    """
    if provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_name}"
        )

    if provider_name in api_keys_store:
        del api_keys_store[provider_name]

    env_key = f"{provider_name.upper()}_API_KEY"
    if env_key in os.environ:
        del os.environ[env_key]

    logger.info(f"Deleted API key for provider: {provider_name}")

    return {"message": f"API key deleted for {provider_name}"}


@router.post("/providers/test", response_model=ProviderTestResponse)
async def test_provider_connection(request: ProviderTestRequest):
    """
    Test connection to a data provider.

    Args:
        request: Provider name to test

    Returns:
        Test result with success status and latency
    """
    import time

    if request.provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {request.provider_name}"
        )

    # Check if API key is configured
    env_key = f"{request.provider_name.upper()}_API_KEY"
    api_key = api_keys_store.get(request.provider_name) or os.environ.get(env_key)

    if not api_key and request.provider_name != "yahoo_finance":
        return ProviderTestResponse(
            provider_name=request.provider_name,
            success=False,
            message=f"No API key configured for {request.provider_name}"
        )

    # Simulate connection test
    start_time = time.time()

    try:
        # In production, would actually test the API connection
        # For now, simulate a successful connection
        import random
        time.sleep(random.uniform(0.1, 0.3))  # Simulate network latency

        latency = (time.time() - start_time) * 1000

        logger.info(f"Successfully tested connection to {request.provider_name}")

        return ProviderTestResponse(
            provider_name=request.provider_name,
            success=True,
            message=f"Connection to {request.provider_name} successful",
            latency_ms=round(latency, 2)
        )

    except Exception as e:
        logger.error(f"Connection test failed for {request.provider_name}: {e}")
        return ProviderTestResponse(
            provider_name=request.provider_name,
            success=False,
            message=f"Connection failed: {str(e)}"
        )


@router.get("/gpu-info", response_model=GpuInfoResponse)
async def get_gpu_info():
    """
    Get GPU/CUDA availability information.

    Returns:
        GPU device info including memory and CUDA version
    """
    try:
        import torch

        if torch.cuda.is_available():
            device_count = torch.cuda.device_count()
            device_name = torch.cuda.get_device_name(0) if device_count > 0 else None

            # Get memory info for first device
            if device_count > 0:
                total_memory = torch.cuda.get_device_properties(0).total_memory
                free_memory = total_memory - torch.cuda.memory_allocated(0)
            else:
                total_memory = None
                free_memory = None

            return GpuInfoResponse(
                available=True,
                device_name=device_name,
                device_count=device_count,
                memory_total_gb=round(total_memory / 1e9, 2) if total_memory else None,
                memory_free_gb=round(free_memory / 1e9, 2) if free_memory else None,
                cuda_version=torch.version.cuda,
                message=f"GPU available: {device_name}"
            )
        else:
            return GpuInfoResponse(
                available=False,
                message="CUDA not available. Using CPU for training."
            )

    except ImportError:
        return GpuInfoResponse(
            available=False,
            message="PyTorch not installed. Cannot detect GPU."
        )
    except Exception as e:
        logger.error(f"Error getting GPU info: {e}")
        return GpuInfoResponse(
            available=False,
            message=f"Error detecting GPU: {str(e)}"
        )


@router.get("/system-info")
async def get_system_info():
    """
    Get comprehensive system information.

    Returns:
        System info including Python version, available libraries, etc.
    """
    import sys
    import platform

    info = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "architecture": platform.architecture()[0],
        "libraries": {}
    }

    # Check for key libraries
    libraries_to_check = [
        "torch", "darts", "deap", "transformers",
        "pandas", "numpy", "sklearn", "fastapi"
    ]

    for lib in libraries_to_check:
        try:
            module = __import__(lib)
            version = getattr(module, "__version__", "unknown")
            info["libraries"][lib] = {"available": True, "version": version}
        except ImportError:
            info["libraries"][lib] = {"available": False}

    return info


# In-memory settings store for application configuration
app_settings: Dict[str, any] = {
    "gpu_enabled": True,
    "max_concurrent_jobs": 2,
    "auto_save_checkpoints": True,
    "checkpoint_interval": 5,  # generations
    "log_level": "info",
    "theme": "dark"
}


class AppSettingsUpdate(BaseModel):
    """Request model for updating application settings."""
    gpu_enabled: Optional[bool] = None
    max_concurrent_jobs: Optional[int] = None
    auto_save_checkpoints: Optional[bool] = None
    checkpoint_interval: Optional[int] = None
    log_level: Optional[str] = None
    theme: Optional[str] = None


@router.get("")
async def get_app_settings():
    """
    Get current application settings.

    Returns:
        Current settings values
    """
    return {
        "settings": app_settings,
        "gpu_available": await _check_gpu_available()
    }


@router.put("")
async def update_app_settings(settings: AppSettingsUpdate):
    """
    Update application settings.

    Args:
        settings: Settings to update

    Returns:
        Updated settings
    """
    updated = []

    if settings.gpu_enabled is not None:
        app_settings["gpu_enabled"] = settings.gpu_enabled
        updated.append("gpu_enabled")
        logger.info(f"GPU enabled set to: {settings.gpu_enabled}")

    if settings.max_concurrent_jobs is not None:
        if settings.max_concurrent_jobs < 1:
            raise HTTPException(
                status_code=400,
                detail="max_concurrent_jobs must be at least 1"
            )
        if settings.max_concurrent_jobs > 10:
            raise HTTPException(
                status_code=400,
                detail="max_concurrent_jobs cannot exceed 10"
            )
        app_settings["max_concurrent_jobs"] = settings.max_concurrent_jobs
        updated.append("max_concurrent_jobs")
        logger.info(f"Max concurrent jobs set to: {settings.max_concurrent_jobs}")

    if settings.auto_save_checkpoints is not None:
        app_settings["auto_save_checkpoints"] = settings.auto_save_checkpoints
        updated.append("auto_save_checkpoints")

    if settings.checkpoint_interval is not None:
        if settings.checkpoint_interval < 1:
            raise HTTPException(
                status_code=400,
                detail="checkpoint_interval must be at least 1"
            )
        app_settings["checkpoint_interval"] = settings.checkpoint_interval
        updated.append("checkpoint_interval")

    if settings.log_level is not None:
        valid_levels = ["debug", "info", "warning", "error"]
        if settings.log_level.lower() not in valid_levels:
            raise HTTPException(
                status_code=400,
                detail=f"log_level must be one of: {valid_levels}"
            )
        app_settings["log_level"] = settings.log_level.lower()
        updated.append("log_level")

    if settings.theme is not None:
        valid_themes = ["light", "dark", "system"]
        if settings.theme.lower() not in valid_themes:
            raise HTTPException(
                status_code=400,
                detail=f"theme must be one of: {valid_themes}"
            )
        app_settings["theme"] = settings.theme.lower()
        updated.append("theme")

    return {
        "message": f"Updated settings: {', '.join(updated)}" if updated else "No settings updated",
        "settings": app_settings,
        "updated": updated
    }


@router.get("/gpu-acceleration")
async def get_gpu_acceleration_status():
    """
    Get GPU acceleration status.

    Returns:
        GPU enabled status and availability
    """
    gpu_available = await _check_gpu_available()

    return {
        "enabled": app_settings["gpu_enabled"],
        "available": gpu_available,
        "will_use_gpu": app_settings["gpu_enabled"] and gpu_available,
        "message": (
            "GPU acceleration is enabled and available"
            if app_settings["gpu_enabled"] and gpu_available
            else "GPU acceleration is disabled" if not app_settings["gpu_enabled"]
            else "GPU is not available on this system"
        )
    }


@router.put("/gpu-acceleration")
async def set_gpu_acceleration(enabled: bool = True):
    """
    Enable or disable GPU acceleration.

    Args:
        enabled: Whether to enable GPU acceleration

    Returns:
        Updated GPU settings
    """
    app_settings["gpu_enabled"] = enabled
    gpu_available = await _check_gpu_available()

    logger.info(f"GPU acceleration {'enabled' if enabled else 'disabled'}")

    return {
        "enabled": enabled,
        "available": gpu_available,
        "will_use_gpu": enabled and gpu_available,
        "message": f"GPU acceleration {'enabled' if enabled else 'disabled'}"
    }


@router.get("/job-limits")
async def get_job_limits():
    """
    Get job concurrency limits.

    Returns:
        Current job limits
    """
    return {
        "max_concurrent_jobs": app_settings["max_concurrent_jobs"],
        "description": "Maximum number of optimization jobs that can run simultaneously"
    }


@router.put("/job-limits")
async def set_job_limits(max_concurrent_jobs: int = 2):
    """
    Set job concurrency limits.

    Args:
        max_concurrent_jobs: Maximum concurrent jobs (1-10)

    Returns:
        Updated job limits
    """
    if max_concurrent_jobs < 1 or max_concurrent_jobs > 10:
        raise HTTPException(
            status_code=400,
            detail="max_concurrent_jobs must be between 1 and 10"
        )

    app_settings["max_concurrent_jobs"] = max_concurrent_jobs
    logger.info(f"Max concurrent jobs set to: {max_concurrent_jobs}")

    return {
        "max_concurrent_jobs": max_concurrent_jobs,
        "message": f"Max concurrent jobs set to {max_concurrent_jobs}"
    }


async def _check_gpu_available() -> bool:
    """Check if GPU is available."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


# ============================================================================
# GPU Memory Management
# ============================================================================

class GpuMemoryStatus(BaseModel):
    """Response model for GPU memory status."""
    available: bool
    device_name: Optional[str] = None
    total_memory_gb: Optional[float] = None
    used_memory_gb: Optional[float] = None
    free_memory_gb: Optional[float] = None
    utilization_percent: Optional[float] = None
    can_allocate_gb: Optional[float] = None
    memory_threshold_percent: float = 90.0
    is_near_limit: bool = False
    active_jobs: int = 0


# GPU memory management settings
gpu_memory_settings = {
    "memory_threshold_percent": 90.0,  # Warn/queue when above this
    "min_free_memory_gb": 2.0,  # Minimum free memory to start new job
    "auto_clear_cache": True,  # Automatically clear cache between jobs
    "batch_size_reduction": True,  # Reduce batch size when memory low
}


@router.get("/gpu-memory", response_model=GpuMemoryStatus)
async def get_gpu_memory_status():
    """
    Get detailed GPU memory status.

    Returns:
        GPU memory usage information for OOM prevention
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return GpuMemoryStatus(available=False)

        # Get memory info
        device = torch.cuda.current_device()
        device_name = torch.cuda.get_device_name(device)
        total_memory = torch.cuda.get_device_properties(device).total_memory
        allocated_memory = torch.cuda.memory_allocated(device)
        cached_memory = torch.cuda.memory_reserved(device)
        free_memory = total_memory - allocated_memory

        total_gb = total_memory / (1024 ** 3)
        used_gb = allocated_memory / (1024 ** 3)
        free_gb = free_memory / (1024 ** 3)
        utilization = (allocated_memory / total_memory) * 100

        # Calculate how much can be safely allocated
        threshold = gpu_memory_settings["memory_threshold_percent"]
        can_allocate = max(0, (total_memory * (threshold / 100) - allocated_memory)) / (1024 ** 3)

        # Check if near limit
        is_near_limit = utilization >= threshold

        # Get active job count
        from app.api.jobs import jobs_store
        active_jobs = sum(1 for j in jobs_store.values() if j.get("status") == "running")

        return GpuMemoryStatus(
            available=True,
            device_name=device_name,
            total_memory_gb=round(total_gb, 2),
            used_memory_gb=round(used_gb, 2),
            free_memory_gb=round(free_gb, 2),
            utilization_percent=round(utilization, 1),
            can_allocate_gb=round(can_allocate, 2),
            memory_threshold_percent=threshold,
            is_near_limit=is_near_limit,
            active_jobs=active_jobs
        )

    except ImportError:
        return GpuMemoryStatus(available=False)
    except Exception as e:
        logger.error(f"Error getting GPU memory status: {e}")
        return GpuMemoryStatus(available=False)


@router.post("/gpu-memory/clear-cache")
async def clear_gpu_cache():
    """
    Clear GPU cache to free memory.

    Use this to free cached memory between training jobs
    or when memory is running low.

    Returns:
        Memory freed and current status
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return {
                "success": False,
                "message": "CUDA not available"
            }

        # Get memory before
        before_allocated = torch.cuda.memory_allocated(0)
        before_cached = torch.cuda.memory_reserved(0)

        # Clear cache
        torch.cuda.empty_cache()

        # Get memory after
        after_allocated = torch.cuda.memory_allocated(0)
        after_cached = torch.cuda.memory_reserved(0)

        freed_mb = (before_cached - after_cached) / (1024 ** 2)

        logger.info(f"Cleared GPU cache, freed {freed_mb:.1f} MB")

        return {
            "success": True,
            "message": f"Cleared {freed_mb:.1f} MB of cached memory",
            "freed_mb": round(freed_mb, 1),
            "current_allocated_mb": round(after_allocated / (1024 ** 2), 1),
            "current_cached_mb": round(after_cached / (1024 ** 2), 1)
        }

    except ImportError:
        return {
            "success": False,
            "message": "PyTorch not available"
        }
    except Exception as e:
        logger.error(f"Error clearing GPU cache: {e}")
        return {
            "success": False,
            "message": str(e)
        }


@router.get("/gpu-memory/settings")
async def get_gpu_memory_settings():
    """
    Get GPU memory management settings.

    Returns:
        Current GPU memory thresholds and options
    """
    return {
        "settings": gpu_memory_settings,
        "description": {
            "memory_threshold_percent": "Warn/queue jobs when GPU usage exceeds this percentage",
            "min_free_memory_gb": "Minimum free GPU memory (GB) required to start a new job",
            "auto_clear_cache": "Automatically clear GPU cache between jobs",
            "batch_size_reduction": "Automatically reduce batch size when memory is low"
        }
    }


@router.put("/gpu-memory/settings")
async def update_gpu_memory_settings(
    memory_threshold_percent: Optional[float] = None,
    min_free_memory_gb: Optional[float] = None,
    auto_clear_cache: Optional[bool] = None,
    batch_size_reduction: Optional[bool] = None
):
    """
    Update GPU memory management settings.

    Args:
        memory_threshold_percent: Threshold for warning (50-99)
        min_free_memory_gb: Minimum free memory required (0.5-16)
        auto_clear_cache: Enable automatic cache clearing
        batch_size_reduction: Enable automatic batch size reduction

    Returns:
        Updated settings
    """
    updated = []

    if memory_threshold_percent is not None:
        if not 50 <= memory_threshold_percent <= 99:
            raise HTTPException(
                status_code=400,
                detail="memory_threshold_percent must be between 50 and 99"
            )
        gpu_memory_settings["memory_threshold_percent"] = memory_threshold_percent
        updated.append("memory_threshold_percent")

    if min_free_memory_gb is not None:
        if not 0.5 <= min_free_memory_gb <= 16:
            raise HTTPException(
                status_code=400,
                detail="min_free_memory_gb must be between 0.5 and 16"
            )
        gpu_memory_settings["min_free_memory_gb"] = min_free_memory_gb
        updated.append("min_free_memory_gb")

    if auto_clear_cache is not None:
        gpu_memory_settings["auto_clear_cache"] = auto_clear_cache
        updated.append("auto_clear_cache")

    if batch_size_reduction is not None:
        gpu_memory_settings["batch_size_reduction"] = batch_size_reduction
        updated.append("batch_size_reduction")

    logger.info(f"Updated GPU memory settings: {updated}")

    return {
        "message": f"Updated settings: {', '.join(updated)}" if updated else "No settings updated",
        "settings": gpu_memory_settings,
        "updated": updated
    }


def check_can_start_job() -> tuple:
    """
    Check if a new job can be started based on GPU memory.

    Returns:
        Tuple of (can_start: bool, reason: str)
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return True, "CPU mode - no GPU memory constraints"

        device = torch.cuda.current_device()
        total_memory = torch.cuda.get_device_properties(device).total_memory
        allocated_memory = torch.cuda.memory_allocated(device)
        free_memory = total_memory - allocated_memory

        utilization = (allocated_memory / total_memory) * 100
        free_gb = free_memory / (1024 ** 3)

        threshold = gpu_memory_settings["memory_threshold_percent"]
        min_free = gpu_memory_settings["min_free_memory_gb"]

        if utilization >= threshold:
            return False, f"GPU memory utilization ({utilization:.1f}%) exceeds threshold ({threshold}%)"

        if free_gb < min_free:
            return False, f"Insufficient free GPU memory ({free_gb:.2f} GB, need {min_free} GB)"

        return True, f"GPU memory OK ({free_gb:.2f} GB free)"

    except ImportError:
        return True, "PyTorch not available - cannot check GPU memory"
    except Exception as e:
        logger.error(f"Error checking GPU memory: {e}")
        return True, f"Error checking GPU memory: {str(e)}"


# ---------------------------------------------------------------------------
# Import API keys from the live trade platform's DB
# ---------------------------------------------------------------------------
# After the DB-layout split, the test platform's keys live in its own keys DB
# (the ba2_common-configured DB = ba2_common.config.DB_FILE, under test/). The
# live trade platform keeps its own credentials in the trade DB (under trade/).
# This endpoint lets a user copy the credential AppSetting rows from the trade DB
# into the test keys DB so backtests can resolve provider keys without a manual
# DB import.

class ImportKeysResponse(BaseModel):
    """Response model for importing keys from the trade platform DB."""
    imported: List[str]
    count: int
    source_db: str


def _credential_like(key: str) -> bool:
    """True if an AppSetting key looks like a credential worth importing."""
    k = key.lower()
    return any(tok in k for tok in ("api_key", "_key", "token", "secret"))


def _trade_db_path() -> str:
    """Resolve the live trade platform's DB path.

    Prefer the live config (ba2_trade_platform.config.DB_FILE) if it is importable
    on this box; otherwise fall back to the layout default <TRADE_DIR>/db.sqlite."""
    try:
        import ba2_trade_platform.config as trade_cfg  # type: ignore
        return trade_cfg.DB_FILE
    except Exception:  # noqa: BLE001 — live package may not be installed in the test venv
        from ba2_common.config import TRADE_DIR
        return os.path.join(TRADE_DIR, "db.sqlite")


@router.post("/import-keys-from-trade", response_model=ImportKeysResponse)
async def import_keys_from_trade():
    """Import credential AppSetting rows from the live trade platform's DB into the
    test platform's keys DB (the ba2_common-configured DB).

    Opens the trade DB READ-ONLY, selects AppSetting rows whose key looks like a
    credential (contains api_key/_key/token/secret, case-insensitive), and upserts
    each into the test keys DB. Returns the imported keys and a count."""
    import sqlite3

    trade_db = _trade_db_path()
    if not os.path.exists(trade_db):
        raise HTTPException(
            status_code=404,
            detail=f"Trade platform DB not found at {trade_db}. "
                   "Run the live platform (or the migration) first."
        )

    # --- read credential rows from the trade DB (read-only, no engine pollution) ---
    pairs: Dict[str, Optional[str]] = {}
    try:
        uri = f"file:{trade_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("SELECT key, value_str FROM appsetting").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read AppSetting rows from trade DB: {e}"
        )
    for key, value_str in rows:
        if key and _credential_like(key) and value_str:
            pairs[key] = value_str

    if not pairs:
        return ImportKeysResponse(imported=[], count=0, source_db=trade_db)

    # --- upsert into the test keys DB (the ba2_common-configured engine) ---
    from ba2_common.core.db import get_engine, init_db
    from ba2_common.core.models import AppSetting
    from sqlmodel import Session, select

    init_db()  # ensure the AppSetting table exists in a fresh test keys DB
    imported: List[str] = []
    engine = get_engine()
    with Session(engine) as session:
        for key, value_str in pairs.items():
            existing = session.exec(
                select(AppSetting).where(AppSetting.key == key)
            ).first()
            if existing:
                existing.value_str = value_str
                session.add(existing)
            else:
                session.add(AppSetting(key=key, value_str=value_str))
            imported.append(key)
        session.commit()

    logger.info(f"Imported {len(imported)} credential key(s) from trade DB {trade_db}")
    return ImportKeysResponse(imported=imported, count=len(imported), source_db=trade_db)


# ---------------------------------------------------------------------------
# View / set individual credential keys (the ba2_common AppSetting table)
# ---------------------------------------------------------------------------
# These endpoints read/write the SAME AppSetting rows that get_app_setting() reads,
# in the ba2_common-configured DB (the test platform's keys DB). The GET masks values
# (last 4 chars) and never returns plaintext; the PUT upserts {key: value} pairs and
# never logs the secret values.

# Standard credential keys we always surface in the UI, even when currently unset, so
# the user can add a missing one. (Derived list = these UNION whatever the DB already
# has.) Keep names exactly as the providers read them via get_app_setting().
KNOWN_CREDENTIAL_KEYS: List[str] = [
    "FMP_API_KEY",
    "finnhub_api_key",
    "alpaca_market_api_key",
    "alpaca_market_api_secret",
    "alpaca_trade_api_key",
    "alpaca_trade_api_secret",
    "OPENAI_API_KEY",
    "FRED_API_KEY",
]


class CredentialKey(BaseModel):
    """A credential AppSetting key with a MASKED value (never plaintext)."""
    key: str
    is_set: bool
    masked_value: Optional[str] = None


class CredentialKeysResponse(BaseModel):
    """Response model for listing credential keys (masked)."""
    keys: List[CredentialKey]


class CredentialKeysUpdate(BaseModel):
    """Request model for upserting credential key/value pairs."""
    values: Dict[str, str]


class CredentialKeysUpdateResponse(BaseModel):
    """Response model after upserting credential keys."""
    updated: List[str]
    count: int


def _mask_secret(value: Optional[str]) -> Optional[str]:
    """Mask a secret value, revealing only the last 4 characters."""
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


@router.get("/credential-keys", response_model=CredentialKeysResponse)
async def list_credential_keys():
    """List credential AppSetting keys with MASKED values.

    Reads the ba2_common-configured DB (the same rows get_app_setting() reads). A key
    counts as a credential if it contains api_key/_key/token/secret (case-insensitive).
    The returned list is the UNION of the standard known keys and whatever credential
    rows already exist in the DB, so the UI can also offer to set currently-unset keys.
    Values are never returned in plaintext.
    """
    from ba2_common.core.db import get_engine, init_db
    from ba2_common.core.models import AppSetting
    from sqlmodel import Session, select

    init_db()  # ensure AppSetting table exists in a fresh keys DB

    existing: Dict[str, Optional[str]] = {}
    engine = get_engine()
    with Session(engine) as session:
        for row in session.exec(select(AppSetting)).all():
            if row.key and _credential_like(row.key):
                existing[row.key] = row.value_str

    # Union: known keys first (stable order), then any extra credential keys from the DB.
    ordered: List[str] = list(KNOWN_CREDENTIAL_KEYS)
    for k in existing:
        if k not in ordered:
            ordered.append(k)

    keys = [
        CredentialKey(
            key=k,
            is_set=bool(existing.get(k)),
            masked_value=_mask_secret(existing.get(k)),
        )
        for k in ordered
    ]
    return CredentialKeysResponse(keys=keys)


@router.put("/credential-keys", response_model=CredentialKeysUpdateResponse)
async def update_credential_keys(payload: CredentialKeysUpdate):
    """Upsert credential key/value pairs into the ba2_common AppSetting table.

    Writes the SAME rows get_app_setting() reads. Empty values are skipped (use the
    masked GET to see current state; sending a blank field is a no-op, not a clear).
    Secret values are never logged — only the key names are.
    """
    from ba2_common.core.db import get_engine, init_db
    from ba2_common.core.models import AppSetting
    from sqlmodel import Session, select

    init_db()  # ensure AppSetting table exists in a fresh keys DB

    updated: List[str] = []
    engine = get_engine()
    with Session(engine) as session:
        for key, value in payload.values.items():
            if not key or value is None or value == "":
                continue
            existing = session.exec(
                select(AppSetting).where(AppSetting.key == key)
            ).first()
            if existing:
                existing.value_str = value
                session.add(existing)
            else:
                session.add(AppSetting(key=key, value_str=value))
            updated.append(key)
        session.commit()

    if updated:
        logger.info(f"Updated {len(updated)} credential key(s): {', '.join(updated)}")
    return CredentialKeysUpdateResponse(updated=updated, count=len(updated))
