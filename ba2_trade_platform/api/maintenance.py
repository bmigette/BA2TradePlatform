"""The three pre-existing operations endpoints, under /api/v1. Their implementation stays in
``ui/api_routes.py`` (also served at the legacy /api/* paths); this router registers the SAME function
objects, so the two paths cannot drift."""
from fastapi import APIRouter

from ..ui import api_routes
from .audit import AuditedRoute

router = APIRouter(tags=["maintenance"], route_class=AuditedRoute)

router.add_api_route("/reload", api_routes.reload_from_db, methods=["POST"])
router.add_api_route("/run-schedule", api_routes.run_schedule_now, methods=["POST"])
router.add_api_route("/process-recommendations", api_routes.process_recommendations, methods=["POST"])
