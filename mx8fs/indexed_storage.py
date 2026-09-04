"""Optional SQL-indexed JSON file storage implementation."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from .file_io import (
    _get_file_version,
    _list_current_file_versions,
    read_file_with_version,
)
from .index import (
    IndexManager,
    IndexRebuildResult,
    IndexSchemaError,
    IndexUpdateError,
    JsonIndex,
    StorageQuery,
    namespace_for,
)
from .storage import _MAX_VERSION_ATTEMPTS, JsonFileStorage, _parallel_map

logger = logging.getLogger("mx8.storage")

_DEFAULT_REBUILD_WORKERS = 100
_index_engines: dict[tuple[str, ...], tuple[Engine, str | None]] = {}
_index_engines_lock = threading.Lock()
_index_engine_locks: dict[tuple[str, ...], threading.Lock] = {}


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
                pool_pre_ping=True,
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


class IndexedJsonFileStorage[ModelT](JsonFileStorage[ModelT]):
    """JSON storage with an explicitly migrated SQL secondary index."""

    _index_definition: JsonIndex[Any]

    def __init__(self, base_path: str, randomizer: Callable[[], None] | None = None) -> None:
        """Initialize file storage without migrating an existing SQL index."""
        super().__init__(base_path, randomizer)
        self.index = cast(JsonIndex[ModelT], self._index_definition)
        self._namespace = namespace_for(base_path)
        self._index_manager = self._create_manager()
        self._index_ready = False

    def _create_manager(self) -> IndexManager[ModelT]:
        engine, _ = _create_index_engine(self.base_path)
        return IndexManager(engine, self.index, self._namespace)

    def _prepare_schema(self) -> None:
        """Create a missing index or reject an incompatible existing one."""
        self._index_manager.ensure_catalog()
        self._index_manager.create_schema_if_missing()
        try:
            self._index_manager.validate_schema()
        except IndexSchemaError as error:
            raise type(error)(f"{error}. Run migrate_index() during deployment") from error

    def _ensure_index(self) -> None:
        if self._index_ready:
            return
        self._prepare_schema()
        if self._index_manager.namespace_ready():
            self._index_ready = True
            return
        logger.info("Creating JSON index namespace %s", self._namespace)
        with self._index_manager.lease(f"namespace:{self.index.table_name}:{self._namespace}") as lease:
            self._prepare_schema()
            if not self._index_manager.namespace_ready():
                self._reconcile_index(lease)
                self._index_manager.mark_namespace_ready()
        self._index_ready = True

    def _read_rebuild_key(self, key: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            contents, version = read_file_with_version(self._get_path(key))
        except FileNotFoundError:
            return key, None, None
        try:
            model = self._json_to_model(contents)
        except ValidationError as error:
            logger.error(
                "Skipping malformed JSON object key %s during index reconciliation: %s",
                key,
                error,
            )
            return key, None, version
        return key, self.index.values_for(model, self._namespace, version), version

    def _reconcile_index(self, lease: Any, max_workers: int | None = None) -> IndexRebuildResult:
        initial_indexed_keys = self._index_manager.indexed_keys()
        workers = _DEFAULT_REBUILD_WORKERS if max_workers is None else max_workers
        listed_versions = _list_current_file_versions(self.base_path, self._extension)
        results = _parallel_map(
            self._read_rebuild_key,
            sorted(listed_versions),
            workers,
            lease.maybe_renew,
        )
        observed_versions = {key: version for key, _, version in results if version is not None}
        projections = [projection for _, projection, _ in results if projection is not None]
        indexed_keys = {cast(str, projection[self._key_field]) for projection in projections}
        self._index_manager.replace_namespace(projections)
        lease.maybe_renew()

        for _ in range(_MAX_VERSION_ATTEMPTS):
            current_versions = _list_current_file_versions(self.base_path, self._extension)
            if current_versions == observed_versions:
                return IndexRebuildResult(
                    upserted=len(indexed_keys),
                    removed=len(initial_indexed_keys - indexed_keys),
                )
            changed_keys = sorted(
                key for key, version in current_versions.items() if observed_versions.get(key) != version
            )
            removed_source_keys = set(observed_versions) - set(current_versions)
            delete_index_keys = set(removed_source_keys)
            changed_results = _parallel_map(
                self._read_rebuild_key,
                changed_keys,
                workers,
                lease.maybe_renew,
            )
            changed_projections: list[dict[str, Any]] = []
            for key, projection, version in changed_results:
                if version is None:
                    observed_versions.pop(key, None)
                    delete_index_keys.add(key)
                else:
                    observed_versions[key] = version
                if projection is None:
                    delete_index_keys.add(key)
                    indexed_keys.discard(key)
                else:
                    changed_projections.append(projection)
                    indexed_keys.add(cast(str, projection[self._key_field]))
            for key in removed_source_keys:
                observed_versions.pop(key, None)
                indexed_keys.discard(key)
            self._index_manager.delete_many(sorted(delete_index_keys))
            self._index_manager.upsert_projections(changed_projections)
            lease.maybe_renew()
        raise IndexUpdateError(f"JSON changed repeatedly while rebuilding index namespace {self._namespace}")

    def rebuild_index(self, max_workers: int | None = None) -> IndexRebuildResult:
        """Fully reconcile this JSON namespace with its SQL index."""
        self._prepare_schema()
        with self._index_manager.lease(f"namespace:{self.index.table_name}:{self._namespace}") as lease:
            result = self._reconcile_index(lease, max_workers)
            self._index_manager.mark_namespace_ready()
            self._index_ready = True
            return result

    def migrate_index(self, max_workers: int | None = None) -> IndexRebuildResult:
        """Explicitly migrate the SQL index schema and rebuild this namespace."""
        self.migrate_schema()
        with self._index_manager.lease(f"namespace:{self.index.table_name}:{self._namespace}") as lease:
            result = self._reconcile_index(lease, max_workers)
            self._index_manager.mark_namespace_ready()
        self._index_ready = True
        return result

    def migrate_schema(self) -> None:
        """Explicitly migrate this storage's physical index schema."""
        self._index_ready = False
        self._index_manager.migrate_schema()

    def query(self) -> StorageQuery[ModelT]:
        """Create a scoped SQLAlchemy index query."""
        return StorageQuery(self)

    def update(self, content: ModelT) -> ModelT:
        """Write canonical JSON and stabilize its index without file locking."""
        self._ensure_index()
        key = cast(str, getattr(content, self._key_field))
        version = self._write_with_version(content)
        try:
            self._synchronize_written(key, content, version)
        except Exception as error:
            raise IndexUpdateError(f"JSON was updated but index synchronization failed for key {key}") from error
        return content

    def mutate(
        self,
        key: str,
        mutation: Callable[[ModelT], ModelT],
        max_attempts: int = _MAX_VERSION_ATTEMPTS,
    ) -> ModelT:
        """Mutate canonical JSON and stabilize its index."""
        self._ensure_index()
        updated, version = self._mutate_with_version(key, mutation, max_attempts)
        try:
            self._synchronize_written(key, updated, version)
        except Exception as error:
            raise IndexUpdateError(f"JSON was updated but index synchronization failed for key {key}") from error
        return updated

    def update_if_version(self, content: ModelT, version: str) -> ModelT:
        """Update canonical JSON only when its current version matches."""
        self._ensure_index()
        key = cast(str, getattr(content, self._key_field))
        written_version = self._update_if_version(content, version)
        try:
            self._synchronize_written(key, content, written_version)
        except Exception as error:
            raise IndexUpdateError(f"JSON was updated but index synchronization failed for key {key}") from error
        return content

    def _synchronize_written(self, key: str, model: ModelT, version: str) -> None:
        """Index a known write and reread only when a concurrent change occurred."""
        self._index_manager.upsert(model, version)
        try:
            if _get_file_version(self._get_path(key)) == version:
                return
        except FileNotFoundError:
            pass
        self._synchronize_key(key)

    def _synchronize_key(self, key: str) -> None:
        path = self._get_path(key)
        for _ in range(_MAX_VERSION_ATTEMPTS):
            try:
                model, version = self.read_with_version(key)
            except FileNotFoundError:
                self._index_manager.delete(key)
                try:
                    _get_file_version(path)
                except FileNotFoundError:
                    return
                continue
            self._index_manager.upsert(model, version)
            try:
                current_version = _get_file_version(path)
            except FileNotFoundError:
                continue
            if current_version == version:
                return
        raise IndexUpdateError(f"JSON changed repeatedly while synchronizing index key {key}")

    def delete(self, key: str) -> None:
        """Delete canonical JSON and stabilize its index without file locking."""
        self._ensure_index()
        super().delete(key)
        try:
            self._synchronize_key(key)
        except Exception as error:
            raise IndexUpdateError(f"JSON was deleted but index synchronization failed for key {key}") from error
