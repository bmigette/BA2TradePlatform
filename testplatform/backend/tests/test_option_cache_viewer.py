"""Read-only option-cache viewer: the honesty contract.

The viewer is a broker-style chain over data the backtest actually holds, and a
broker-style layout invites filling every column. Most of them have no data. These
tests pin the *absence* as hard as the presence:

  * the legacy Alpaca sqlite stores ``bid == ask == close`` in every quoted row —
    a synthesised placeholder, NOT a quote — so it must never be presented as a spread;
  * its ``open_interest`` / ``volume`` chain columns are NULL in all 1,440,782 rows and
    its ``iv`` / ``delta`` / ``gamma`` / ``theta`` / ``vega`` are NULL in 54% of them, and
    NULL must render as "n/a" with a reason, never as ``0.00``;
  * greeks are computable only where an IV *and* a spot exist, and a computed greek is
    model output, not exchange data — it must say so;
  * the as-of picker may only offer dates that exist — the real chain table holds ONE
    snapshot date in total (2024-02-01; see
    ``ba2_common.core.option_selector._publishes_spread``, the one re-verified record; this
    header used to say three) — and asking for one with no data must say which do.

Everything here runs against fixtures. Nothing in the viewer may write to any cache.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

# A PRE-GREEKS schema shape, kept ON PURPOSE and NOT a picture of today's file. Note what
# this option_bar does NOT have: iv/delta/gamma/theta/vega. A cache written before
# options_cache._GREEK_COLS keeps that layout (CREATE TABLE IF NOT EXISTS is a no-op on an
# existing table), so the viewer must INTROSPECT columns and never assume them — which is the
# only thing this fixture exists to prove.
# The real ~/Documents/ba2/common/cache/options/options_history.sqlite is NOT like this: it is
# 4.12 GB (not 10.9), its option_bar declares all five and populates them on 88.2% of rows, and
# its option_chain populates them on 46.0%. See option_selector._publishes_spread.
_LEGACY_CHAIN_DDL = """CREATE TABLE option_chain(
  underlying TEXT, as_of TEXT, occ_symbol TEXT, option_type TEXT, strike REAL, expiry TEXT,
  bid REAL, ask REAL, last REAL, iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  open_interest INTEGER, volume INTEGER, PRIMARY KEY(underlying, as_of, occ_symbol))"""
_LEGACY_BAR_DDL = """CREATE TABLE option_bar(
  occ_symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  underlying TEXT, option_type TEXT, strike REAL, expiry TEXT, PRIMARY KEY(occ_symbol, date))"""

# FIXTURE DATES, not cache dates — every test seeds them itself via _seed_legacy, so nothing
# here depends on either existing in the real file. (This comment used to call AS_OF "a real
# snapshot date in the legacy cache"; it is not one. The real file's ONLY snapshot is
# 2024-02-01 — OTHER_AS_OF, which is real by coincidence, not by design.) What matters is that
# both are FIXED: a viewer that quietly answered for "today" would pass on any date.
AS_OF = "2026-06-09"          # Never "today".
OTHER_AS_OF = "2024-02-01"    # ditto
NEXT_DAY = "2026-06-10"       # a bar date that is NOT a chain snapshot date
EXPIRY_NEAR = "2026-06-19"    # 10 DTE from AS_OF
EXPIRY_FAR = "2026-07-17"     # 38 DTE from AS_OF


def _occ(sym: str, expiry: str, right: str, strike: float) -> str:
    y, m, d = expiry.split("-")
    return f"{sym}{y[2:]}{m}{d}{right}{int(round(strike * 1000)):08d}"


def _seed_legacy(path: str) -> None:
    """The real file's QUOTE shape (bid==ask==last) on the PRE-GREEKS schema above. The
    all-NULL greeks are the fixture's doing, not the store's — see the DDL note."""
    cx = sqlite3.connect(path)
    cx.execute(_LEGACY_CHAIN_DDL)
    cx.execute(_LEGACY_BAR_DDL)
    cx.execute("CREATE INDEX idx_option_chain_underlying ON option_chain(underlying)")
    cx.execute("CREATE INDEX idx_option_bar_underlying ON option_bar(underlying)")
    rows = []
    for expiry in (EXPIRY_NEAR, EXPIRY_FAR):
        for strike, call_px, put_px in ((190.0, 12.5, 1.75), (200.0, 5.5, 4.9), (210.0, 1.6, 11.4)):
            for right, px in (("C", call_px), ("P", put_px)):
                occ = _occ("AAPL", expiry, right, strike)
                rows.append((
                    "AAPL", AS_OF, occ, "call" if right == "C" else "put", strike, expiry,
                    px, px, px,          # bid == ask == last: the synthesised placeholder
                    None, None, None, None, None,   # iv, delta, gamma, theta, vega
                    None, None,                     # open_interest, volume
                ))
    # A second snapshot date, so "available dates" has more than one entry to be right about.
    rows.append((
        "AAPL", OTHER_AS_OF, _occ("AAPL", "2024-02-16", "C", 180.0), "call", 180.0, "2024-02-16",
        3.3, 3.3, 3.3, None, None, None, None, None, None, None,
    ))
    # SYNTHETIC, and deliberately unlike the real file: bid != ask != last. ZERO of the
    # 1,083,571 quoted rows in the production cache look like this. It exists only to pin
    # WHICH column the single "Close" reads — the last trade, not the bid — so that a
    # differently-built cache cannot silently start showing a bid under a Close header.
    rows.append((
        "AAPL", OTHER_AS_OF, _occ("AAPL", "2024-02-16", "C", 185.0), "call", 185.0, "2024-02-16",
        2.9, 3.1, 3.0, None, None, None, None, None, None, None,
    ))
    # A different underlying, so symbol search has something to NOT match.
    rows.append((
        "MSFT", AS_OF, _occ("MSFT", EXPIRY_NEAR, "C", 400.0), "call", 400.0, EXPIRY_NEAR,
        7.7, 7.7, 7.7, None, None, None, None, None, None, None,
    ))
    # A contract present in the snapshot but with NO price at all. This is NOT an edge case:
    # 357,211 of the real file's 1,440,782 chain rows have NULL bid/ask/last. "Listed in
    # the chain" and "priced" are different claims.
    rows.append((
        "AAPL", AS_OF, _occ("AAPL", EXPIRY_NEAR, "C", 220.0), "call", 220.0, EXPIRY_NEAR,
        None, None, None, None, None, None, None, None, None, None,
    ))
    cx.executemany(
        "INSERT INTO option_chain(underlying,as_of,occ_symbol,option_type,strike,expiry,"
        "bid,ask,last,iv,delta,gamma,theta,vega,open_interest,volume) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # Daily bars: OHLC + volume, no quotes, no greeks. A bar date that is NOT a chain as_of,
    # which is the whole point of exposing the bar store separately.
    bars = []
    for bar_date in ("2026-06-09", "2026-06-10"):
        for strike, px in ((190.0, 12.5), (200.0, 5.5)):
            occ = _occ("AAPL", EXPIRY_NEAR, "C", strike)
            bars.append((occ, bar_date, px, px + 0.4, px - 0.3, px + 0.1, 42.0,
                         "AAPL", "call", strike, EXPIRY_NEAR))
            occp = _occ("AAPL", EXPIRY_NEAR, "P", strike)
            bars.append((occp, bar_date, 2.0, 2.2, 1.8, 2.05, 7.0,
                         "AAPL", "put", strike, EXPIRY_NEAR))
    cx.executemany(
        "INSERT INTO option_bar(occ_symbol,date,open,high,low,close,volume,underlying,"
        "option_type,strike,expiry) VALUES(?,?,?,?,?,?,?,?,?,?,?)", bars)
    cx.commit()
    cx.close()


def _seed_parquet(root: str) -> None:
    """The TastyTrade parquet store — the only one that carries iv + open_interest."""
    from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    store = OptionHistoryParquetStore(root=root)
    exp = date.fromisoformat(EXPIRY_NEAR)
    bars = [
        # strike 200 call: a full row — iv AND open interest present.
        OptionEodBar(occ_symbol=_occ("AAPL", EXPIRY_NEAR, "C", 200.0),
                     bar_date=date.fromisoformat(AS_OF),
                     open=5.4, high=5.8, low=5.2, close=5.5, volume=1200,
                     open_interest=3410, iv=0.2841),
        # strike 200 put: iv present, open interest genuinely NULL (never recorded).
        OptionEodBar(occ_symbol=_occ("AAPL", EXPIRY_NEAR, "P", 200.0),
                     bar_date=date.fromisoformat(AS_OF),
                     open=4.8, high=5.0, low=4.7, close=4.9, volume=800,
                     open_interest=None, iv=0.3102),
        # strike 210 call: NULL iv, and open interest a genuine ZERO. Zero is a recorded
        # fact; NULL is the absence of one. They must never render the same.
        OptionEodBar(occ_symbol=_occ("AAPL", EXPIRY_NEAR, "C", 210.0),
                     bar_date=date.fromisoformat(AS_OF),
                     open=1.5, high=1.7, low=1.4, close=1.6, volume=0,
                     open_interest=0, iv=None),
        # A far-OTM put with a fat skew IV. THREE ivs on this expiry, deliberately: with
        # two, the median and the mean coincide and a mean-instead-of-median summary would
        # be invisible. 0.9 also makes the median robust in a way a mean is not, which is
        # why the header says "median".
        OptionEodBar(occ_symbol=_occ("AAPL", EXPIRY_NEAR, "P", 190.0),
                     bar_date=date.fromisoformat(AS_OF),
                     open=1.7, high=1.9, low=1.6, close=1.75, volume=300,
                     open_interest=880, iv=0.9),
        # THE NEXT DAY, same contract, deliberately different numbers. A partition holds a
        # whole history; showing a chain for one as-of date means filtering to that date,
        # and a viewer that quietly mixes days is worse than one that shows nothing.
        OptionEodBar(occ_symbol=_occ("AAPL", EXPIRY_NEAR, "C", 200.0),
                     bar_date=date.fromisoformat(NEXT_DAY),
                     open=6.1, high=6.6, low=6.0, close=6.25, volume=1500,
                     open_interest=3500, iv=0.1110),
    ]
    store.write_partition("AAPL", exp, bars,
                          date.fromisoformat(AS_OF), date.fromisoformat(AS_OF))


@pytest.fixture
def legacy_only(tmp_path, monkeypatch):
    """Legacy sqlite present, parquet store ABSENT — exactly this machine's state."""
    import ba2_common.config as cfg
    cache = tmp_path / "cache"
    (cache / "options").mkdir(parents=True)
    db = cache / "options" / "options_history.sqlite"
    _seed_legacy(str(db))
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(cache))
    monkeypatch.setattr(cfg, "OPTIONS_CACHE_DB", str(db))
    from app.services import option_cache_reader
    option_cache_reader.reset_caches()
    yield {"db": str(db), "cache": str(cache)}
    option_cache_reader.reset_caches()


