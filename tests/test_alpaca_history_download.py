"""tools/alpaca_history_download.py: a chunk is stored whole and verified, or not at all."""
import datetime as dt
import gzip
import importlib.util
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "alpaca_history_download", Path(__file__).resolve().parents[1] / "tools" / "alpaca_history_download.py")
dl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dl)

UTC = dt.timezone.utc
START = dt.datetime(2019, 12, 30, tzinfo=UTC)
END = dt.datetime(2020, 1, 4, tzinfo=UTC)
DAYS = [dt.date(2019, 12, 30), dt.date(2019, 12, 31), dt.date(2020, 1, 2), dt.date(2020, 1, 3)]


def _bar(moment):
    return {"t": moment.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5,
            "v": 100, "n": 3, "vw": 10.2}


def _daily(days):
    return [_bar(dt.datetime(day.year, day.month, day.day, 5, tzinfo=UTC)) for day in days]


def _intraday(days, bars_per_day=4):
    out = []
    for day in days:
        first = dt.datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
        out.extend(_bar(first + dt.timedelta(minutes=5 * index)) for index in range(bars_per_day))
    return out


class _Response:
    def __init__(self, payload, headers=None):
        self._raw = gzip.compress(json.dumps(payload).encode())
        self.headers = dict({"Content-Encoding": "gzip", "X-RateLimit-Remaining": "150",
                             "X-RateLimit-Reset": "0"}, **(headers or {}))

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAlpaca:
    """Serves bars the way the vendor does: inclusive `end`, `page_token`, one symbol per request."""

    def __init__(self, page_size=3):
        self.bars = {}          # (symbol, timeframe) -> list of bars
        self.page_size = page_size
        self.faults = []        # consumed one per request, before the real answer
        self.fail_after = None  # every request from this call index on raises URLError
        self.calls = []

    def __call__(self, request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(url.query).items()}
        self.calls.append((url.path, query))
        assert request.get_header("Apca-api-key-id") == "KEY"
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise urllib.error.URLError("connection reset")
        if self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, Exception):
                raise fault
            code, headers = fault
            raise urllib.error.HTTPError(request.full_url, code, "fault", headers, io.BytesIO(b"fault body"))
        rows = [bar for bar in self.bars.get((query["symbols"], query["timeframe"]), [])
                if query["start"] <= bar["t"] <= query["end"]]
        offset = int(query.get("page_token", 0))
        page = rows[offset:offset + self.page_size]
        token = str(offset + self.page_size) if offset + self.page_size < len(rows) else None
        return _Response({"bars": {query["symbols"]: page} if page else {}, "next_page_token": token})


def _run(tmp_path, fake, *, end=END, retry_refused=False, max_attempts=3, sleeps=None, max_gap_days=0):
    transport = dl.Transport("KEY", "SECRET", rpm=1e9, max_attempts=max_attempts, min_remaining=10,
                             opener=fake, sleep=(sleeps.append if sleeps is not None else lambda _s: None))
    asof = dl.open_store(tmp_path, START, None, "2020-01-03")
    run = dl.Downloader(tmp_path, transport, start=START, end_exclusive=end, asof=asof,
                        retry_refused=retry_refused, max_consecutive_failures=99, max_gap_days=max_gap_days)
    run.daily("ABC")
    run.intraday("ABC")
    return run


def _fake(intraday_days=DAYS, page_size=3):
    fake = FakeAlpaca(page_size)
    fake.bars[("ABC", "1Day")] = _daily(DAYS)
    fake.bars[("ABC", "5Min")] = _intraday(intraday_days)
    return fake


def _chunks(tmp_path):
    return json.loads((tmp_path / "manifest" / "symbol=ABC.json").read_text())["chunks"]


def _files(tmp_path):
    return sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / "bars").rglob("*") if path.is_file())


def test_pages_are_joined_and_each_year_is_one_verified_file(tmp_path):
    run = _run(tmp_path, _fake())
    assert _files(tmp_path) == ["bars/1Day/symbol=ABC/all.parquet", "bars/5Min/symbol=ABC/2019.parquet",
                                "bars/5Min/symbol=ABC/2020.parquet"]
    chunks = _chunks(tmp_path)
    assert {key: entry["status"] for key, entry in chunks.items()} == {
        "1Day/all": "complete", "5Min/2019": "complete", "5Min/2020": "complete"}
    assert chunks["5Min/2019"]["bars"] == 8 and chunks["5Min/2019"]["pages"] == 3
    frame = pd.read_parquet(tmp_path / "bars/5Min/symbol=ABC/2020.parquet")
    assert list(frame.columns) == dl.BAR_COLUMNS and len(frame) == 8
    assert str(frame["t"].dt.tz) == "UTC" and frame["t"].is_monotonic_increasing
    assert chunks["5Min/2020"]["sha256"] == dl.sha256_file(tmp_path / "bars/5Min/symbol=ABC/2020.parquet")
    assert run.counts["written"] == 3 and not list(tmp_path.rglob("*.tmp"))


