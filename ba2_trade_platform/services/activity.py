"""Activity log reads for the management API."""
from datetime import datetime
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import ActivityLog
from ..core.types import ActivityLogSeverity, ActivityLogType
from ._common import naive_utc, page, parse_enum, row_dict


def list_activity(*, type_: Optional[str], severity: Optional[str], expert: Optional[int],
                  account: Optional[int], from_: Optional[datetime], limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(ActivityLog)
    if type_ is not None:
        stmt = stmt.where(ActivityLog.type == parse_enum(ActivityLogType, type_, "type"))
    if severity is not None:
        stmt = stmt.where(ActivityLog.severity == parse_enum(ActivityLogSeverity, severity, "severity"))
    if expert is not None:
        stmt = stmt.where(ActivityLog.source_expert_id == expert)
    if account is not None:
        stmt = stmt.where(ActivityLog.source_account_id == account)
    if from_ is not None:
        stmt = stmt.where(ActivityLog.created_at >= naive_utc(from_))
    with get_db() as session:
        return page(session, stmt.order_by(ActivityLog.id.desc()), limit=limit, offset=offset, convert=row_dict)
