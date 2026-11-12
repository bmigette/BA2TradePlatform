"""FileRateBudget: per-owner and per-key ceilings over a sliding minute, shared through a file."""
from ba2_providers.av_rate_budget import FileRateBudget


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += max(s, 0.5)


def _budget(path, owner, per_min, key_limit, clk):
    return FileRateBudget(str(path), owner, per_min, key_limit, clock=clk.now, sleep=clk.sleep)


def test_owner_cap_blocks_the_next_call_until_the_window_slides(tmp_path):
    clk = FakeClock()
    b = _budget(tmp_path / "b.json", "a", 3, 75, clk)
    for _ in range(3):
        b.acquire()
    assert clk.slept == []
    b.acquire()                         # 4th inside one minute: must wait
    assert clk.slept and clk.t >= 1060.0


def test_key_limit_is_shared_between_owners(tmp_path):
    clk = FakeClock()
    p = tmp_path / "b.json"
    a, b = _budget(p, "a", 10, 4, clk), _budget(p, "b", 10, 4, clk)
    a.acquire(); a.acquire(); b.acquire(); b.acquire()
    assert clk.slept == []
    a.acquire()                         # key ceiling (4) reached by the OTHER owner's calls too
    assert clk.slept and clk.t >= 1060.0


def test_corrupt_budget_file_is_treated_as_empty(tmp_path):
    clk = FakeClock()
    p = tmp_path / "b.json"
    p.write_text("not json")
    _budget(p, "a", 2, 75, clk).acquire()
    assert clk.slept == []
