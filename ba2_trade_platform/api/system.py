"""Liveness / version probe: the smallest read, used by tools/api_smoke.py and the MCP client."""
from fastapi import APIRouter

from ..version import APP_VERSION

router = APIRouter(tags=["system"])


@router.get("/ping")
def ping():
    return {"status": "ok", "app_version": APP_VERSION}