def test_a_download_that_dies_between_pages_stores_nothing_and_resumes(tmp_path):
    fake = _fake()
    fake.fail_after = 3  # daily: 2 pages; then one intraday page succeeds; every later request fails
    run = _run(tmp_path, fake, max_attempts=2)
    assert _files(tmp_path) == ["bars/1Day/symbol=ABC/all.parquet"]
    assert set(_chunks(tmp_path)) == {"1Day/all"}, "a failed chunk leaves no manifest entry"
    assert run.counts["failed"] == 2
    failures = (tmp_path / "state" / "failures.jsonl").read_text().splitlines()
    assert len(failures) == 2 and "retries exhausted" in failures[0]

    fake.fail_after = None
    before = len(fake.calls)
    run = _run(tmp_path, fake)
    assert run.counts == {"written": 2, "skipped": 1, "refused": 0, "failed": 0, "empty": 0}
    assert all(query["timeframe"] == "5Min" for _path, query in fake.calls[before:]), "the daily chunk is not refetched"


def test_a_finished_store_makes_no_request(tmp_path):
    fake = _fake()
    _run(tmp_path, fake)
    before = len(fake.calls)
    run = _run(tmp_path, fake)
    assert len(fake.calls) == before and run.counts["skipped"] == 3


def test_5xx_and_429_are_retried_with_backoff(tmp_path):
    fake = _fake()
    fake.faults = [(500, {}), (429, {"Retry-After": "7"}), urllib.error.URLError("timed out")]
    sleeps = []
    run = _run(tmp_path, fake, max_attempts=5, sleeps=sleeps)
    assert run.counts["written"] == 3 and run.transport.retries == 3 and run.transport.rate_limited == 1
    assert sleeps[1] == 7.0, "429 waits what the vendor asked for"
    assert 1.5 <= sleeps[0] <= 2.5 and 6.0 <= sleeps[2] <= 10.0, "exponential backoff with jitter"


def test_bad_credentials_stop_the_run(tmp_path):
    fake = _fake()
    fake.faults = [(403, {})]
    with pytest.raises(dl.FatalError):
        _run(tmp_path, fake)
    assert _files(tmp_path) == []


def test_a_year_with_a_day_that_has_no_intraday_bars_is_refused(tmp_path):
    fake = _fake(intraday_days=[DAYS[0], DAYS[2], DAYS[3]])  # 2019-12-31 has a daily bar and no 5-minute bar
    run = _run(tmp_path, fake)
    assert _files(tmp_path) == ["bars/1Day/symbol=ABC/all.parquet", "bars/5Min/symbol=ABC/2020.parquet"]
    refused = _chunks(tmp_path)["5Min/2019"]
    assert refused["status"] == "refused_days_without_intraday_bars"
    assert refused["detail"] == {"count": 1, "of": 2, "days": ["2019-12-31"]}
    assert run.counts["refused"] == 1

    before = len(fake.calls)
    assert _run(tmp_path, fake).counts["skipped"] == 3, "a refused chunk is not fetched again by default"
    assert len(fake.calls) == before
    fake.bars[("ABC", "5Min")] = _intraday(DAYS)
    run = _run(tmp_path, fake, retry_refused=True)
    assert run.counts["written"] == 1 and _chunks(tmp_path)["5Min/2019"]["status"] == "complete"


def test_a_zero_trade_placeholder_day_needs_no_intraday_bars(tmp_path):
    fake = _fake(intraday_days=[DAYS[0], DAYS[2], DAYS[3]])
    placeholder = fake.bars[("ABC", "1Day")][1]  # 2019-12-31: Alpaca's flat bar for a day nothing traded
    placeholder.update(o=10.5, h=10.5, l=10.5, c=10.5, v=0, n=0, vw=0.0)
    run = _run(tmp_path, fake)
    assert run.counts["refused"] == 0
    entry = _chunks(tmp_path)["5Min/2019"]
    assert entry["status"] == "complete"
    assert entry["daily_days"] == 1 and entry["zero_trade_daily_days"] == 1


def test_a_day_with_trades_but_no_volume_is_still_expected(tmp_path):
    fake = _fake(intraday_days=[DAYS[0], DAYS[2], DAYS[3]])
    fake.bars[("ABC", "1Day")][1].update(v=0, n=2)  # not the placeholder shape: the day must have intraday bars
    _run(tmp_path, fake)
    assert _chunks(tmp_path)["5Min/2019"]["status"] == "refused_days_without_intraday_bars"


