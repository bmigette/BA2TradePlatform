"""Live raw-bar client: request shape, paging, loud failures (fake transport, no network)."""
import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveBars, AlpacaLiveFetchError


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    def get(self, url, headers):
        self.calls += 1
        u = urlparse(url)
        self.requests.append({k: v[0] for k, v in parse_qs(u.query).items()})
        status, payload = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return status, (payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def _bar(t, o=10.0, h=11.0, l=9.0, c=10.5, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "n": 5, "vw": 10.2}


def _page(sym, bars, token=None):
    return (200, {"bars": {sym: bars}, "next_page_token": token})


def client(transport, sleeps=None):
    return AlpacaLiveBars("k", "s", transport=transport,
                          sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


D0, D1 = date(2026, 10, 19), date(2026, 10, 24)


def test_daily_raw_request_shape_and_symbol_translation():
    ft = FakeTransport([_page("BRK.B", [_bar("2026-10-19T04:00:00Z")])])
    client(ft).daily("BRK-B", D0, D1)
    q = ft.requests[0]
    assert q["adjustment"] == "raw" and q["feed"] == "sip" and q["timeframe"] == "1Day"
    assert q["symbols"] == "BRK.B"
    assert q["start"] == "2026-10-19T04:00:00Z" and q["end"] == "2026-10-24T03:59:59Z"


def test_native_layout_is_the_builds():
    ft = FakeTransport([_page("AAA", [_bar("2026-10-19T04:00:00Z"), _bar("2026-10-20T04:00:00Z")])])
    df = client(ft).daily("AAA", D0, D1)
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert df["Date"].dt.tz is None and df["Date"].iloc[0] == pd.Timestamp("2026-10-19")


def test_5min_is_regular_session_only_ny_naive():
    bars = [_bar("2026-10-19T12:00:00Z"),            # 08:00 NY premarket: dropped
            _bar("2026-10-19T13:30:00Z"),            # 09:30 NY
            _bar("2026-10-19T19:55:00Z"),            # 15:55 NY
            _bar("2026-10-19T20:00:00Z")]            # 16:00 NY after hours: dropped
    df = client(FakeTransport([_page("AAA", bars)])).intraday_5min("AAA", D0, date(2026, 10, 20))
    assert [str(d) for d in df["Date"]] == ["2026-10-19 09:30:00", "2026-10-19 15:55:00"]


def test_pages_are_followed_and_loop_detected():
    ft = FakeTransport([_page("AAA", [_bar("2026-10-19T04:00:00Z")], "p2"),
                        _page("AAA", [_bar("2026-10-20T04:00:00Z")], None)])
    assert len(client(ft).daily("AAA", D0, D1)) == 2
    assert ft.requests[1]["page_token"] == "p2"
    loop = FakeTransport([_page("AAA", [_bar("2026-10-19T04:00:00Z")], "same")])
    with pytest.raises(AlpacaLiveFetchError, match="pagination loop"):
        client(loop).daily("AAA", D0, D1)


def test_http_500_raises_after_three_tries_with_backoff():
    sleeps = []
    ft = FakeTransport([(500, b"boom")])
    with pytest.raises(AlpacaLiveFetchError, match="500"):
        client(ft, sleeps).daily("AAA", D0, D1)
    assert ft.calls == 3 and len(sleeps) == 2


def test_http_403_is_not_retried():
    ft = FakeTransport([(403, b"forbidden")])
    with pytest.raises(AlpacaLiveFetchError, match="403"):
        client(ft).daily("AAA", D0, D1)
    assert ft.calls == 1


def test_malformed_json_and_shape_refuse():
    with pytest.raises(AlpacaLiveFetchError, match="malformed"):
        client(FakeTransport([(200, b"<html>")])).daily("AAA", D0, D1)
    with pytest.raises(AlpacaLiveFetchError, match="shape"):
        client(FakeTransport([(200, {"oops": 1})])).daily("AAA", D0, D1)


def test_null_bar_values_refuse():
    bad = _bar("2026-10-19T04:00:00Z")
    bad["c"] = None
    with pytest.raises(AlpacaLiveFetchError, match="null"):
        client(FakeTransport([_page("AAA", [bad])])).daily("AAA", D0, D1)


def test_empty_answer_is_an_empty_frame_not_an_error():
    df = client(FakeTransport([(200, {"bars": None, "next_page_token": None})])).daily(
        "AAA", date(2026, 10, 24), date(2026, 10, 26))
    assert df.empty and list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_missing_credentials_refuse():
    with pytest.raises(AlpacaLiveFetchError):
        AlpacaLiveBars("", "s", transport=FakeTransport([]))
