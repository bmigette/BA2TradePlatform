"""Market analyses and expert recommendations for the management API (reads)."""
import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ..core.types import AnalysisUseCase, MarketAnalysisStatus
from ._common import jsonable, naive_utc, page, parse_enum, require_row, row_dict
from ._scrub import scrub

#: Largest analysis ``state`` (serialised JSON, in characters) the detail endpoint returns; beyond it the state is
#: cut and ``state_truncated`` is true.
MAX_STATE_BYTES = 1_000_000


def _skip_reason(state: Dict[str, Any]) -> Optional[str]:
    return state["skip_reason"] if "skip_reason" in state else None


def _analysis_summary(a: MarketAnalysis) -> Dict[str, Any]:
    out = row_dict(a)
    state = out.pop("state")                 # list rows stay small; the full state is on the detail
    out["skip_reason"] = _skip_reason(state)
    return out


def list_analyses(*, expert: Optional[int], symbol: Optional[str], status: Optional[str],
                  subtype: Optional[str], from_: Optional[datetime], limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(MarketAnalysis)
    if expert is not None:
        stmt = stmt.where(MarketAnalysis.expert_instance_id == expert)
    if symbol is not None:
        stmt = stmt.where(MarketAnalysis.symbol == symbol.upper())
    if status is not None:
        stmt = stmt.where(MarketAnalysis.status == parse_enum(MarketAnalysisStatus, status, "status"))
    if subtype is not None:
        stmt = stmt.where(MarketAnalysis.subtype == parse_enum(AnalysisUseCase, subtype, "subtype"))
    if from_ is not None:
        stmt = stmt.where(MarketAnalysis.created_at >= naive_utc(from_))
    with get_db() as session:
        return page(session, stmt.order_by(MarketAnalysis.id.desc()), limit=limit, offset=offset,
                    convert=_analysis_summary)


def _output_dict(o: AnalysisOutput, include_text: bool) -> Dict[str, Any]:
    d = jsonable(o.model_dump(exclude={"blob", "text"}))     # never ship the binary blob
    d["text_length"] = 0 if o.text is None else len(o.text)
    if include_text:
        d["text"] = o.text
    return d


def analysis_detail(analysis_id: int, include_text: bool, include_state: bool = False) -> Dict[str, Any]:
    with get_db() as session:
        a = require_row(session, MarketAnalysis, analysis_id, "analysis")
        out = row_dict(a)
        state = out.pop("state")
        out["skip_reason"] = _skip_reason(state)
        out["state_included"] = include_state
        if include_state:
            # scrub BEFORE cutting: a cut can slice a credential in half and leave a prefix no pattern matches
            text = json.dumps(scrub(state), ensure_ascii=False, default=str)
            if len(text) > MAX_STATE_BYTES:
                out["state"], out["state_truncated"] = text[:MAX_STATE_BYTES], True
            else:
                out["state"], out["state_truncated"] = json.loads(text), False
        outputs = session.exec(select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis_id)
                               .order_by(AnalysisOutput.id)).all()
        recs = session.exec(select(ExpertRecommendation)
                            .where(ExpertRecommendation.market_analysis_id == analysis_id)
                            .order_by(ExpertRecommendation.id)).all()
        out["outputs"] = [_output_dict(o, include_text) for o in outputs]
        out["recommendations"] = [row_dict(r) for r in recs]
    return out


def list_recommendations(*, expert: Optional[int], symbol: Optional[str], from_: Optional[datetime],
                         limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(ExpertRecommendation)
    if expert is not None:
        stmt = stmt.where(ExpertRecommendation.instance_id == expert)
    if symbol is not None:
        stmt = stmt.where(ExpertRecommendation.symbol == symbol.upper())
    if from_ is not None:
        stmt = stmt.where(ExpertRecommendation.created_at >= naive_utc(from_))
    with get_db() as session:
        return page(session, stmt.order_by(ExpertRecommendation.id.desc()), limit=limit, offset=offset,
                    convert=row_dict)