@pytest.fixture
def both_stores(legacy_only):
    _seed_parquet(os.path.join(legacy_only["cache"], "TastyTradeOptionsProvider"))
    from app.services import option_cache_reader
    option_cache_reader.reset_caches()
    return legacy_only


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def _cell(leg, name):
    return leg[name]


# ---------------------------------------------------------------------------
# Stores: what exists, and what each one can and cannot say
# ---------------------------------------------------------------------------

def test_stores_declares_each_backend_capabilities(legacy_only, client):
    r = client.get("/api/cache/options/stores")
    assert r.status_code == 200
    by_id = {s["id"]: s for s in r.json()["stores"]}
    assert set(by_id) == {"alpaca-chain", "alpaca-bars", "tastytrade-parquet"}

    chain = by_id["alpaca-chain"]
    assert chain["present"] is True
    assert chain["has_iv"] is False
    assert chain["has_open_interest"] is False
    assert chain["has_greeks"] is False
    # The crux: this store has NO spread, and says so rather than showing one.
    assert chain["has_quote_spread"] is False
    assert "bid" in chain["quote_note"].lower() and "ask" in chain["quote_note"].lower()

    bars = by_id["alpaca-bars"]
    assert bars["present"] is True
    assert bars["has_quote_spread"] is False
    assert bars["has_iv"] is False


