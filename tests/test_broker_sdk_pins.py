"""The two broker SDKs this platform writes directly against must be BOUNDED.

tastytrade 12.x is the OAuth-only async rewrite: `Account.place_order` became a
coroutine with a `dry_run` parameter that defaults to True, and `Session` moved to
`provider_secret`/`refresh_token`. An unbounded `tastytrade` line lets a routine
`pip install -r requirements.txt` move that API under TastyTradeAccount. alpaca-py
is bounded for the same reason (TradeAccount/Asset field shapes).

BOUNDS, NOT AN EXACT PIN -- changed 2026-09-18 after the exact pin became the hazard it
existed to prevent. `==12.0.2` / `==0.43.2` were written from some other reference and were
never what was installed here: both venvs, the live trading one included, have run 12.4.1 /
0.43.4 since May, so `pip install -r requirements.txt` would have DOWNGRADED the SDK the
platform trades on by three minor versions. An exact pin only holds while someone keeps it
equal to reality, and for a month nobody did.

What actually has to be true is a floor and a ceiling: the install can never go BACKWARDS from
what production runs, and can never cross into the next breaking version by accident. These
tests assert those two properties against the installed distribution, so they stay true as the
version moves instead of going stale the next time it does.

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


def _requirements():
    """Every requirement line in requirements.txt, parsed, keyed by lowercased name."""
    from packaging.requirements import Requirement

    out = {}
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        out[req.name.lower()] = req
    return out


def _assert_bounded(dist_name, next_breaking):
    """The installed version satisfies the line, cannot be downgraded by it, and the line
    refuses the next breaking version."""
    from packaging.version import Version

    req = _requirements().get(dist_name)
    assert req is not None, f"{dist_name} is not in requirements.txt at all"
    installed = Version(version(dist_name))

    assert req.specifier.contains(installed), (
        f"{dist_name}=={installed} is installed but does not satisfy {req.specifier} -- "
        f"`pip install -r requirements.txt` would CHANGE the SDK the live platform trades on")

    lower = [s for s in req.specifier if s.operator in (">=", "==", "~=")]
    assert lower, f"{dist_name} has no lower bound; an install could downgrade it"
    assert max(Version(s.version) for s in lower) >= installed, (
        f"{dist_name}'s floor is below the installed {installed}; an install could DOWNGRADE "
        f"production")

    assert not req.specifier.contains(Version(next_breaking)), (
        f"{dist_name} would accept {next_breaking}, which may move the broker API under us "
        f"on a routine install")


def test_tastytrade_cannot_be_downgraded_or_cross_a_major():
    _assert_bounded("tastytrade", "13.0.0")


def test_alpaca_py_cannot_be_downgraded_or_cross_a_breaking_minor():
    # alpaca-py is 0.x, where the MINOR is the breaking number.
    _assert_bounded("alpaca-py", "0.44.0")


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
