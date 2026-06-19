"""Tests for the survivorship-free universe builder (ba2_providers.screener.universe).

Pure list/replay logic — all FMP fetches are mocked. These tests prove:
  - broad_universe honours the [ipoDate, delistedDate] lifecycle window (a name that
    traded on the scan date but delisted later is PRESENT; gone after delistedDate),
  - index_universe inverts post-as_of add/remove change-log events to reconstruct
    dated membership,
  - fetch_lifecycle_map merges active (no delistedDate) + delisted lists and paginates.
"""
from datetime import datetime, timezone

import ba2_providers.screener.universe as U


def D(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class _R:
    """Minimal stand-in for a requests.Response with a .json()."""

    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


# --------------------------------------------------------------------------- #
# broad_universe — lifecycle window                                            #
# --------------------------------------------------------------------------- #


def test_broad_universe_lifecycle_window():
    lifecycle = {
        "ALIVE":  (D("2010-01-01"), None),             # active, IPO'd long ago
        "DEAD":   (D("2015-01-01"), D("2021-06-30")),  # delisted AFTER the scan date
        "NEWBIE": (D("2024-01-01"), None),             # IPO'd after the scan date
    }
    on = U.broad_universe(D("2020-06-30"), lifecycle=lifecycle)
    assert "ALIVE" in on
    assert "DEAD" in on        # survivorship: traded on 2020-06-30, delisted 2021 -> present
    assert "NEWBIE" not in on  # not yet public on the scan date


def test_broad_universe_excludes_delisted_after_death():
    lifecycle = {"DEAD": (D("2015-01-01"), D("2021-06-30"))}
    after = U.broad_universe(D("2022-01-03"), lifecycle=lifecycle)
    assert "DEAD" not in after  # gone after its delistedDate


def test_broad_universe_missing_ipo_is_always_eligible():
    # An active row whose ipoDate was absent (None) must not be excluded.
    lifecycle = {"NOIPO": (None, None)}
    assert "NOIPO" in U.broad_universe(D("1990-01-01"), lifecycle=lifecycle)


def test_broad_universe_boundary_dates_inclusive():
    # On the exact ipoDate and the exact delistedDate the symbol is tradable.
    lifecycle = {"EDGE": (D("2020-01-01"), D("2020-12-31"))}
    assert "EDGE" in U.broad_universe(D("2020-01-01"), lifecycle=lifecycle)  # == ipoDate
    assert "EDGE" in U.broad_universe(D("2020-12-31"), lifecycle=lifecycle)  # == delistedDate
    assert "EDGE" not in U.broad_universe(D("2019-12-31"), lifecycle=lifecycle)  # before IPO
    assert "EDGE" not in U.broad_universe(D("2021-01-01"), lifecycle=lifecycle)  # after delist


def test_broad_universe_is_sorted():
    lifecycle = {"ZZZ": (None, None), "AAA": (None, None), "MMM": (None, None)}
    out = U.broad_universe(D("2020-01-01"), lifecycle=lifecycle)
    assert out == sorted(out)


# --------------------------------------------------------------------------- #
# fetch_lifecycle_map — merge active + delisted, pagination                    #
# --------------------------------------------------------------------------- #


def test_fetch_lifecycle_map_merges_and_paginates(monkeypatch):
    calls = {"delisted_pages": []}

    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("/available-traded/list"):
            return _R([
                {"symbol": "alive", "ipoDate": "2010-05-01"},  # lowercase -> upper()
                {"symbol": "DUP", "ipoDate": "2011-01-01"},    # also in delisted (delisted wins)
                {"not_a": "symbol"},                           # skipped (no symbol)
            ])
        if url.endswith("/delisted-companies"):
            page = (params or {}).get("page")
            calls["delisted_pages"].append(page)
            if page == 0:
                # full page (100 rows) -> a second page must be fetched
                rows = [
                    {"symbol": f"D{i}", "ipoDate": "2012-01-01", "delistedDate": "2019-01-01"}
                    for i in range(99)
                ]
                rows.append({"symbol": "DUP", "ipoDate": "2011-01-01", "delistedDate": "2018-06-30"})
                return _R(rows)  # len == 100 == page size
            # short page -> last page
            return _R([{"symbol": "LAST", "ipoDate": "2013-01-01", "delistedDate": "2020-01-01"}])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")

    m = U.fetch_lifecycle_map()
    assert calls["delisted_pages"] == [0, 1]          # paginated until the short page
    assert m["ALIVE"] == (D("2010-05-01"), None)       # active -> delisted_date None
    assert m["DUP"] == (D("2011-01-01"), D("2018-06-30"))  # delisted list overrides active
    assert m["LAST"] == (D("2013-01-01"), D("2020-01-01"))
    assert "D0" in m and m["D0"][1] == D("2019-01-01")


# --------------------------------------------------------------------------- #
# index_universe — change-log replay                                          #
# --------------------------------------------------------------------------- #


def test_index_universe_replays_changelog(monkeypatch):
    # current members = {AAA, BBB, CCC}; after as_of, CCC was added and DDD removed.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("_constituent") and "historical" not in url:
            return _R([{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}])
        return _R([
            {"date": "2023-03-01", "symbol": "CCC", "removedTicker": ""},  # CCC added 2023 (after as_of)
            {"date": "2023-03-01", "symbol": "", "removedTicker": "DDD"},  # DDD removed 2023
        ])

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("sp500", D("2022-01-03"))
    assert "CCC" not in members  # added after as_of -> not a member then
    assert "DDD" in members      # removed after as_of -> a member then
    assert "AAA" in members and "BBB" in members


def test_index_universe_ignores_events_on_or_before_as_of(monkeypatch):
    # An add/remove that happened BEFORE as_of is already reflected in membership.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("_constituent") and "historical" not in url:
            return _R([{"symbol": "AAA"}, {"symbol": "BBB"}])
        return _R([
            {"date": "2019-01-01", "symbol": "AAA", "removedTicker": ""},  # before as_of -> ignored
            {"date": "2019-01-01", "symbol": "", "removedTicker": "ZZZ"},  # before as_of -> ignored
        ])

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("nasdaq", D("2022-01-03"))
    assert members == ["AAA", "BBB"]  # pre-as_of events do not perturb membership


def test_index_universe_added_security_alias(monkeypatch):
    # FMP change-log rows sometimes carry the added ticker under 'addedSecurity'.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("_constituent") and "historical" not in url:
            return _R([{"symbol": "AAA"}, {"symbol": "EEE"}])
        return _R([
            {"dateAdded": "2024-01-01", "addedSecurity": "EEE", "removedTicker": ""},
        ])

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("sp500", D("2022-01-03"))
    assert "EEE" not in members  # added (via addedSecurity/dateAdded) after as_of -> removed
    assert "AAA" in members


def test_index_universe_rejects_unknown_index():
    import pytest
    with pytest.raises(ValueError):
        U.index_universe("russell2000", D("2022-01-03"))