def test_parquet_store_absent_is_reported_not_crashed(legacy_only, client):
    """It is empty on this machine. The viewer must degrade, not explode."""
    r = client.get("/api/cache/options/stores")
    assert r.status_code == 200
    pq = {s["id"]: s for s in r.json()["stores"]}["tastytrade-parquet"]
    assert pq["present"] is False
    assert pq["symbols"] == 0
    assert "warm_options_history" in pq["absent_reason"]
    # its declared capabilities are still truthful: this is the store that WOULD have iv/oi
    assert pq["has_iv"] is True
    assert pq["has_open_interest"] is True


def test_chain_from_an_absent_store_404s_with_the_reason(legacy_only, client):
    r = client.get("/api/cache/options/chain",
                   params={"symbol": "AAPL", "as_of": AS_OF, "store": "tastytrade-parquet"})
    assert r.status_code == 404
    assert "warm_options_history" in r.json()["detail"]


def test_parquet_store_present_is_reported(both_stores, client):
    pq = {s["id"]: s for s in client.get("/api/cache/options/stores").json()["stores"]}[
        "tastytrade-parquet"]
    assert pq["present"] is True
    assert pq["symbols"] == 1
    assert pq["absent_reason"] is None


def test_unknown_store_is_rejected(legacy_only, client):
    r = client.get("/api/cache/options/chain",
                   params={"symbol": "AAPL", "as_of": AS_OF, "store": "bogus"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Symbol search + the as-of dates that actually exist
# ---------------------------------------------------------------------------

def test_symbol_search_is_prefix_matched_and_names_the_stores(legacy_only, client):
    r = client.get("/api/cache/options/symbols", params={"q": "aap"})
    assert r.status_code == 200
    syms = r.json()["symbols"]
    assert [s["symbol"] for s in syms] == ["AAPL"]
    assert "alpaca-chain" in syms[0]["stores"]
    assert "alpaca-bars" in syms[0]["stores"]
    assert [s["symbol"] for s in
            client.get("/api/cache/options/symbols", params={"q": "MS"}).json()["symbols"]] == ["MSFT"]


def test_available_dates_are_only_the_dates_that_exist(legacy_only, client):
    r = client.get("/api/cache/options/dates", params={"symbol": "AAPL"})
    assert r.status_code == 200
    stores = r.json()["stores"]
    chain_dates = [d["as_of"] for d in stores["alpaca-chain"]["dates"]]
    assert chain_dates == [OTHER_AS_OF, AS_OF]        # ascending, and NOTHING else
    assert stores["alpaca-chain"]["dates"][1]["rows"] == 13
    # the bar store's dates are its own — a superset in date terms, disjoint in kind
    assert [d["as_of"] for d in stores["alpaca-bars"]["dates"]] == ["2026-06-09", "2026-06-10"]
    assert stores["tastytrade-parquet"]["present"] is False
    assert stores["tastytrade-parquet"]["dates"] == []


def test_asking_for_an_as_of_with_no_data_names_the_ones_that_have_it(legacy_only, client):
    r = client.get("/api/cache/options/chain",
                   params={"symbol": "AAPL", "as_of": "2025-03-14", "store": "alpaca-chain"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "2025-03-14" in detail
    assert OTHER_AS_OF in detail and AS_OF in detail


def test_unknown_symbol_says_so(legacy_only, client):
    r = client.get("/api/cache/options/chain",
                   params={"symbol": "ZZZZ", "as_of": AS_OF, "store": "alpaca-chain"})
    assert r.status_code == 404
    assert "ZZZZ" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The chain layout: strikes down the middle, calls left, puts right
# ---------------------------------------------------------------------------

def test_chain_groups_by_expiry_with_dte_measured_from_as_of(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    assert [e["expiry"] for e in body["expiries"]] == [EXPIRY_NEAR, EXPIRY_FAR]
    # DTE is expiry - as_of. NOT expiry - today: a cache viewer reads a snapshot.
    assert [e["dte"] for e in body["expiries"]] == [10, 38]


def test_every_leg_belongs_to_the_expiry_group_it_is_filed_under(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    for group in body["expiries"]:
        for row in group["rows"]:
            for leg in (row["call"], row["put"]):
                if leg is None:
                    continue
                assert leg["expiry"] == group["expiry"]
                # and the OCC symbol itself encodes that expiry — a cross-check the
                # grouping code cannot fake
                y, m, d = group["expiry"].split("-")
                assert leg["occ_symbol"][4:10] == f"{y[2:]}{m}{d}"


def test_calls_are_not_transposed_with_puts(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    near = body["expiries"][0]
    # 220 is the listed-but-unpriced call; it has no put, hence the loop below
    assert [r["strike"] for r in near["rows"]] == [190.0, 200.0, 210.0, 220.0]
    for row in near["rows"]:
        if row["put"] is None:
            continue
        assert row["call"]["option_type"] == "call"
        assert row["put"]["option_type"] == "put"
        # the OCC right character is the ground truth the layout must agree with
        assert row["call"]["occ_symbol"][10] == "C"
        assert row["put"]["occ_symbol"][10] == "P"
        assert row["call"]["strike"] == row["put"]["strike"] == row["strike"]
    # and the prices did not swap sides: the 190 call is deep ITM, the 190 put is not
    assert near["rows"][0]["call"]["close"]["value"] == 12.5
    assert near["rows"][0]["put"]["close"]["value"] == 1.75


@pytest.mark.parametrize("store,as_of", [("alpaca-chain", AS_OF), ("alpaca-bars", "2026-06-10")])
def test_every_leg_names_the_store_it_came_from(legacy_only, client, store, as_of):
    """Two stores share one sqlite file and disagree about what they know. A row that does
    not say which one it came from cannot be judged."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": as_of, "store": store}).json()
    assert body["store"] == store
    seen = 0
    for group in body["expiries"]:
        for row in group["rows"]:
            for leg in (row["call"], row["put"]):
                if leg is not None:
                    assert leg["store"] == store
                    seen += 1
    assert seen > 0


def test_expiries_and_strikes_are_both_ascending(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    expiries = [e["expiry"] for e in body["expiries"]]
    assert expiries == sorted(expiries)
    for group in body["expiries"]:
        strikes = [r["strike"] for r in group["rows"]]
        assert strikes == sorted(strikes)
        assert len(strikes) == len(set(strikes))       # one row per strike, never two


def test_a_strike_with_only_one_side_leaves_the_other_empty(legacy_only, client):
    """OTHER_AS_OF holds a lone call. The put slot is None — not a zero-filled row."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": OTHER_AS_OF,
                              "store": "alpaca-chain"}).json()
    row = body["expiries"][0]["rows"][0]
    assert row["call"] is not None
    assert row["put"] is None


# ---------------------------------------------------------------------------
# THE HONESTY CONTRACT
# ---------------------------------------------------------------------------

def test_legacy_chain_never_presents_bid_ask_as_a_spread(legacy_only, client):
    """bid == ask == close in every quoted row. There is no spread to show."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    assert body["columns"]["quote"] == "close"      # a single close column, not bid/ask
    assert body["columns"]["has_quote_spread"] is False
    leg = body["expiries"][0]["rows"][0]["call"]
    # No bid/ask keys are offered at all for this store...
    assert "bid" not in leg and "ask" not in leg
    # ...and the close is labelled for what it is.
    assert leg["close"]["value"] == 12.5
    assert leg["close"]["source"] == "cache"
    note = " ".join(body["notes"]).lower()
    assert "bid" in note and "ask" in note and "spread" in note


def test_the_single_close_is_the_last_trade_not_the_bid(legacy_only, client):
    """The seed's 185 call has bid 2.90 / ask 3.10 / last 3.00 — synthetic, because no row
    in the real file differs. It pins which column the one price column reads."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": OTHER_AS_OF,
                              "store": "alpaca-chain"}).json()
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[185.0]["call"]
    assert leg["close"]["value"] == 3.0
    assert leg["close"]["value"] != 2.9        # not the bid
    assert leg["close"]["value"] != 3.1        # not the ask


@pytest.mark.parametrize("store,as_of", [("alpaca-chain", AS_OF), ("alpaca-bars", "2026-06-10")])
def test_no_store_here_claims_a_bid_ask_spread(legacy_only, client, store, as_of):
    """None of these caches records a two-sided quote. Not one of them may imply otherwise."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": as_of, "store": store}).json()
    assert body["columns"]["has_quote_spread"] is False
    for group in body["expiries"]:
        for row in group["rows"]:
            for leg in (row["call"], row["put"]):
                if leg is not None:
                    assert "bid" not in leg and "ask" not in leg


def test_parquet_store_also_claims_no_spread(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    assert body["columns"]["has_quote_spread"] is False
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[200.0]["call"]
    assert "bid" not in leg and "ask" not in leg


def test_a_listed_but_unpriced_contract_shows_no_price_not_a_zero_price(legacy_only, client):
    """36% of the real file's chain rows have NULL bid/ask/last. A free option is a very
    different claim from an unpriced one, and the row must not make the first."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[220.0]["call"]
    assert leg["close"]["value"] is None
    assert leg["close"]["source"] == "unavailable"
    assert "357,211" in leg["close"]["reason"]
    # the contract is still LISTED — it just has no price
    assert leg["occ_symbol"].endswith("C00220000")


def test_null_iv_renders_unavailable_with_a_reason_never_zero(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    iv = body["expiries"][0]["rows"][0]["call"]["iv"]
    assert iv["value"] is None
    assert iv["value"] != 0 and iv["value"] != 0.0
    assert iv["source"] == "unavailable"
    assert iv["reason"] and "null" in iv["reason"].lower()


def test_null_open_interest_renders_unavailable_with_a_reason_never_zero(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    oi = body["expiries"][0]["rows"][0]["call"]["open_interest"]
    assert oi["value"] is None
    assert oi["source"] == "unavailable"
    assert oi["reason"]
    # "no open interest recorded" is not "zero open interest"
    assert "0" != str(oi["value"])


def test_greeks_are_not_computable_without_iv(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain", "spot": 199.0}).json()
    leg = body["expiries"][0]["rows"][0]["call"]
    for g in ("delta", "gamma", "theta", "vega"):
        assert leg[g]["value"] is None
        assert leg[g]["source"] == "unavailable"
        assert "implied volatility" in leg[g]["reason"].lower()
    assert body["columns"]["greeks"] == "unavailable"


def test_iv_median_is_labelled_derived_and_is_not_called_ivx(legacy_only, client):
    """A broker's IVX is a vendor index. A median over cached rows is not that."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "alpaca-chain"}).json()
    grp = body["expiries"][0]
    assert grp["iv_median"]["value"] is None
    assert grp["iv_median"]["source"] == "unavailable"
    assert "ivx" not in str(body).lower()


def test_bar_store_shows_ohlc_and_volume_and_no_quotes_or_greeks(legacy_only, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": "2026-06-10",
                              "store": "alpaca-bars"}).json()
    assert body["columns"]["quote"] == "ohlc"
    row = body["expiries"][0]["rows"][0]
    assert row["call"]["close"]["value"] == pytest.approx(12.6)
    assert row["call"]["open"]["value"] == pytest.approx(12.5)
    assert row["call"]["volume"]["value"] == 42
    assert row["call"]["volume"]["source"] == "cache"
    assert row["call"]["iv"]["source"] == "unavailable"
    assert row["call"]["open_interest"]["source"] == "unavailable"
    assert "bid" not in row["call"]


# ---------------------------------------------------------------------------
# The parquet store: the only one with real IV / OI, and the only one where a
# greek can be computed at all.
# ---------------------------------------------------------------------------

def test_parquet_iv_and_open_interest_come_through_as_cache_values(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    rows = {r["strike"]: r for r in body["expiries"][0]["rows"]}
    call200 = rows[200.0]["call"]
    assert call200["iv"]["value"] == pytest.approx(0.2841)
    assert call200["iv"]["source"] == "cache"
    assert call200["open_interest"]["value"] == 3410
    assert call200["open_interest"]["source"] == "cache"


def test_a_recorded_zero_open_interest_is_not_the_same_as_no_record(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    rows = {r["strike"]: r for r in body["expiries"][0]["rows"]}
    recorded_zero = rows[210.0]["call"]["open_interest"]
    no_record = rows[200.0]["put"]["open_interest"]
    assert recorded_zero["value"] == 0
    assert recorded_zero["source"] == "cache"
    assert no_record["value"] is None
    assert no_record["source"] == "unavailable"
    assert recorded_zero != no_record


def test_the_parquet_chain_shows_only_the_as_of_date_asked_for(both_stores, client):
    """The 200 call has a bar on AS_OF (close 5.50) and another the next day (6.25)."""
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    call200 = {r["strike"]: r for r in body["expiries"][0]["rows"]}[200.0]["call"]
    assert call200["close"]["value"] == pytest.approx(5.5)
    assert call200["close"]["value"] != pytest.approx(6.25)
    assert body["contracts"] == 4                    # not 5: the next day's bar is excluded

    nxt = client.get("/api/cache/options/chain",
                     params={"symbol": "AAPL", "as_of": NEXT_DAY,
                             "store": "tastytrade-parquet"}).json()
    assert nxt["contracts"] == 1
    assert nxt["expiries"][0]["rows"][0]["call"]["close"]["value"] == pytest.approx(6.25)


def test_parquet_as_of_with_no_rows_names_the_dates_that_have_them(both_stores, client):
    r = client.get("/api/cache/options/chain",
                   params={"symbol": "AAPL", "as_of": "2026-01-05",
                           "store": "tastytrade-parquet"})
    assert r.status_code == 404
    assert AS_OF in r.json()["detail"] and NEXT_DAY in r.json()["detail"]


def test_parquet_dates_are_listed_for_the_picker(both_stores, client):
    stores = client.get("/api/cache/options/dates", params={"symbol": "AAPL"}).json()["stores"]
    pq = stores["tastytrade-parquet"]
    assert pq["present"] is True
    assert [d["as_of"] for d in pq["dates"]] == [AS_OF, NEXT_DAY]
    assert [d["rows"] for d in pq["dates"]] == [4, 1]


def test_parquet_row_with_null_iv_still_reports_iv_unavailable(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    rows = {r["strike"]: r for r in body["expiries"][0]["rows"]}
    iv = rows[210.0]["call"]["iv"]
    assert iv["value"] is None
    assert iv["source"] == "unavailable"


def test_greeks_need_a_spot_and_say_so_when_they_have_none(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    assert body["spot"]["value"] is None
    assert "spot" in body["spot"]["reason"].lower()
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[200.0]["call"]
    assert leg["delta"]["value"] is None
    assert leg["delta"]["source"] == "unavailable"
    assert "spot" in leg["delta"]["reason"].lower()


def test_a_computed_greek_is_labelled_computed_not_exchange_data(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet", "spot": 199.0}).json()
    assert body["columns"]["greeks"] == "computed"
    assert body["spot"]["source"] == "user-supplied"
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[200.0]["call"]
    for g in ("delta", "gamma", "theta", "vega"):
        assert leg[g]["value"] is not None
        # NOT "cache" and NOT "exchange": this is model output.
        assert leg[g]["source"] == "computed"
        assert leg[g]["source"] != "cache"
    assert "black" in body["greeks_model"].lower()
    note = " ".join(body["notes"]).lower()
    assert "computed" in note and "not exchange" in note


def test_computed_greeks_are_the_shared_black_scholes_not_a_second_one(both_stores, client):
    """Uses ba2_common.core.finance_calc.derivatives.black_scholes — verified by value."""
    from ba2_common.core.finance_calc.derivatives import black_scholes

    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF, "store": "tastytrade-parquet",
                              "spot": 199.0, "rate": 0.04}).json()
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[200.0]["call"]
    years = (date.fromisoformat(EXPIRY_NEAR) - date.fromisoformat(AS_OF)).days / 365.0
    expected = black_scholes(199.0, 200.0, years, 0.04, 0.2841, option_type="call")
    assert leg["delta"]["value"] == pytest.approx(expected["delta"])
    assert leg["gamma"]["value"] == pytest.approx(expected["gamma"])
    assert leg["theta"]["value"] == pytest.approx(expected["theta_per_day"])
    assert leg["vega"]["value"] == pytest.approx(expected["vega_per_point"])


def test_computed_put_greeks_are_the_shared_black_scholes_too(both_stores, client):
    """A put is not a call with the sign flipped by hand. Pinning both sides stops the
    option_type ever being hard-coded on the way into the model."""
    from ba2_common.core.finance_calc.derivatives import black_scholes

    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF, "store": "tastytrade-parquet",
                              "spot": 199.0, "rate": 0.04}).json()
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[200.0]["put"]
    years = (date.fromisoformat(EXPIRY_NEAR) - date.fromisoformat(AS_OF)).days / 365.0
    expected = black_scholes(199.0, 200.0, years, 0.04, 0.3102, option_type="put")
    assert leg["delta"]["value"] == pytest.approx(expected["delta"])
    assert leg["delta"]["value"] < 0                      # a put delta is negative
    assert leg["theta"]["value"] == pytest.approx(expected["theta_per_day"])


def test_the_rate_the_greeks_were_priced_with_is_reported_and_defaults_to_zero(both_stores, client):
    """Every greek moves with the rate, and the rate is an ASSUMPTION this viewer supplies —
    not something any cache recorded. It has to be visible, or the numbers are unfalsifiable."""
    def fetch(**extra):
        return client.get("/api/cache/options/chain",
                          params={"symbol": "AAPL", "as_of": AS_OF,
                                  "store": "tastytrade-parquet", "spot": 199.0,
                                  **extra}).json()

    default = fetch()
    assert default["greeks_inputs"]["rate"] == 0.0
    assert default["greeks_inputs"]["dividend_yield"] == 0.0
    assert default["greeks_inputs"]["day_count"] == "actual/365"

    bumped = fetch(rate=0.25)
    assert bumped["greeks_inputs"]["rate"] == 0.25
    d0 = {r["strike"]: r for r in default["expiries"][0]["rows"]}[200.0]["call"]["delta"]["value"]
    d1 = {r["strike"]: r for r in bumped["expiries"][0]["rows"]}[200.0]["call"]["delta"]["value"]
    assert d0 != d1
    assert str(bumped["greeks_inputs"]["rate"]) in " ".join(bumped["notes"]) or "0.2500" in " ".join(bumped["notes"])


def test_a_strike_whose_iv_is_null_gets_no_greeks_even_when_spot_is_known(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet", "spot": 199.0}).json()
    leg = {r["strike"]: r for r in body["expiries"][0]["rows"]}[210.0]["call"]
    assert leg["delta"]["value"] is None
    assert "implied volatility" in leg["delta"]["reason"].lower()


def test_parquet_iv_median_is_derived_from_the_rows_that_have_one(both_stores, client):
    body = client.get("/api/cache/options/chain",
                      params={"symbol": "AAPL", "as_of": AS_OF,
                              "store": "tastytrade-parquet"}).json()
    grp = body["expiries"][0]
    # median of {0.2841, 0.3102, 0.9}; the NULL-iv strike contributes nothing, and the
    # value is the MIDDLE one — not the mean (0.4981), which the skew would drag up.
    assert grp["iv_median"]["value"] == pytest.approx(0.3102)
    assert grp["iv_median"]["value"] != pytest.approx((0.2841 + 0.3102 + 0.9) / 3)
    assert grp["iv_median"]["source"] == "derived"
    assert "median" in grp["iv_median"]["reason"].lower()
    assert "3" in grp["iv_median"]["reason"]     # says HOW MANY rows it summarised


# ---------------------------------------------------------------------------
# Read-only. Non-negotiable.
# ---------------------------------------------------------------------------

def test_the_viewer_never_writes_to_the_cache_database(legacy_only, client):
    db = legacy_only["db"]
    before = hashlib.sha256(open(db, "rb").read()).hexdigest()
    before_files = sorted(os.listdir(os.path.dirname(db)))
    client.get("/api/cache/options/stores")
    client.get("/api/cache/options/symbols", params={"q": "AAPL"})
    client.get("/api/cache/options/dates", params={"symbol": "AAPL"})
    client.get("/api/cache/options/chain",
               params={"symbol": "AAPL", "as_of": AS_OF, "store": "alpaca-chain"})
    client.get("/api/cache/options/chain",
               params={"symbol": "AAPL", "as_of": "2026-06-10", "store": "alpaca-bars"})
    assert hashlib.sha256(open(db, "rb").read()).hexdigest() == before
    # no -wal/-shm sidecars either: a read-only connection creates neither
    assert sorted(os.listdir(os.path.dirname(db))) == before_files


def test_the_service_refuses_to_guess_the_pricing_assumptions(legacy_only):
    """``rate``/``dividend_yield`` are assumptions, not data. The service takes no default
    for them, so the ONE default lives at the HTTP boundary where it is documented — and
    a change to it is visible in ``greeks_inputs`` (see the test above)."""
    import inspect

    from app.services import option_cache_reader
    sig = inspect.signature(option_cache_reader.chain)
    for name in ("rate", "dividend_yield", "spot"):
        assert sig.parameters[name].default is inspect.Parameter.empty, name
    with pytest.raises(TypeError):
        option_cache_reader.chain("AAPL", AS_OF, "alpaca-chain")


def test_the_connection_itself_is_read_only(legacy_only):
    """Belt and braces: the handle the reader opens must REFUSE a write."""
    from app.services import option_cache_reader
    with option_cache_reader.open_legacy_readonly(legacy_only["db"]) as cx:
        with pytest.raises(sqlite3.OperationalError):
            cx.execute("CREATE TABLE scribble(x)")


def test_reader_does_not_use_the_writing_cache_class(legacy_only):
    """OptionsHistoryCache.__init__ runs CREATE TABLE / ALTER TABLE / CREATE INDEX on the
    file it is handed. Importing it here would put a write path one typo away from the
    4.12 GB production cache."""
    import inspect

    from app.services import option_cache_reader
    src = inspect.getsource(option_cache_reader)
    assert "OptionsHistoryCache" not in src
