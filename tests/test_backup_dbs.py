"""tools/backup_dbs.py: the online copy is archived under the source name, the temp copy (and its
WAL/SHM sidecars) never survives the run, and retention keeps the newest N by filename date."""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import backup_dbs  # noqa: E402


@pytest.fixture
def wal_db(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "dl_forecasting.db"
    src.parent.mkdir()
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    con.commit()
    # keep a writer open with a WAL, like the live platform does
    con.execute("INSERT INTO t VALUES (-1)")
    con.commit()
    yield src
    con.close()


def test_backup_one_archives_under_source_name_and_leaves_no_temp(wal_db: Path, tmp_path: Path):
    dest, tmp = tmp_path / "dest", tmp_path / "tmp"
    dest.mkdir(); tmp.mkdir()
    day = dt.date(2026, 9, 15)
    assert backup_dbs.backup_one("test", wal_db, dest, tmp, keep=7, day=day, dry_run=False) is True
    final = dest / "test_2026-09-15.sqlite.zip"
    assert final.is_file()
    assert not list(dest.glob("*.part"))
    assert list(tmp.iterdir()) == [], "temp copy or its -wal/-shm sidecars survived"
    with zipfile.ZipFile(final) as z:
        assert z.namelist() == ["dl_forecasting.db"]
        z.extractall(tmp_path / "restore")
    with sqlite3.connect(tmp_path / "restore" / "dl_forecasting.db") as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 1001
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_prune_keeps_newest_by_filename_date(tmp_path: Path):
    for d in ("2026-09-01", "2026-09-03", "2026-09-02", "2026-09-10"):
        (tmp_path / f"test_{d}.sqlite.zip").write_bytes(b"x")
    (tmp_path / "prod_2026-08-01.sqlite.zip").write_bytes(b"x")  # other name: untouched
    victims = backup_dbs.prune(tmp_path, "test", keep=2)
    assert sorted(v.name for v in victims) == ["test_2026-09-01.sqlite.zip", "test_2026-09-02.sqlite.zip"]
    assert sorted(p.name for p in tmp_path.glob("*.zip")) == [
        "prod_2026-08-01.sqlite.zip", "test_2026-09-03.sqlite.zip", "test_2026-09-10.sqlite.zip"]


def test_missing_source_is_reported_not_raised(tmp_path: Path):
    assert backup_dbs.backup_one("prod", tmp_path / "nope.sqlite", tmp_path, tmp_path, 7,
                                 dt.date.today(), dry_run=False) is False
