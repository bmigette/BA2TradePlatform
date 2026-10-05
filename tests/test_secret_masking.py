"""Credentials are hidden by default everywhere the UI shows or accepts them.

Pure rules (``is_secret_setting``, ``mask_secret``, ``mask_secrets_in_data``), the dialogs'
input construction (password + eye toggle, never pre-revealed), the no-edit-save guarantee for a
masked field, and a source guard so a NEW input that renders a credential cannot ship unmasked.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.ui.utils.secret_mask import (
    is_secret_setting, mask_secret, mask_secrets_in_data, secret_input_css)
from tests.conftest import MockExpert
from tests.test_settings_dialog_missing_defaults import (   # noqa: F401  (fixtures + helpers)
    _El, _account_edit_form, _account_rows, _account_tab, _stored_alpaca_account,
    fake_ui, instance, tab)

UI_DIR = pathlib.Path(settings_page.__file__).resolve().parent.parent
BULLET = "•"


# ------------------------------------------------------------------------- the rule
@pytest.mark.parametrize("key", [
    "api_key", "api_secret", "refresh_token", "client_secret", "flex_token", "password",
    "passphrase", "private_key", "bearer", "auth", "auth_token", "openai_api_key",
    "FMP_API_KEY", "aws_secret_access_key", "aws_access_key_id", "stocktwits_oauth_token",
    "apiKey", "accessToken", "DB_PASSWORD", "bearer_token", "token",
])
def test_credentials_are_secret(key):
    assert is_secret_setting(key)


@pytest.mark.parametrize("key", [
    "max_tokens", "tokens_used", "token_limit", "max_token", "token_count", "total_tokens",
    "prompt_tokens", "completion_tokens", "tokens", "xai_team_id", "account_id", "host",
    "client_id", "flex_query_id", "author", "authority", "keyboard", "monkey",
    "aws_bedrock_region", "paper_account", "key_levels", "data_feed",
])
def test_non_secrets_are_not_secret(key):
    assert not is_secret_setting(key)


def test_a_definition_can_mark_a_setting_secret():
    assert is_secret_setting("whatever", {"secret": True})
    assert is_secret_setting("whatever", {"type": "password"})
    assert is_secret_setting("whatever", {"type": "str", "ui_editor_type": "password"})
    assert not is_secret_setting("whatever", {"type": "str"})
    assert not is_secret_setting("whatever", {"secret": False})


def test_the_account_classes_flag_their_credentials():
    for provider, keys in {"Alpaca": {"api_key", "api_secret"},
                           "TastyTrade": {"client_secret", "refresh_token"},
                           "IBKR": {"flex_token"}}.items():
        defs = settings_page.providers[provider].get_settings_definitions()
        for key in keys:
            assert defs[key].get("secret") is True, (provider, key)
        flagged = {k for k, d in defs.items() if d.get("secret") is True}
        assert flagged == keys


def test_every_declared_account_setting_that_looks_secret_is_flagged():
    for provider, cls in settings_page.providers.items():
        for key, meta in cls.get_settings_definitions().items():
            if is_secret_setting(key):
                assert meta.get("secret") is True, (provider, key)


# ------------------------------------------------------------------------- masking
def test_mask_secret_shows_last_four_only_from_eight_chars():
    assert mask_secret("abcdefgh") == BULLET * 8 + "efgh"
    assert mask_secret("PK1234567890XYZ9") == BULLET * 8 + "XYZ9"
    assert mask_secret("abcdefg") == BULLET * 8          # 7 chars: nothing revealed
    assert mask_secret("ab") == BULLET * 8
    assert mask_secret("") == "" and mask_secret(None) == ""
    assert "secretvalue" not in mask_secret("secretvalue123")


def test_mask_secrets_in_data_masks_nested_secrets_and_copies():
    data = {"a": 1, "api_key": "abcdefghijkl", "n": {"refresh_token": "short", "max_tokens": 5},
            "l": [{"password": "hunter2hunter2"}, 3]}
    out = mask_secrets_in_data(data)
    assert out["a"] == 1 and out["n"]["max_tokens"] == 5
    assert out["api_key"] == BULLET * 8 + "ijkl"
    assert out["n"]["refresh_token"] == BULLET * 8
    assert out["l"][0]["password"] == BULLET * 8 + "ter2"
    assert data["api_key"] == "abcdefghijkl"              # the input is untouched


def test_the_eye_toggle_is_a_40px_target():
    css = secret_input_css()
    assert "min-width:40px" in css and "min-height:40px" in css
    from ba2_trade_platform.ui.utils.phone_sections import phone_css
    assert css in phone_css()


# ------------------------------------------------------------------------- the dialogs
def _is_masked_input(el):
    return (el.kind == "input" and el.kwargs.get("password") is True
            and el.kwargs.get("password_toggle_button") is True)


@pytest.mark.parametrize("provider", ["Alpaca", "IBKR", "TastyTrade"])
def test_account_dialog_builds_secret_inputs_masked_and_the_rest_plain(fake_ui, provider):
    t = _account_tab()
    t._render_dynamic_settings(provider, None)
    defs = settings_page.providers[provider].get_merged_settings_definitions()
    found = 0
    for key, inp in t.settings_inputs.items():
        if defs[key]["type"] in ("bool", "float") or defs[key].get("valid_values"):
            continue
        if is_secret_setting(key, defs[key]):
            found += 1
            assert _is_masked_input(inp), key
        else:
            assert inp.kwargs.get("password") is None, key
    assert found >= 1


def test_expert_dialog_masks_secret_str_settings(tab, instance, fake_ui, monkeypatch):
    defs = dict(MockExpert.get_settings_definitions())
    defs["stocktwits_oauth_token"] = {"type": "str", "default": "", "description": "t",
                                      "ui_editor_type": "password"}
    defs["vendor_api_key"] = {"type": "str", "default": "", "description": "k"}
    defs["note"] = {"type": "str", "default": "x", "description": "n"}
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(lambda cls: defs))
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    inputs = tab.expert_settings_inputs
    assert _is_masked_input(inputs["stocktwits_oauth_token"])
    assert _is_masked_input(inputs["vendor_api_key"])
    assert inputs["note"].kwargs.get("password") is None


def test_an_untouched_masked_field_is_not_rewritten_on_save(fake_ui, monkeypatch):
    """password=True only changes how the browser draws the field: the control still holds the
    REAL stored text (never bullets), and a no-edit save writes nothing."""
    monkeypatch.setattr(settings_page, "get_account_instance_from_id", lambda *a, **k: None)
    acc = _stored_alpaca_account()
    before = _account_rows(acc.id)
    t = _account_edit_form("Alpaca", acc)
    assert t.settings_inputs["api_key"].value == "k"
    assert t.settings_inputs["api_secret"].value == "s"
    t.save_account(acc)
    assert not any(kw.get("type") == "negative" for _, kw in fake_ui.notes)
    assert _account_rows(acc.id) == before


def test_an_edited_masked_field_is_saved_verbatim(fake_ui, monkeypatch):
    monkeypatch.setattr(settings_page, "get_account_instance_from_id", lambda *a, **k: None)
    acc = _stored_alpaca_account()
    t = _account_edit_form("Alpaca", acc)
    t.settings_inputs["api_secret"].value = "new-secret-value"
    t.save_account(acc)
    rows = {r[0]: r[1] for r in _account_rows(acc.id)}
    assert rows["api_secret"] == "new-secret-value" and rows["api_key"] == "k"


# ------------------------------------------------------------------------- source guard
def _ui_sources():
    for path in sorted(UI_DIR.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _string_constants(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _ui_calls(tree, names):
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in names
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "ui"):
            yield n


def test_no_ui_input_with_a_credential_label_is_unmasked():
    """A ``ui.input`` whose literal label text names a credential must be built through
    ``_secret_input`` / ``password=True``. (Dynamic dialogs decide per key at runtime: tested above.)"""
    offenders = []
    for path, tree in _ui_sources():
        for n in _ui_calls(tree, ("input", "textarea")):
            kwargs = {k.arg for k in n.keywords}
            texts = [s for k in n.keywords if k.arg in ("label", "placeholder")
                     for s in _string_constants(k.value)]
            texts += [s for a in n.args for s in _string_constants(a)]
            if any(is_secret_setting(t) for t in texts) and "password" not in kwargs:
                offenders.append((path.name, n.lineno, texts))
    assert offenders == []


def test_raw_json_viewers_go_through_the_masking_component():
    offenders = []
    for path, tree in _ui_sources():
        if path.name == "secret_display.py":
            continue
        offenders += [(path.name, n.lineno) for n in _ui_calls(tree, ("json_editor", "code"))]
    assert offenders == []


def test_global_settings_credentials_all_use_the_secret_input():
    src = pathlib.Path(settings_page.__file__).read_text(encoding="utf-8")
    assert src.count("password_toggle_button=True") == 1   # defined once, in _secret_input
    assert src.count("_secret_input(") >= 20
