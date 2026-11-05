"""Typed service errors and the shared serialisation / paging helpers of the management API."""
from datetime import date, datetime, timezone
from enum import Enum

import pytest
from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.types import TransactionStatus
from ba2_trade_platform.services import errors
from ba2_trade_platform.services._common import (
    jsonable, page, parse_enum, require_row, row_dict,
)
from tests.factories import create_account_definition


pytestmark = pytest.mark.api_db


def test_error_classes_carry_code_and_status():
    cases = {
        errors.NotFound: (404, "not_found"),
        errors.Conflict: (409, "conflict"),
        errors.Invalid: (422, "invalid"),
        errors.BrokerRefused: (502, "broker_refused"),
        errors.BrokerUnreachable: (503, "broker_unreachable"),
        errors.Unmeasurable: (503, "unmeasurable"),
    }
    for cls, (status, code) in cases.items():
        e = cls("boom", {"k": 1})
        assert (e.status_code, e.code, e.message, e.detail) == (status, code, "boom", {"k": 1})
    assert errors.NotFound("x").detail == {}


def test_jsonable_converts_enums_dates_and_nested():
    class Color(str, Enum):
        RED = "red"
    out = jsonable({"c": Color.RED, "d": date(2026, 1, 2),
                    "t": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
                    "l": [Color.RED, (1, 2)]})
    assert out == {"c": "red", "d": "2026-01-02", "t": "2026-01-02T03:04:00+00:00",
                   "l": ["red", [1, 2]]}


def test_jsonable_rejects_non_finite_floats_as_unmeasurable():
    with pytest.raises(errors.Unmeasurable):
        jsonable({"x": float("nan")})


def test_parse_enum_accepts_value_or_name_and_rejects_garbage():
    assert parse_enum(TransactionStatus, "OPENED", "status") is TransactionStatus.OPENED
    assert parse_enum(TransactionStatus, "opened", "status") is TransactionStatus.OPENED
    with pytest.raises(errors.Invalid) as ei:
        parse_enum(TransactionStatus, "nope", "status")
    assert ei.value.detail["field"] == "status"


def test_page_and_require_row():
    for i in range(5):
        create_account_definition(name=f"a{i}")
    with get_db() as s:
        out = page(s, select(AccountDefinition).order_by(AccountDefinition.id),
                   limit=2, offset=1, convert=row_dict)
        assert out["total"] == 5 and out["limit"] == 2 and out["offset"] == 1
        assert [r["name"] for r in out["items"]] == ["a1", "a2"]
        assert require_row(s, AccountDefinition, out["items"][0]["id"], "account").id
        with pytest.raises(errors.NotFound):
            require_row(s, AccountDefinition, 99999, "account")
