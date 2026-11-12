"""Liveness / version probe: the smallest read, used by tools/api_smoke.py and the MCP client.

``/capture-health`` is the replay-capture ops read: whether the live decision recorder is on and every
counter of what it failed to record (the same counters ``capabilities.capture_health`` freezes into a
session at finalize). Same access rule as every other GET (app-wide ``require_access``).
"""
from fastapi import APIRouter

from ..version import APP_VERSION

router = APIRouter(tags=["system"])


@router.get("/ping")
def ping():
    return {"status": "ok", "app_version": APP_VERSION}


@router.get("/capture-health")
def capture_health():
    from ..core.replay_capture import get_capture_status

    return get_capture_status()
