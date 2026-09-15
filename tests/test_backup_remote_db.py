"""tools/backup_remote_db.py: the remote program produces a self-contained archive under the
source name and leaves no copy behind; the driver publishes via .part, verifies the size, always
cleans the remote, and reports (not raises) a failed remote step."""
from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import backup_remote_db as brd  # noqa: E402


def test_remote_program_archives_wal_db_and_cleans_up(tmp_path: Path):
    db = tmp_path / "dl_forecasting.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    con.commit()  # writer stays open, WAL populated
    out = tmp_path / "bk" / "x.sqlite.zip"
    r = subprocess.run([sys.executable, "-", str(db), str(out)], input=brd._REMOTE_PROGRAM,
                       capture_output=True, text=True)
    con.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("OK ")
    assert int(r.stdout.split()[1]) == out.stat().st_size
    assert sorted(p.name for p in out.parent.iterdir()) == ["x.sqlite.zip"]
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["dl_forecasting.db"]
        z.extractall(tmp_path / "restore")
    with sqlite3.connect(tmp_path / "restore" / "dl_forecasting.db") as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 500
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_backup_one_fetches_via_part_and_prunes(tmp_path: Path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    for d in ("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"):
        (dest / f"r_{d}.sqlite.zip").write_bytes(b"x")
    calls = []
    monkeypatch.setattr(brd, "remote_archive", lambda host, db, out: calls.append(("build", out)) or 5)

    def fake_fetch(host, remote_path, final):
        assert final.name == "r_2026-08-29.sqlite.zip"
        final.write_bytes(b"12345")
        return 5

    monkeypatch.setattr(brd, "fetch", fake_fetch)
    monkeypatch.setattr(brd, "remote_cleanup", lambda host, p: calls.append(("rm", p)))
    spec = {"host": "h", "db": "/x/dl_forecasting.db", "remote_tmp": "/x/bk"}
    assert brd.backup_one("r", spec, dest, keep=4, day=dt.date(2026, 8, 29), dry_run=False) is True
    assert ("rm", "/x/bk/r_2026-08-29.sqlite.zip") in calls
    assert sorted(p.name for p in dest.glob("r_*.zip")) == [
        "r_2026-08-08.sqlite.zip", "r_2026-08-15.sqlite.zip", "r_2026-08-22.sqlite.zip",
        "r_2026-08-29.sqlite.zip"]


def test_failed_remote_step_is_reported_and_remote_cleaned(tmp_path: Path, monkeypatch):
    cleaned = []

    def boom(host, db, out):
        raise RuntimeError("remote step rc=255: ssh: connect timed out")

    monkeypatch.setattr(brd, "remote_archive", boom)
    monkeypatch.setattr(brd, "remote_cleanup", lambda host, p: cleaned.append(p))
    spec = {"host": "h", "db": "/x/dl_forecasting.db", "remote_tmp": "/x/bk"}
    assert brd.backup_one("r", spec, tmp_path, 4, dt.date(2026, 8, 29), dry_run=False) is False
    assert cleaned == ["/x/bk/r_2026-08-29.sqlite.zip"]
    assert not list(tmp_path.glob("*.zip"))
