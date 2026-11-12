"""tools/build_screener_universe.py: which delisted names enter the dated universe, and why the others do not."""
import datetime as dt
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_screener_universe", Path(__file__).resolve().parents[1] / "tools" / "build_screener_universe.py")
bu = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bu)

D = dt.date


def _asset(symbol, name, exchange="NASDAQ", id_=None):
    return {"id": id_ or symbol, "symbol": symbol, "name": name, "exchange": exchange}


def test_classification_keeps_common_stock_and_drops_derivatives_and_etf_venues():
    c = bu.classify_symbol_and_name
    assert c("XLNX", "Xilinx, Inc. Common Stock", "NASDAQ") is None
    assert c("RUHN", "Ruhnn Holding Limited American Depositary Shares", "NASDAQ") is None
    assert c("AMX", "America Movil ADS (each representing the right to receive twenty Series B Shares)", "NYSE") is None
    assert c("ACAMU", "Acamar Partners Acquisition Corp. Units", "NASDAQ") == "name_not_common"
    assert c("ADNWW", "ADVENT TECHNOLOGIES HLDGS INC Warrant", "NASDAQ") == "name_not_common"
    assert c("IRHOR", "Iron Horse Acquisitions Corp. II Rights", "NASDAQ") == "name_not_common"
    assert c("FITBP", "Fifth Third 6% Non-Cumulative Preferred", "NASDAQ") == "name_not_common"
    assert c("SLY", "SPDR S&P 600 ETF", "ARCA") == "exchange_not_equity_venue"
    assert c("BRK.B", "Berkshire", "NYSE") == "symbol_shape" and c("692CVR020", None, None) == "symbol_shape"
    assert c("ABCDW", None, None) == "symbol_shape"          # no name on record, 5 letters ending W: a warrant
    assert c("TWTR", None, None) is None                      # a merger acquiree with no asset row is judged by FMP later
    assert c("GONE", "Gone Bank", "OTC") is None              # OTC rows (dead tickers move there) are judged by FMP


def test_month_gap_detection():
    assert not bu.has_month_gap([D(2020, 1, 1), D(2020, 2, 1), D(2020, 3, 1)])
    assert bu.has_month_gap([D(2020, 1, 1), D(2020, 3, 1)])
    assert not bu.has_month_gap([D(2020, 1, 1)])


def _gather(inactive, active=(), actions=None, universe=(), profiles=None, extras=()):
    fetch = None if profiles is None else (lambda symbols: {s: profiles[s] for s in symbols if s in profiles})
    return bu.gather_candidates(list(inactive), list(active), actions or {}, set(universe), fetch, extras)


def _profile(**kw):
    base = {"name": "Co", "exchange": "NYSE", "etf": False, "fund": False, "active": False, "ipo": "2001-01-01"}
    return dict(base, **kw)


def test_merger_acquiree_without_asset_row_needs_a_clean_fmp_profile():
    actions = {"cash_mergers": [{"acquiree_symbol": "TWTR", "process_date": "2022-10-28"},
                                {"acquiree_symbol": "XHYF", "process_date": "2026-05-15"},
                                {"acquiree_symbol": "ZZZZ", "process_date": "2022-01-03"}],
               "stock_mergers": [{"acquiree_symbol": "AAA", "acquirer_symbol": "AAA", "process_date": "2022-10-14"}]}
    cands, rej = _gather([], actions=actions,
                         profiles={"TWTR": _profile(name="Twitter, Inc."), "XHYF": _profile(etf=True)})
    assert sorted(cands) == ["TWTR"] and cands["TWTR"]["sources"] == ["cash_mergers"]
    assert dict((s, r) for s, r, _ in rej) == {"XHYF": "etf_or_fund", "ZZZZ": "no_profile"}   # AAA->AAA is not a delisting


def test_alpaca_named_asset_survives_without_an_fmp_profile_but_otc_rows_need_one():
    inactive = [_asset("OLDCO", "Old Co Common Stock", "NYSE"), _asset("SBNY", "Signature Bank", "OTC"),
                _asset("JUNK", "Junk Corp", "OTC")]
    cands, rej = _gather(inactive, profiles={"SBNY": _profile(name="Signature Bank")})
    assert sorted(cands) == ["OLDCO", "SBNY"]
    assert dict((s, r) for s, r, _ in rej) == {"JUNK": "no_profile"}


def test_renames_reused_tickers_and_the_existing_universe():
    inactive = [_asset("FB", "Facebook", id_="a"), _asset("RAD", "Rite Aid", "NYSE", id_="b"),
                _asset("WISH", "ContextLogic", id_="c"), _asset("DUP", "One", id_="d"), _asset("DUP", "Two", id_="e"),
                _asset("KEEP", "Already Here", id_="f")]
    active = [{"symbol": "META", "exchange": "NASDAQ", "name": "Meta"}, {"symbol": "LOGC", "exchange": "NASDAQ", "name": "x"}]
    actions = {"name_changes": [{"old_symbol": "FB", "new_symbol": "META"}, {"old_symbol": "RAD", "new_symbol": "RADCQ"},
                                {"old_symbol": "WISH", "new_symbol": "LOGC"}]}
    cands, rej = _gather(inactive, active, actions, universe=["KEEP"])
    reasons = dict((s, r) for s, r, _ in rej)
    assert sorted(cands) == ["RAD"]                    # RAD went OTC (bankruptcy): a delisting, kept
    assert reasons == {"FB": "renamed", "WISH": "renamed", "DUP": "ticker_reused_inactive"}


