"""Optional SQL-indexed JSON file storage implementation."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError

from .file_io import file_exists
from .index import (
    IndexManager,
    IndexRebuildResult,
    IndexUpdateError,
    JsonIndex,
    MissingIndexError,
    StorageQuery,
    namespace_for,
)
from .lock import FileLock
from .storage import JsonFileStorage

logger = logging.getLogger("mx8.storage")

_index_engines: dict[tuple[str, ...], tuple[Engine, str | None]] = {}
_index_engines_lock = threading.Lock()
_index_engine_locks: dict[tuple[str, ...], threading.Lock] = {}


class _ReentrantFileLock(FileLock):
    """Reuse a storage lock already held by this thread and storage instance."""

    def __init__(self, storage: IndexedJsonFileStorage[Any], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._storage = storage
        self._owns_file_lock = False

    def __enter__(self) -> _ReentrantFileLock:
        counts = self._storage._lock_counts()
        if counts.get(self.file, 0) == 0:
            super().__enter__()
            self._owns_file_lock = True
        counts[self.file] = counts.get(self.file, 0) + 1
        return self

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
        counts = self._storage._lock_counts()
        counts[self.file] -= 1
        if counts[self.file] == 0:
            del counts[self.file]
            if self._owns_file_lock:  # pragma: no branch - false owners exit before the outer lock
                super().__exit__(*args, **kwargs)


def _index_engine_key(base_path: str) -> tuple[str, ...]:
    """Return the process-local identity of the index database."""
    process = str(os.getpid())
    if base_path.startswith("s3://"):
        endpoint = os.getenv("MX8FS_DSQL_ENDPOINT")
        if not endpoint:
            raise ValueError("MX8FS_DSQL_ENDPOINT is required for indexed S3 storage")
        return (
            process,
            "dsql",
            endpoint,
            os.getenv("MX8FS_DSQL_USER", "admin"),
            os.getenv("MX8FS_DSQL_DATABASE", "postgres"),
        )
    database_path = os.path.realpath(os.path.join(base_path, ".mx8fs-index.sqlite3"))
    return process, "sqlite", database_path


def _new_index_engine(key: tuple[str, ...]) -> tuple[Engine, str | None]:
    """Create an uncached SQLAlchemy engine for an index database."""
    if key[1] == "dsql":
        from aurora_dsql_sqlalchemy import create_dsql_engine
        from botocore.httpsession import get_cert_path

        return (
            create_dsql_engine(
                host=key[2],
                user=key[3],
                dbname=key[4],
                driver="psycopg",
                sslrootcert=get_cert_path(True),
            ),
            None,
        )

    database_path = key[2]
    os.makedirs(os.path.dirname(database_path), exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _configure_sqlite(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine, database_path


def _index_engine_lock(key: tuple[str, ...]) -> threading.Lock:
    """Return the lock that serializes work for one index database."""
    with _index_engines_lock:
        lock = _index_engine_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _index_engine_locks[key] = lock
        return lock


def _create_index_engine(base_path: str) -> tuple[Engine, str | None]:
    """Return the shared process-local engine for an index database."""
    key = _index_engine_key(base_path)
    with _index_engine_lock(key):
        with _index_engines_lock:
            current = _index_engines.get(key)
        if current is not None:
            return current
        current = _new_index_engine(key)
        with _index_engines_lock:
            _index_engines[key] = current
        return current


def _replace_corrupt_index_engine(
    base_path: str,
    failed_engine: Engine,
    database_path: str,
) -> tuple[Engine, str | None]:
    """Replace a corrupt cached SQLite engine once for this process."""
    key = _index_engine_key(base_path)
    with _index_engine_lock(key):
        with _index_engines_lock:
            current = _index_engines.get(key)
        if current is not None and current[0] is not failed_engine:
            return current
        failed_engine.dispose()
        with FileLock(database_path):
            if os.path.exists(database_path):
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
                os.replace(database_path, f"{database_path}.invalid-{timestamp}")
        replacement = _new_index_engine(key)
        with _index_engines_lock:
            _index_engines[key] = replacement
        return replacement


def _is_corrupt_sqlite(error: DatabaseError) -> bool:
    message = str(error).lower()
    return "file is not a database" in message or "database disk image is malformed" in message


class IndexedJsonFileStorage[ModelT](JsonFileStorage[ModelT]):
    """JSON storage with a self-healing SQL secondary index."""

    _index_definition: JsonIndex[Any]

    def __init__(self, base_path: str, randomizer: Callable[[], None] | None = None) -> None:
        """Initialize file storage and ensure its SQL index is ready."""
        super().__init__(base_path, randomizer)
        self.index = cast(JsonIndex[ModelT], self._index_definition)
        self._namespace = namespace_for(base_path)
        self._held_locks = threading.local()
        self._index_manager = self._create_manager()
        self._ensure_index()

    def _create_manager(self) -> IndexManager[ModelT]:
        engine, database_path = _create_index_engine(self.base_path)
        try:
            return IndexManager(engine, self.index, self._namespace)
        except DatabaseError as error:
            if database_path is None or not _is_corrupt_sqlite(error):
                raise
            logger.exception("Local JSON index database is corrupt; preserving and recreating it")
            replacement, _ = _replace_corrupt_index_engine(self.base_path, engine, database_path)
            return IndexManager(replacement, self.index, self._namespace)

    def _lock_counts(self) -> dict[str, int]:
        counts = getattr(self._held_locks, "counts", None)
        if counts is None:
            counts = {}
            self._held_locks.counts = counts
        return cast(dict[str, int], counts)

    def get_lock(
        self,
        key: str,
        wait_period: float = 0.1,
        time_out_seconds: int = 840,
        maximum_age: int = 900,
    ) -> FileLock:
        """Get a re-entrant file lock for an indexed stored model."""
        return _ReentrantFileLock(
            self,
            self._get_path(key),
            wait_period=wait_period,
            time_out_seconds=time_out_seconds,
            maximum_age=maximum_age,
        )

    def _ensure_index(self) -> None:
        self._index_manager.ensure_schema()
        if self._index_manager.namespace_ready():
            return
        try:
            raise MissingIndexError(f"Index namespace {self._namespace} has not been reconciled")
        except MissingIndexError:
            logger.exception("JSON index namespace is missing; rebuilding")
        with self._index_manager.lease(f"namespace:{self.index.table_name}:{self._namespace}") as lease:
            self._index_manager.ensure_schema()
            if not self._index_manager.namespace_ready():
                self._reconcile_index(lease)
                self._index_manager.mark_namespace_ready()

    def _reconcile_index(self, lease: Any) -> IndexRebuildResult:
        current_keys = set(self.list())
        upserted = 0
        removed = 0
        for key in sorted(current_keys):
            with self.get_lock(key):
                if file_exists(self._get_path(key)):
                    self._index_manager.upsert(self.read(key))
                    upserted += 1
                else:
                    self._index_manager.delete(key)
            lease.maybe_renew()

        for key in sorted(self._index_manager.indexed_keys() - current_keys):
            with self.get_lock(key):
                if file_exists(self._get_path(key)):
                    self._index_manager.upsert(self.read(key))
                    upserted += 1
                else:
                    self._index_manager.delete(key)
                    removed += 1
            lease.maybe_renew()
        return IndexRebuildResult(upserted=upserted, removed=removed)

    def rebuild_index(self) -> IndexRebuildResult:
        """Fully reconcile this JSON namespace with its SQL index."""
        self._index_manager.ensure_schema()
        with self._index_manager.lease(f"namespace:{self.index.table_name}:{self._namespace}") as lease:
            result = self._reconcile_index(lease)
            self._index_manager.mark_namespace_ready()
            return result

    def query(self) -> StorageQuery[ModelT]:
        """Create a scoped SQLAlchemy index query."""
        return StorageQuery(self)

    def update(self, content: ModelT) -> ModelT:
        """Write canonical JSON and synchronize its index under one key lock."""
        self._ensure_index()
        key = cast(str, getattr(content, self._key_field))
        with self.get_lock(key):
            result = super().update(content)
            try:
                self._index_manager.upsert(result)
            except Exception as error:
                raise IndexUpdateError(f"JSON was updated but index synchronization failed for key {key}") from error
            return result

    def delete(self, key: str) -> None:
        """Delete canonical JSON and synchronize its index under one key lock."""
        self._ensure_index()
        with self.get_lock(key):
            super().delete(key)
            try:
                self._index_manager.delete(key)
            except Exception as error:
                raise IndexUpdateError(f"JSON was deleted but index synchronization failed for key {key}") from error
