"""Rules import/export endpoints.

Converts between the rules_export_import ruleset JSON (v1.1) and the Strategy
condition-tree shape used by the optimizer/param-space.

- POST /api/strategies/import-rules : ruleset JSON -> condition tree
- GET  /api/strategies/{id}/export-rules?which=enter|exit : Strategy -> ruleset JSON

exit_conditions is a LIST of exit-rule dicts (id/name/conditions/action/...), not a
single tree. For export we wrap it into an OR group whose branches are each rule's
`conditions` sub-tree, so tree_to_ruleset_json sees the same OR(rules)->AND(triggers)
shape it produces for the entry trees.
"""
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.models import get_db, Strategy
from app.services.rules_tree_json import ruleset_json_to_tree, tree_to_ruleset_json

logger = logging.getLogger(__name__)

# Same path as the strategies router; registered after it in main.py. The static
# segments (import-rules / export-rules) are matched before the /{strategy_id}
# catch-all because FastAPI tries this router's routes too and literal paths win.
router = APIRouter(prefix="/api/strategies", tags=["rules"])

_VALID_WHICH = {"enter", "exit"}


class ImportRulesRequest(BaseModel):
    # `json` is the request field name (per spec); aliased to avoid shadowing
    # BaseModel.json and silence the pydantic warning.
    model_config = ConfigDict(populate_by_name=True)
    payload: Dict[str, Any] = Field(alias="json")
    which: str


def _exit_rules_to_tree(exit_rules: Any) -> Dict[str, Any]:
    """Wrap a list of exit-rule dicts into an OR group of their condition sub-trees."""
    or_children = []
    for rule in (exit_rules or []):
        if not isinstance(rule, dict):
            continue
        conds = rule.get("conditions")
        if conds:
            or_children.append(conds)
    return {"id": "exit-root", "operator": "OR", "conditions": or_children}


@router.post("/import-rules")
async def import_rules(req: ImportRulesRequest):
    """Convert a v1.1 ruleset JSON into a Strategy condition tree."""
    if req.which not in _VALID_WHICH:
        raise HTTPException(status_code=422,
                            detail=f"which must be one of {sorted(_VALID_WHICH)}")
    try:
        tree = ruleset_json_to_tree(req.payload, req.which)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"tree": tree}


@router.get("/{strategy_id}/export-rules")
async def export_rules(
    strategy_id: int,
    which: str,
    db: Session = Depends(get_db),
):
    """Export a strategy's enter/exit rules as v1.1 ruleset JSON."""
    if which not in _VALID_WHICH:
        raise HTTPException(status_code=422,
                            detail=f"which must be one of {sorted(_VALID_WHICH)}")

    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    if which == "enter":
        tree = strategy.buy_entry_conditions
    else:
        # exit_conditions is a LIST of exit-rule dicts -> wrap into an OR tree.
        tree = _exit_rules_to_tree(strategy.exit_conditions)

    # Empty/None tree -> still produce a valid (empty-rules) ruleset JSON.
    if not tree:
        tree = {"id": "empty", "operator": "OR", "conditions": []}

    return tree_to_ruleset_json(tree, which, name=strategy.name)
