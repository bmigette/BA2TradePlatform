"""The two broker SDKs this platform writes directly against must be PINNED.

tastytrade 12.x is the OAuth-only async rewrite: `Account.place_order` became a
coroutine with a `dry_run` parameter that defaults to True, and `Session` moved to
`provider_secret`/`refresh_token`. An unpinned `tastytrade` line lets a routine
`pip install -r requirements.txt` move that API under TastyTradeAccount. alpaca-py
is pinned for the same reason (TradeAccount/Asset field shapes).
"""
from importlib.metadata import version
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def _pinned_versions():
    """Parse `name==version` lines out of requirements.txt, ignoring comments."""
    pins = {}
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, _, pinned = line.partition("==")
        pins[name.strip().lower()] = pinned.strip()
    return pins


def test_tastytrade_is_pinned_to_the_installed_version():
    assert _pinned_versions().get("tastytrade") == version("tastytrade")


def test_alpaca_py_is_pinned_to_the_installed_version():
    assert _pinned_versions().get("alpaca-py") == version("alpaca-py")
