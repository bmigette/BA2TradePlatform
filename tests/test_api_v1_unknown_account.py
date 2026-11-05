"""Every phase-4 endpoint that takes an account id answers 404 not_found for an account that does not exist (the real
account factory raises InstanceNotFound for it; that must not become a 500)."""
import pytest

from tests.api_helpers import v1_client

pytestmark = pytest.mark.api_db

PATHS = ["/accounts/999", "/accounts/999/positions", "/accounts/999/orders", "/accounts/999/growth",
         "/accounts/999/growth/labels", "/accounts/999/growth/labels/Core/positions", "/accounts/999/pnl/closed",
         "/accounts/999/dividends", "/accounts/999/portfolio", "/stats/overview?account=999"]


@pytest.mark.parametrize("path", PATHS)
def test_unknown_account_is_404_not_found(path):
    r = v1_client().get(path)
    assert r.status_code == 404 and r.json()["error"] == "not_found", (path, r.status_code, r.text[:200])