def test_a_briefly_held_target_file_is_replaced_once_released(tmp_path, monkeypatch):
    real_replace, calls = dl.os.replace, []

    def held_twice(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(dl.os, "replace", held_twice)
    monkeypatch.setattr(dl.time, "sleep", lambda _s: None)
    dl.atomic_write_json(tmp_path / "progress.json", {"a": 1})
    assert json.loads((tmp_path / "progress.json").read_text()) == {"a": 1} and len(calls) == 3


def test_a_target_file_held_for_good_stops_the_run(tmp_path, monkeypatch):
    def always_held(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(dl.os, "replace", always_held)
    monkeypatch.setattr(dl.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        dl.atomic_write_json(tmp_path / "progress.json", {"a": 1})


def test_a_year_missing_few_traded_days_is_stored_with_the_days_listed(tmp_path):
    fake = _fake(intraday_days=[DAYS[0], DAYS[2], DAYS[3]])
    run = _run(tmp_path, fake, max_gap_days=1)
    entry = _chunks(tmp_path)["5Min/2019"]
    assert run.counts["refused"] == 0 and entry["status"] == "complete"
    assert entry["days_without_intraday_bars"] == {"count": 1, "of": 2, "days": ["2019-12-31"]}
    assert _chunks(tmp_path)["5Min/2020"]["days_without_intraday_bars"]["count"] == 0


def test_a_year_missing_more_than_the_limit_is_refused(tmp_path):
    fake = _fake(intraday_days=[DAYS[2], DAYS[3]])  # both 2019 days missing
    _run(tmp_path, fake, max_gap_days=1)
    assert _chunks(tmp_path)["5Min/2019"]["status"] == "refused_days_without_intraday_bars"


def test_a_year_with_no_intraday_bar_at_all_is_refused_even_under_the_limit(tmp_path):
    fake = _fake(intraday_days=[DAYS[2], DAYS[3]])
    _run(tmp_path, fake, max_gap_days=5)
    assert _chunks(tmp_path)["5Min/2019"]["status"] == "refused_days_without_intraday_bars"


def test_a_duplicated_bar_refuses_the_chunk(tmp_path):
    fake = _fake()
    bars = fake.bars[("ABC", "5Min")]
    bars.insert(1, dict(bars[0]))
    _run(tmp_path, fake)
    assert _chunks(tmp_path)["5Min/2019"]["status"] == "refused_duplicate_timestamps"
    assert "bars/5Min/symbol=ABC/2019.parquet" not in _files(tmp_path)


def test_the_bar_on_the_year_boundary_is_stored_once_in_the_new_year(tmp_path):
    fake = _fake()
    boundary = _bar(dt.datetime(2020, 1, 1, tzinfo=UTC))  # 19:00 New York on 31 December
    fake.bars[("ABC", "5Min")] = sorted(fake.bars[("ABC", "5Min")] + [boundary], key=lambda bar: bar["t"])
    _run(tmp_path, fake)
    old = pd.read_parquet(tmp_path / "bars/5Min/symbol=ABC/2019.parquet")
    new = pd.read_parquet(tmp_path / "bars/5Min/symbol=ABC/2020.parquet")
    stamp = pd.Timestamp("2020-01-01T00:00:00Z")
    assert stamp not in set(old["t"]) and stamp in set(new["t"]) and len(old) + len(new) == 17


def test_a_refused_extension_keeps_the_data_already_verified(tmp_path):
    fake = _fake()
    _run(tmp_path, fake)
    stored = dl.sha256_file(tmp_path / "bars/5Min/symbol=ABC/2020.parquet")
    later = dt.datetime(2020, 1, 7, tzinfo=UTC)
    fake.bars[("ABC", "1Day")] = _daily(DAYS + [dt.date(2020, 1, 6)])  # the new day has no 5-minute bars
    run = _run(tmp_path, fake, end=later)
    entry = _chunks(tmp_path)["5Min/2020"]
    assert run.counts["refused"] == 1 and entry["status"] == "complete"
    assert entry["refused_update"]["status"] == "refused_days_without_intraday_bars"
    assert dl.sha256_file(tmp_path / "bars/5Min/symbol=ABC/2020.parquet") == stored


def test_a_symbol_without_daily_bars_gets_no_intraday_request(tmp_path):
    fake = FakeAlpaca()
    run = _run(tmp_path, fake)
    assert _chunks(tmp_path)["1Day/all"]["status"] == "no_data" and _files(tmp_path) == []
    assert [query["timeframe"] for _path, query in fake.calls] == ["1Day"]
    assert run.counts["empty"] == 3  # the daily chunk and both intraday years


def test_one_store_holds_one_basis(tmp_path):
    dl.open_store(tmp_path, START, None, "2020-01-03")
    with pytest.raises(dl.FatalError):
        dl.open_store(tmp_path, START + dt.timedelta(days=1), None, "2020-01-03")
    with pytest.raises(dl.FatalError):
        dl.open_store(tmp_path, START, "2021-01-01", "2020-01-03")
    assert dl.open_store(tmp_path, START, None, "2099-01-01") == "2020-01-03", "asof is pinned at creation"


def test_leftover_temp_files_are_swept(tmp_path):
    leftover = tmp_path / "bars" / "5Min" / "symbol=ABC" / "2019.parquet.tmp"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"half a file")
    assert dl.sweep_temp_files(tmp_path) == 1 and not leftover.exists()


def test_class_share_spelling_and_month_windows():
    assert dl.alpaca_symbol("BRK-B") == "BRK.B"
    assert dl.month_chunks(dt.date(2019, 12, 15), dt.date(2020, 2, 3)) == [
        ("2019-12", dt.date(2019, 12, 15), dt.date(2019, 12, 31)),
        ("2020-01", dt.date(2020, 1, 1), dt.date(2020, 1, 31)),
        ("2020-02", dt.date(2020, 2, 1), dt.date(2020, 2, 2))]
