"""The two broker SDKs this platform writes directly against must be PINNED.

tastytrade 12.x is the OAuth-only async rewrite: `Account.place_order` became a
coroutine with a `dry_run` parameter that defaults to True, and `Session` moved to
`provider_secret`/`refresh_token`. An unpinned `tastytrade` line lets a routine
`pip install -r requirements.txt` move that API under TastyTradeAccount. alpaca-py
is pinned for the same reason (TradeAccount/Asset field shapes).

pandas-market-calendars is guarded here too. It is not an SDK we write against, but it is the
offline NYSE holiday/half-day calendar behind ba2_common.core.market_calendar, which is the
fallback under ReadOnlyAccountInterface.get_market_hours(). It reaches this venv only through
tastytrade's own `Requires-Dist: pandas-market-calendars>=5.1.1`, so moving the tastytrade pin
would silently delete the market-hours gate's offline path.
"""
from importlib.metadata import version
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
PYPROJECT = Path(__file__).resolve().parents[1] / "packages" / "common" / "pyproject.toml"


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


def _requirement_names():
    """Lower-cased distribution names declared in requirements.txt, comments stripped.

    Unlike ``_pinned_versions()`` this keeps FLOOR pins (``name>=x``) and bare names,
    because pandas-market-calendars is pinned as a floor rather than to an exact
    version: the NYSE holiday rules only ever get more complete, and 5.1.1 is what
    tastytrade already requires.
    """
    names = []
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        for separator in ("==", ">=", "<=", "~=", ">", "<"):
            if separator in line:
                line = line.partition(separator)[0]
                break
        names.append(line.strip().lower())
    return names


def test_pandas_market_calendars_is_declared_not_merely_transitive():
    """The offline NYSE calendar behind the market-hours fallback must be OURS to pin.

    Today it reaches this venv only through tastytrade's own
    `Requires-Dist: pandas-market-calendars>=5.1.1`. Relying on that means the day the
    tastytrade pin moves, the gate loses its offline path and every account reports
    source == "unavailable" -- so the allocation wizard refuses to submit, forever.
    """
    assert "pandas-market-calendars" in _requirement_names()


def test_ba2_common_declares_pandas_market_calendars():
    """ba2_common is separately installable (packages/common/pyproject.toml has its own
    dependencies list), and ba2_common.core.market_calendar imports the package. A
    standalone `pip install ba2trade-common` must therefore pull it in."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "pandas-market-calendars" in text, (
        "packages/common/pyproject.toml must list pandas-market-calendars; "
        "ba2_common.core.market_calendar imports it")


def test_the_nyse_calendar_builds_offline():
    """No network: pandas_market_calendars ships the NYSE holiday rules as DATA."""
    from pandas_market_calendars import get_calendar

    assert get_calendar("NYSE") is not None
