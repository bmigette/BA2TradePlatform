"""Logic behind the Settings-page API token card."""
from ba2_trade_platform.api.auth import get_allow_remote, get_api_token
from ba2_trade_platform.ui.pages.api_token_card import (
    generate_token_for_ui, needs_remote_confirmation, set_allow_remote_for_ui, token_status_text,
)
import pytest


pytestmark = pytest.mark.api_db


def test_status_text_reflects_stored_token_without_revealing_it():
    assert "No API token" in token_status_text()
    token = generate_token_for_ui()
    text = token_status_text()
    assert "set" in text and token not in text and token[-4:] in text


def test_generate_replaces_the_stored_token():
    first = generate_token_for_ui()
    second = generate_token_for_ui()
    assert first != second and get_api_token() == second


def test_allow_remote_toggle_is_off_by_default_and_saved_immediately():
    assert get_allow_remote() is False
    set_allow_remote_for_ui(True)
    assert get_allow_remote() is True
    set_allow_remote_for_ui(False)
    assert get_allow_remote() is False


def test_only_the_off_to_on_flip_needs_confirmation():
    assert needs_remote_confirmation(False, True) is True
    assert needs_remote_confirmation(True, False) is False
    assert needs_remote_confirmation(False, False) is False
    assert needs_remote_confirmation(True, True) is False
