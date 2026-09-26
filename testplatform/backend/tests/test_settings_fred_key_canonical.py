"""The test platform's Settings -> API Keys page stores the FRED key under ``fred_api_key``.

It listed and saved ``FRED_API_KEY``, while every FRED reader looks up ``fred_api_key``
(AppSetting lookups are exact), so a key saved here reached nothing.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def keys_db(tmp_path, monkeypatch):
    from ba2_common.core import db

    previous = db._db_file
    db.configure_db(str(tmp_path / "keys.sqlite"))
    db.init_db()
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    yield
    db.configure_db(previous)


def _stored():
    from sqlmodel import Session, select

    from ba2_common.core.db import get_engine
    from ba2_common.core.models import AppSetting

    with Session(get_engine()) as s:
        return {r.key: r.value_str for r in s.exec(select(AppSetting)).all()}


def test_the_page_lists_the_canonical_key():
    from app.api.settings import KNOWN_CREDENTIAL_KEYS
    from ba2_common.core.fred_api_key import FRED_API_KEY_SETTING

    assert FRED_API_KEY_SETTING in KNOWN_CREDENTIAL_KEYS
    assert "FRED_API_KEY" not in KNOWN_CREDENTIAL_KEYS


def test_saving_the_canonical_key_reaches_the_resolver(keys_db):
    from app.api.settings import CredentialKeysUpdate, list_credential_keys, update_credential_keys
    from ba2_common.core.fred_api_key import resolve_fred_api_key

    update_credential_keys(CredentialKeysUpdate(values={"fred_api_key": "abcd1234"}))
    assert _stored()["fred_api_key"] == "abcd1234"
    assert resolve_fred_api_key() == "abcd1234"
    listed = {k.key: k for k in list_credential_keys().keys}
    assert listed["fred_api_key"].is_set and listed["fred_api_key"].masked_value.endswith("1234")


def test_a_legacy_spelling_is_saved_under_the_canonical_name(keys_db):
    from app.api.settings import CredentialKeysUpdate, update_credential_keys

    resp = update_credential_keys(CredentialKeysUpdate(values={"FRED_API_KEY": "zz99"}))
    assert resp.updated == ["fred_api_key"]
    stored = _stored()
    assert stored["fred_api_key"] == "zz99" and "FRED_API_KEY" not in stored
