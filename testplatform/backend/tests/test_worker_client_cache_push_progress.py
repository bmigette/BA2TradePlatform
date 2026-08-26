"""worker_client._iter_with_progress — upload-progress logging for push_cache.

A cache push to a remote worker reachable only over the internet (not a LAN) can run for a
long time on a slow link; before this, push_cache logged once before the transfer and once
after, silent for the whole duration in between -- indistinguishable in the log from a hang.
This proves the pass-through wrapper logs periodically without altering the byte stream.
"""
from app.services import worker_client


def test_iter_with_progress_yields_every_chunk_unchanged():
    chunks = [b"a" * 10, b"b" * 20, b"c" * 5]
    out = list(worker_client._iter_with_progress(chunks, total_bytes=35,
                                                 worker_name="w1", log=lambda _m: None))
    assert out == chunks  # pure pass-through: the wrapper must never alter the stream


def test_iter_with_progress_logs_periodically_not_per_chunk(monkeypatch):
    # Every time.monotonic() call (one per chunk) advances by a full interval, so each of the
    # 4 chunks crosses the log threshold -- deterministic, no reliance on real wall-clock time.
    calls = [0]

    def fake_monotonic():
        calls[0] += 1
        return 1000.0 + calls[0] * worker_client._PUSH_PROGRESS_LOG_INTERVAL_S

    monkeypatch.setattr(worker_client.time, "monotonic", fake_monotonic)
    logged = []
    chunks = [b"x" * 25 for _ in range(4)]

    list(worker_client._iter_with_progress(chunks, total_bytes=100,
                                           worker_name="remote1", log=logged.append))

    assert len(logged) == 4, logged
    assert all("remote1" in m for m in logged), logged
    # Percent climbs monotonically and the final line reports full completion.
    assert "100.0%" in logged[-1], logged[-1]


def test_iter_with_progress_does_not_log_when_clock_never_advances(monkeypatch):
    """No progress worth reporting inside one interval -> no log line at all (not a partial/
    misleading one)."""
    monkeypatch.setattr(worker_client.time, "monotonic", lambda: 1000.0)  # frozen clock
    logged = []
    list(worker_client._iter_with_progress([b"x" * 10], total_bytes=10,
                                           worker_name="w1", log=logged.append))
    assert logged == []


def test_iter_with_progress_handles_zero_total_bytes_without_dividing_by_zero(monkeypatch):
    calls = [0]

    def fake_monotonic():
        calls[0] += 1
        return 1000.0 + calls[0] * worker_client._PUSH_PROGRESS_LOG_INTERVAL_S

    monkeypatch.setattr(worker_client.time, "monotonic", fake_monotonic)
    logged = []
    list(worker_client._iter_with_progress([b""], total_bytes=0,
                                           worker_name="w1", log=logged.append))
    assert logged == ["cache push -> w1: 0.0 kB/0.0 kB (100.0%)"]
