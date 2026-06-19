"""Expert catalog API (Task 3).

Read-only endpoints the frontend uses to render the expert picker and, per
expert, its settings form:

  * ``GET /api/experts`` -> ``{experts: [...]}``
  * ``GET /api/experts/{class_name}/settings-definitions`` -> ``{class, definitions}``

Unknown class names return 404. The data is sourced entirely from the expert
classes via ``app.services.experts_catalog`` (no DB).
"""
import logging

from fastapi import APIRouter, HTTPException

from app.services import experts_catalog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/experts")


@router.get("")
def get_experts():
    return {"experts": experts_catalog.list_experts()}


@router.get("/{class_name}/settings-definitions")
def get_settings_definitions(class_name: str):
    try:
        definitions = experts_catalog.settings_definitions(class_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown expert class: {class_name}")
    return {"class": class_name, "definitions": definitions}