class _FakeTransport:
    def __init__(self, monthly, daily):
        self.monthly, self.daily, self.calls = monthly, daily, []

    def get(self, path, params):
        self.calls.append((params["timeframe"], params["symbols"], params["end"]))
        table = self.monthly if params["timeframe"] == "1Month" else self.daily
        return {"bars": {s: table[s] for s in params["symbols"].split(",") if s in table}, "next_page_token": None}


def _bar(day):
    return {"t": day + "T04:00:00Z"}


def test_verification_dates_the_delisting_by_the_last_daily_bar_and_rejects_the_rest():
    monthly = {"DEAD": [_bar("2022-01-01"), _bar("2022-02-01"), _bar("2022-03-01")],
               "ALIVE": [_bar("2026-09-01"), _bar("2026-10-01"), _bar("2026-11-01")],
               "GAPPY": [_bar("2022-01-01"), _bar("2022-03-01")], "TINY": [_bar("2022-03-01")],
               "REUSED": [_bar("2021-05-01"), _bar("2021-06-01")]}
    daily = {"DEAD": [_bar("2022-03-02"), _bar("2022-03-17")], "ALIVE": [_bar("2026-11-06")],
             "REUSED": [_bar("2021-06-10")]}
    cands = {s: {"name": f"{s} Inc", "exchange": "NYSE", "sources": ["assets_inactive"], "process_date": None}
             for s in ("DEAD", "ALIVE", "GAPPY", "TINY", "NOBARS", "REUSED")}
    cands["REUSED"]["ipo"] = "2023-02-01"
    transport = _FakeTransport(monthly, daily)
    accepted, rej = bu.verify_candidates(transport, cands, D(2019, 3, 1), D(2026, 11, 7), 10, 2, {})
    assert list(accepted) == ["DEAD"] and accepted["DEAD"]["last_bar"] == "2022-03-17"
    assert dict((s, r) for s, r, _ in rej) == {"ALIVE": "still_trading", "GAPPY": "history_gap", "TINY": "too_short",
                                               "NOBARS": "no_bars", "REUSED": "ipo_after_last_bar"}
    assert all(c[2] <= "2026-11-07T00:00:00Z" for c in transport.calls)       # never asks for the current day


def test_check_against_suspensions(tmp_path):
    tsv = tmp_path / "u.dates.tsv"
    tsv.write_text("symbol\tfirst_month\tlast_bar\tsources\tname\nA\t2020-01-01\t2022-03-17\tx\tA\n"
                   "B\t2020-01-01\t2022-03-17\tx\tB\nC\t2020-01-01\t2022-03-17\tx\tC\n")
    sus = tmp_path / "s.json"
    sus.write_text(json.dumps({"symbols": {"A": [["2022-03-17", None, "delisted", 9, None]],
                                           "B": [["2022-03-10", None, "delisted", 9, None]]}}))
    assert bu.check_against_suspensions(tsv, sus) == [
        ("B", "delisted event last session 2022-03-10 != expected last bar 2022-03-17"),
        ("C", "no delisted event in the cache build (no bars, or last bar within the slack)")]


def test_terminal_events_resolve_against_the_cache_delistings(tmp_path):
    actions = {"worthless_removals": [{"symbol": "WORTH", "process_date": "2023-06-01"}],
               "cash_mergers": [{"acquiree_symbol": "CASH", "process_date": "2022-10-28", "rate": 54.2},
                                {"acquiree_symbol": "OLDM", "process_date": "2019-01-01", "rate": 9.0}],
               "stock_mergers": [{"acquiree_symbol": "STK", "acquirer_symbol": "BIG", "process_date": "2022-02-14"}],
               "name_changes": [{"old_symbol": "RAD", "new_symbol": "RADCQ", "process_date": "2023-10-17"},
                                {"old_symbol": "FB", "new_symbol": "META", "process_date": "2022-06-09"}]}
    events = bu.terminal_events(actions, {"SIVB": "worthless", "PLAIN": None})
    d = lambda last: [last, None, "delisted", 9, None]          # noqa: E731
    suspensions = {"SIVB": [d("2023-03-09")], "WORTH": [d("2023-05-30")], "CASH": [d("2022-10-27")],
                   "OLDM": [d("2022-10-27")],                   # a merger of ANOTHER era on a reused ticker: not this delisting's
                   "STK": [d("2022-02-11")], "RAD": [d("2023-10-16")], "PLAIN": [d("2022-01-03")],
                   "TEMP": [["2022-01-03", "2022-03-01", "exchange_gap", 40, None]]}
    terminals, counts = bu.resolve_terminals(events, suspensions)
    assert {s: (t["kind"], t["price"]) for s, t in terminals.items()} == {
        "SIVB": ("worthless", 0.0), "WORTH": ("worthless", 0.0), "CASH": ("cash_merger", 54.2),
        "STK": ("stock_merger", None), "RAD": ("bankruptcy_otc", 0.0)}
    assert counts["unknown"] == 2 and counts["worthless"] == 2                                # OLDM and PLAIN; TEMP is no delisting
    events_file, sus_file = tmp_path / "e.json", tmp_path / "suspensions.json"
    events_file.write_text(json.dumps(events))
    sus_file.write_text(json.dumps({"build": "B7", "rule": {}, "symbols": suspensions}))
    target, _ = bu.install_terminals(events_file, sus_file)
    assert json.loads(target.read_text())["build"] == "B7" and target.name == "delisting_terminals.json"
