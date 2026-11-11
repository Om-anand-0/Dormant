import os
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from dormant.errors import ArchiveNotFoundError, IndexVersionError
from dormant.index import ArchiveRecord, ArchiveState, Index


def make(tmp_path: Path, **kw: object) -> ArchiveRecord:
    rec = ArchiveRecord(
        id=uuid.uuid4().hex,
        project_root=tmp_path / "Proj",
        target=tmp_path / "Proj" / "node_modules",
        kind="dependencies",
        archive_path=tmp_path / "a.tar.zst",
        checksum="ab" * 32,
        source_bytes=1000,
        archive_bytes=400,
        created_at=1_700_000_000.5,
        format_version=1,
        state=ArchiveState.ACTIVE,
    )
    return replace(rec, **kw)  # type: ignore[arg-type]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "index.sqlite3"


def test_round_trip_and_persistence(tmp_path: Path, db: Path) -> None:
    rec = make(tmp_path)
    purged = make(tmp_path, archive_path=None, checksum="", state=ArchiveState.PURGED)
    with Index(db) as idx:
        idx.add(rec)
        idx.add(purged)
        assert idx.get(rec.id) == rec
    with Index(db) as idx:
        assert idx.get(rec.id) == rec
        assert idx.get(purged.id) == purged
        assert idx.get("missing") is None
        assert len(idx.all()) == 2
        assert {r.id for r in idx.for_project(tmp_path / "Proj")} == {rec.id, purged.id}


def test_find_active(tmp_path: Path, db: Path) -> None:
    rec = make(tmp_path)
    with Index(db) as idx:
        assert idx.find_active(rec.target) is None
        idx.add(make(tmp_path, state=ArchiveState.RESTORED))
        idx.add(rec)
        assert idx.find_active(rec.target) == rec
        idx.set_state(rec.id, ArchiveState.RESTORED)
        assert idx.find_active(rec.target) is None
        assert idx.get(rec.id) == replace(rec, state=ArchiveState.RESTORED)


def test_case_insensitive_lookup(tmp_path: Path, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "normcase", str.lower)
    rec = make(tmp_path)
    with Index(db) as idx:
        idx.add(rec)
        found = idx.find_active(Path(str(rec.target).upper()))
        assert found is not None
        assert found.target == rec.target  # original case preserved
        assert idx.for_project(Path(str(rec.project_root).upper())) == [rec]


def test_set_state_missing_raises(db: Path) -> None:
    with Index(db) as idx, pytest.raises(ArchiveNotFoundError):
        idx.set_state("nope", ArchiveState.STALE)


def test_delete(tmp_path: Path, db: Path) -> None:
    rec = make(tmp_path)
    with Index(db) as idx:
        idx.add(rec)
        idx.delete(rec.id)
        assert idx.get(rec.id) is None
        idx.delete(rec.id)  # missing is fine


def test_duplicate_id_leaves_no_partial_row(tmp_path: Path, db: Path) -> None:
    rec = make(tmp_path)
    with Index(db) as idx:
        idx.add(rec)
        with pytest.raises(sqlite3.IntegrityError):
            idx.add(replace(rec, kind="build"))
        assert idx.all() == [rec]


def test_migrations_fresh_and_idempotent(db: Path) -> None:
    Index(db).close()
    Index(db).close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_newer_version_raises(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(IndexVersionError):
        Index(db)


def test_failed_migration_rolls_back(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dormant import index

    monkeypatch.setattr(index, "_MIGRATIONS", [*index._MIGRATIONS, "CREATE TABLE archives (x);"])
    with pytest.raises(sqlite3.OperationalError):
        Index(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        conn.close()


def test_migration_2_keeps_rows_and_adds_reinstall(tmp_path: Path, db: Path) -> None:
    from dormant import index

    conn = sqlite3.connect(db)
    conn.executescript(index._MIGRATIONS[0] + "; PRAGMA user_version = 1;")
    conn.execute(
        "INSERT INTO archives VALUES ('x', 'r', 'r', 't', 't', 'build', NULL, '', 1, 0, 0.0, 1,"
        " 'active')"
    )
    conn.commit()
    conn.close()
    purged = make(
        tmp_path,
        archive_path=None,
        state=ArchiveState.PURGED,
        reinstall=("npm", "ci"),
        lockfile="package-lock.json",
    )
    with Index(db) as idx:
        old = idx.get("x")
        assert old is not None
        assert (old.reinstall, old.lockfile) == ((), None)
        idx.add(purged)
        assert idx.get(purged.id) == purged
