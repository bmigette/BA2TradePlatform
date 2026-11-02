"""Builders shared by the IBKR tests: an IBKRAccount wired to a FakeIB through real DB rows."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AccountSetting
from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
from ba2_trade_platform.modules.accounts.ibkr_runtime import shutdown_runtime
from tests.factories import create_account_definition
from tests.ibkr_fakes import FakeIB

ACCOUNT_ID = "DU1234567"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "host": "127.0.0.1", "port": 4002, "client_id": 7, "account_id": ACCOUNT_ID,
    "paper_account": True, "read_only": False,
}


def write_settings(account_definition_id: int, settings: Dict[str, Any]) -> None:
    """Persist settings the way the settings dialog does (bool->json, number->float, str->str)."""
    for key, value in settings.items():
        if isinstance(value, bool):
            row = AccountSetting(account_id=account_definition_id, key=key, value_json=value)
        elif isinstance(value, (int, float)):
            row = AccountSetting(account_id=account_definition_id, key=key, value_float=float(value))
        else:
            row = AccountSetting(account_id=account_definition_id, key=key, value_str=str(value))
        add_instance(row)


def make_account(monkeypatch, fake: Optional[FakeIB] = None, *, extra: Optional[Dict[str, Any]] = None,
                 cooldown: float = 0.2, connect_timeout: float = 2.0, **overrides: Any):
    """``(account, fake)``. ``overrides`` replace default settings (None removes a key)."""
    fake = fake or FakeIB(account=overrides.get("account_id", ACCOUNT_ID))
    monkeypatch.setattr(IBKRAccount, "_ib_factory", staticmethod(lambda: fake))
    monkeypatch.setattr(IBKRAccount, "_CONNECT_COOLDOWN", cooldown)
    monkeypatch.setattr(IBKRAccount, "_CONNECT_TIMEOUT", connect_timeout)
    settings = {**DEFAULT_SETTINGS, **overrides, **(extra or {})}
    settings = {k: v for k, v in settings.items() if v is not None}
    definition = create_account_definition(name="IBKR test", provider="IBKR")
    # Account ids restart at 1 in every test but two process-wide registries are keyed by id:
    # the connection runtimes and the shared price cache. Start each test with neither.
    shutdown_runtime(definition.id)
    IBKRAccount._GLOBAL_PRICE_CACHE.pop(definition.id, None)
    write_settings(definition.id, settings)
    account = IBKRAccount(definition.id)
    return account, fake


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self, level: int = logging.WARNING) -> str:
        return chr(10).join(r.getMessage() for r in self.records if r.levelno >= level)


@pytest.fixture
def ibkr_logs():
    """Collect log records from the ba2 loggers. NOT caplog: they set ``propagate = False``."""
    handler = _ListHandler()
    names = ("ba2_trade_platform", "ba2_common")
    for name in names:
        logging.getLogger(name).addHandler(handler)
    try:
        yield handler
    finally:
        for name in names:
            logging.getLogger(name).removeHandler(handler)
