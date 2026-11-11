"""SQLite index of archives. Every write is its own transaction."""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from dormant.errors import ArchiveNotFoundError, IndexVersionError

logger = logging.getLogger(__name__)


class ArchiveState(StrEnum):
    """Lifecycle state of an archive row."""

    ACTIVE = "active"  # target is hibernated, archive file exists
    RESTORED = "restored"
    STALE = "stale"  # target came back by other means; archive kept for prune
    PURGED = "purged"  # deleted without an archive


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    """One archived (or purged) target folder."""

    id: str
    project_root: Path
    target: Path
    kind: str
    archive_path: Path | None
    checksum: str
    source_bytes: int
    archive_bytes: int
    created_at: float
    format_version: int
    state: ArchiveState
    # Purge mode only: the argv run on wake (no shell) and the lockfile it installs from.
    reinstall: tuple[str, ...] = ()
    lockfile: str | None = None


# Append only. Index i upgrades user_version i -> i + 1.
_MIGRATIONS: list[str] = [
    """
    CREATE TABLE archives (
        id TEXT PRIMARY KEY,
        project_root TEXT NOT NULL,
        project_key TEXT NOT NULL,
        target TEXT NOT NULL,
        target_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        archive_path TEXT,
        checksum TEXT NOT NULL,
        source_bytes INTEGER NOT NULL,
        archive_bytes INTEGER NOT NULL,
        created_at REAL NOT NULL,
        format_version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'restored', 'stale', 'purged'))
    );
    CREATE INDEX ix_archives_target ON archives (target_key, state);
    CREATE INDEX ix_archives_project ON archives (project_key);
    """,
    """
    ALTER TABLE archives ADD COLUMN reinstall TEXT;
    ALTER TABLE archives ADD COLUMN lockfile TEXT;
    """,
]

_COLUMNS = (
    "id, project_root, target, kind, archive_path, checksum, source_bytes, "
    "archive_bytes, created_at, format_version, state, reinstall, lockfile"
)


def _key(path: Path) -> str:
    """Lookup key: absolute, case-folded where the OS is case-insensitive."""
    return os.path.normcase(os.path.abspath(path))  # noqa: PTH100 - must not resolve


def _to_record(row: sqlite3.Row) -> ArchiveRecord:
    archive_path = row["archive_path"]
    return ArchiveRecord(
        id=row["id"],
        project_root=Path(row["project_root"]),
        target=Path(row["target"]),
        kind=row["kind"],
        archive_path=None if archive_path is None else Path(archive_path),
        checksum=row["checksum"],
        source_bytes=row["source_bytes"],
        archive_bytes=row["archive_bytes"],
        created_at=row["created_at"],
        format_version=row["format_version"],
        state=ArchiveState(row["state"]),
        reinstall=tuple(json.loads(row["reinstall"])) if row["reinstall"] else (),
        lockfile=row["lockfile"],
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the schema up to date in one transaction.

    Raises:
        IndexVersionError: The database is newer than this code.
    """
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    target = len(_MIGRATIONS)
    if version > target:
        msg = f"index schema version {version} is newer than supported {target}; upgrade Dormant"
        raise IndexVersionError(msg)
    if version == target:
        return
    logger.info("migrating index schema %d -> %d", version, target)
    # ponytail: two processes migrating at once can collide; fine while one CLI runs at a time.
    script = "".join(_MIGRATIONS[version:])
    try:
        conn.executescript(f"BEGIN IMMEDIATE;{script};PRAGMA user_version = {target};COMMIT;")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        raise


class Index:
    """The archive index. Use as a context manager to close it reliably."""

    def __init__(self, path: Path) -> None:
        """Open (and create or migrate) the index at ``path``.

        Args:
            path: SQLite database file.

        Raises:
            IndexVersionError: The database was written by a newer Dormant.
        """
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            _migrate(self._conn)
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def add(self, record: ArchiveRecord) -> None:
        """Insert a record.

        Args:
            record: The record to store; its id must be new.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO archives (id, project_root, project_key, target, target_key, kind, "
                "archive_path, checksum, source_bytes, archive_bytes, created_at, "
                "format_version, state, reinstall, lockfile) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    str(record.project_root),
                    _key(record.project_root),
                    str(record.target),
                    _key(record.target),
                    record.kind,
                    None if record.archive_path is None else str(record.archive_path),
                    record.checksum,
                    record.source_bytes,
                    record.archive_bytes,
                    record.created_at,
                    record.format_version,
                    record.state.value,
                    json.dumps(list(record.reinstall)) if record.reinstall else None,
                    record.lockfile,
                ),
            )

    def _select(self, where: str, params: tuple[str, ...]) -> list[ArchiveRecord]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM archives {where} ORDER BY created_at",  # noqa: S608 - where is a literal
            params,
        ).fetchall()
        return [_to_record(row) for row in rows]

    def get(self, id: str) -> ArchiveRecord | None:
        """Return the record with this id, or None."""
        found = self._select("WHERE id = ?", (id,))
        return found[0] if found else None

    def find_active(self, target: Path) -> ArchiveRecord | None:
        """Return the ACTIVE record for ``target`` (newest if several), or None."""
        found = self._select("WHERE target_key = ? AND state = ?", (_key(target), "active"))
        return found[-1] if found else None

    def for_project(self, project_root: Path) -> list[ArchiveRecord]:
        """Return every record under ``project_root``, oldest first."""
        return self._select("WHERE project_key = ?", (_key(project_root),))

    def all(self) -> list[ArchiveRecord]:
        """Return every record, oldest first."""
        return self._select("", ())

    def set_state(self, id: str, state: ArchiveState) -> None:
        """Change a record's state.

        Raises:
            ArchiveNotFoundError: No record has this id.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE archives SET state = ? WHERE id = ?", (state.value, id)
            )
        if cur.rowcount == 0:
            raise ArchiveNotFoundError(id)

    def delete(self, id: str) -> None:
        """Delete a record. Missing ids are ignored."""
        with self._conn:
            self._conn.execute("DELETE FROM archives WHERE id = ?", (id,))
