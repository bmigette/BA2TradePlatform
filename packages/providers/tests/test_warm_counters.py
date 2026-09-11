"""Request / byte accounting by purpose (spec section 6, "Operational policy").

"Track requests/bytes by endpoint and purpose: normal live, capture overhead (must be
zero network), and warmup." Nothing measured this before, so "capture adds zero
requests" and "warm stayed inside its allowance" were both unfalsifiable claims.

What the counters have to get right, because a budget is only as honest as its meter:

* EVERY ATTEMPT is a request, including the ones that came back 429 and were retried
  -- those consumed a slot at the provider and are the cause of the throttling;
* BYTES only when a body arrived -- a failed attempt transferred nothing;
* the DOMINANT warm traffic goes through ``fmp_list_call`` (three statement histories
  and the earnings calendar, all via fmpsdk) and FRED, not only through
  ``fmp_http_get``. A meter that saw only the last one governed a minority of the
  bytes it claimed to govern.
"""
import pytest

from ba2_providers import fmp_common


class _Response:
    """The part of a ``requests.Response`` ``fmp_http_get`` reads."""

    def __init__(self, body: bytes, status_code: int = 200):
        self.content = body
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def clean_counters():
    fmp_common.reset_purpose_stats()
    yield
    fmp_common.reset_purpose_stats()


def _get(body=b"x" * 100, endpoint="price-target", responses=None):
    queue = list(responses or [_Response(body)])

    def _getter(url, params=None, timeout=None):
        return queue.pop(0)

    return fmp_common.fmp_http_get(
        "https://example.invalid/api", {"apikey": "secret"}, endpoint=endpoint,
        getter=_getter, sleep=lambda _s: None)


# --------------------------------------------------------------------------- #
# Purpose
# --------------------------------------------------------------------------- #
def test_requests_and_bytes_are_counted_by_purpose_and_endpoint():
    _get(b"a" * 10)
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"b" * 40, endpoint="grades")
        _get(b"c" * 60, endpoint="grades")

    stats = fmp_common.get_purpose_stats()

    assert stats["live"]["requests"] == 1 and stats["live"]["bytes"] == 10
    assert stats["warm"]["requests"] == 2 and stats["warm"]["bytes"] == 100
    assert stats["warm"]["endpoints"]["grades"]["requests"] == 2
    assert "capture" not in stats, "recording must never issue a request of its own"


def test_the_purpose_tag_does_not_leak_out_of_its_context():
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        assert fmp_common.current_fmp_purpose() == "warm"
    assert fmp_common.current_fmp_purpose() == "live"


def test_an_unknown_purpose_is_refused():
    with pytest.raises(ValueError):
        with fmp_common.fmp_purpose("whatever"):
            pass


def test_counters_reset_per_utc_day(monkeypatch):
    day = ["2026-09-11"]
    monkeypatch.setattr(fmp_common, "_utc_day", lambda: day[0])

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"x" * 50)
    assert fmp_common.get_purpose_stats()["warm"]["bytes"] == 50

    day[0] = "2026-09-12"
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(b"x" * 5)

    assert fmp_common.get_purpose_stats()["warm"]["bytes"] == 5, (
        "a daily allowance is measured against a day; yesterday's bytes cannot consume it")


# --------------------------------------------------------------------------- #
# Attempts vs bytes
# --------------------------------------------------------------------------- #
def test_a_rate_limited_attempt_is_counted_as_a_request_but_carries_no_bytes():
    responses = [_Response(b"", status_code=429), _Response(b"y" * 30)]

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        _get(endpoint="grades", responses=responses)

    stats = fmp_common.get_purpose_stats()["warm"]
    assert stats["requests"] == 2, (
        "the throttled attempt consumed a slot at the provider; hiding it hides the cause "
        "of the throttling")
    assert stats["bytes"] == 30


def test_a_response_that_cannot_report_its_size_is_still_counted_as_a_request():
    class _NoBody:
        status_code = 200
        headers = {}
        content = None

        def raise_for_status(self):
            return None

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.fmp_http_get("https://example.invalid/api", endpoint="e",
                                getter=lambda url, params=None, timeout=None: _NoBody(),
                                sleep=lambda _s: None)

    stats = fmp_common.get_purpose_stats()["warm"]
    assert stats["requests"] == 1
    assert stats["bytes"] == 0, "an unmeasurable body is not padded with a guess"


# --------------------------------------------------------------------------- #
# The dominant traffic
# --------------------------------------------------------------------------- #
def test_fmp_list_call_is_counted_too():
    """The statements and the earnings calendar go through fmpsdk, not fmp_http_get."""
    payload = [{"symbol": "AAPL", "value": i} for i in range(50)]

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.fmp_list_call(lambda: payload, symbol="AAPL",
                                 endpoint="balance_sheet_statement")

    stats = fmp_common.get_purpose_stats()["warm"]
    assert stats["endpoints"]["balance_sheet_statement"]["requests"] == 1
    assert stats["bytes"] > 0, "an unmetered statement history is most of a warm's traffic"


def test_a_retried_fmp_list_call_counts_each_attempt():
    payloads = [{"Error Message": "Limit Reach."}, [{"ok": True}]]

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.fmp_list_call(lambda: payloads.pop(0), symbol="AAPL", endpoint="income",
                                 delays=(0,), sleep=lambda _s: None)

    assert fmp_common.get_purpose_stats()["warm"]["endpoints"]["income"]["requests"] == 2


def test_an_empty_fmp_list_call_result_is_a_request_with_no_bytes():
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.fmp_list_call(lambda: None, symbol="AAPL", endpoint="income")

    stats = fmp_common.get_purpose_stats()["warm"]
    assert stats["requests"] == 1 and stats["bytes"] == 0


def test_fred_refreshes_are_counted_in_the_same_ledger(monkeypatch, tmp_path):
    """Nine economy-wide series is real background traffic the allowance must see."""
    import ba2_common.core.native_cache  # noqa: F401 - import order parity with the app
    from ba2_providers.macro import fred_series

    class _Resp:
        content = b"z" * 512

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"observations": [{"date": "2026-09-10", "value": "1.0"}]}

    monkeypatch.setattr(fred_series, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(fred_series.requests, "get", lambda *a, **k: _Resp())

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fred_series.refresh_series("VIXCLS", "a-key")

    stats = fmp_common.get_purpose_stats()["warm"]
    assert stats["endpoints"]["fred-observations"]["requests"] == 1
    assert stats["endpoints"]["fred-observations"]["bytes"] == 512


# --------------------------------------------------------------------------- #
# The endpoint key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "https://financialmodelingprep.com/api/v3/price-target?apikey=SECRET",
    "price-target?apikey=SECRET",
    "x" * 200,
])
def test_a_url_or_an_empty_name_is_refused_as_a_counter_key(bad):
    """The key must not carry a symbol -- or, on some FMP paths, the API key."""
    with pytest.raises(ValueError):
        fmp_common.record_fmp_request(bad)
