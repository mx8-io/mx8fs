"""Tests for SQL-backed JSON storage indexes."""

from __future__ import annotations

import os
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    event,
    insert,
    inspect,
    text,
    update,
)
from sqlalchemy.exc import DatabaseError

import mx8fs
import mx8fs.index as index_module
import mx8fs.indexed_storage as indexed_storage_module
import mx8fs.storage as storage_module
from mx8fs import (
    IndexedJsonFileStorage,
    IndexUpdateError,
    JsonFileStorage,
    VersionMismatchError,
    json_file_storage_factory,
    json_index_factory,
)
from mx8fs.index import (
    NAMESPACE_COLUMN,
    SOURCE_VERSION_COLUMN,
    InvalidIndexError,
    MissingIndexError,
    _referenced_definitions,
    _safe_name,
    _sql_type,
    _validate_index_fields,
    _versioned_name,
    namespace_for,
)


class State(StrEnum):
    """Indexed string enum."""

    READY = "ready"
    WAITING = "waiting"


class IndexedRecord(BaseModel):
    """Model covering supported index types."""

    key: str | None = None
    status: str
    created_at: datetime
    not_before: datetime | None = None
    count: int = 0
    ratio: float = 0.0
    active: bool = False
    amount: Decimal = Decimal("0")
    day: date = date(2026, 1, 1)
    identifier: UUID = Field(default_factory=uuid4)
    state: State = State.READY
    kind: Literal["a", "b"] = "a"


INDEX_FIELDS = [
    "status",
    "created_at",
    "not_before",
    "count",
    "ratio",
    "active",
    "amount",
    "day",
    "identifier",
    "state",
    "kind",
]


def make_storage(path: Path) -> IndexedJsonFileStorage[IndexedRecord]:
    """Create an indexed test storage."""
    index = json_index_factory(IndexedRecord, INDEX_FIELDS, table_name="records")
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)
    return storage_type(str(path))


def record(key: str | None, status: str, hour: int) -> IndexedRecord:
    """Create a deterministic test model."""
    return IndexedRecord(key=key, status=status, created_at=datetime(2026, 1, 1, hour, tzinfo=UTC))


def projection(storage: IndexedJsonFileStorage[IndexedRecord], content: IndexedRecord, version: str) -> dict[str, Any]:
    """Project a test record using the storage namespace."""
    return storage.index.values_for(content, storage._namespace, version)


def test_index_factory_and_fingerprint() -> None:
    index = json_index_factory(IndexedRecord, INDEX_FIELDS, table_name="records")

    assert index.logical_table_name == "records"
    assert index.table_name.startswith("records__")
    assert len(index.table_name) <= 63
    assert index.status is index.table.c.status
    assert index.table.c[NAMESPACE_COLUMN].primary_key
    assert SOURCE_VERSION_COLUMN in index.table.c
    assert index.table.c.key.primary_key
    assert len(index.table.indexes) == len(INDEX_FIELDS)
    with pytest.raises(AttributeError):
        _ = index.missing

    class ChangedRecord(IndexedRecord):
        status: str = Field(min_length=3)

    changed = json_index_factory(ChangedRecord, INDEX_FIELDS, table_name="records")
    assert changed.fingerprint != index.fingerprint
    assert changed.table_name != index.table_name


def test_postgresql_upsert_statement_uses_conflict_update() -> None:
    engine = create_engine("postgresql+psycopg://")
    index = json_index_factory(IndexedRecord, ["status"])
    manager = index_module.IndexManager(engine, index, "namespace")

    statement = str(manager._upsert_statement().compile(dialect=engine.dialect))

    assert "ON CONFLICT" in statement
    assert "DO UPDATE" in statement


def test_index_name_and_schema_helpers() -> None:
    with pytest.raises(ValueError, match="letter or number"):
        _safe_name("---")
    assert _safe_name("123 jobs") == "index_123_jobs"
    assert len(_versioned_name("x" * 100, "a" * 64)) == 63
    assert _referenced_definitions(
        {"items": [{"$ref": "#/$defs/Present"}, {"$ref": "#/$defs/Missing"}]},
        {"Present": {"type": "string"}},
    ) == {"Present": {"type": "string"}}
    assert namespace_for("s3://bucket/path/") == namespace_for("s3://bucket/path")


def test_reserved_fields_are_rejected() -> None:
    class FakeModel:
        model_fields = {"key": object(), NAMESPACE_COLUMN: object(), SOURCE_VERSION_COLUMN: object()}

    with pytest.raises(ValueError, match="reserved"):
        _validate_index_fields(cast(type[BaseModel], FakeModel), [NAMESPACE_COLUMN], "key")
    with pytest.raises(ValueError, match="reserved"):
        _validate_index_fields(cast(type[BaseModel], FakeModel), [SOURCE_VERSION_COLUMN], "key")


def test_key_may_be_projected_without_a_secondary_index() -> None:
    index = json_index_factory(IndexedRecord, ["key"])
    assert not index.table.indexes


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ([], "At least one"),
        (["status", "status"], "unique"),
        (["missing"], "Unknown indexed"),
        ("status", "sequence"),
    ],
)
def test_index_factory_rejects_bad_fields(fields: Any, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        json_index_factory(IndexedRecord, fields)


def test_index_factory_rejects_bad_names_and_keys() -> None:
    with pytest.raises(ValueError, match="Unknown key"):
        json_index_factory(IndexedRecord, ["status"], key_field="missing")
    with pytest.raises(ValueError, match="lowercase"):
        json_index_factory(IndexedRecord, ["status"], table_name="Bad Name")

    class NumericNameModel(BaseModel):
        key: str
        value: str

    index = json_index_factory(NumericNameModel, ["value"], table_name="valid_name")
    assert index.logical_table_name == "valid_name"


def test_index_factory_rejects_unsupported_types() -> None:
    class Unsupported(BaseModel):
        key: str
        values: list[str]

    class UnionModel(BaseModel):
        key: str
        value: str | int

    class LiteralModel(BaseModel):
        key: str
        value: Literal["one", 2]

    class MixedEnum(Enum):
        ONE = "one"
        TWO = 2

    class EnumModel(BaseModel):
        key: str
        value: MixedEnum

    for model, field in [
        (Unsupported, "values"),
        (UnionModel, "value"),
        (LiteralModel, "value"),
        (EnumModel, "value"),
    ]:
        with pytest.raises(TypeError):
            json_index_factory(cast(type[BaseModel], model), [field])


def test_storage_factory_validates_index() -> None:
    index = json_index_factory(IndexedRecord, ["status"])

    class OtherRecord(IndexedRecord):
        pass

    with pytest.raises(ValueError, match="must match"):
        json_file_storage_factory("json", OtherRecord, index=index)
    with pytest.raises(ValueError, match="must match"):
        json_file_storage_factory("json", IndexedRecord, "identifier", index=index)

    plain_type = json_file_storage_factory("json", IndexedRecord)
    assert issubclass(plain_type, JsonFileStorage)
    assert not issubclass(plain_type, IndexedJsonFileStorage)


def test_indexed_storage_reuses_engine_for_database(tmp_path: Path) -> None:
    first = make_storage(tmp_path)
    second = make_storage(Path(f"{tmp_path}/."))
    other = make_storage(tmp_path / "other")

    assert first._index_manager.engine is second._index_manager.engine
    assert first._index_manager.engine is not other._index_manager.engine


def test_catalog_tables_are_created_once_per_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite3'}")
    index = json_index_factory(IndexedRecord, ["status"])
    calls: list[str] = []
    real_create_table = index_module._create_table

    def track_create_table(target_engine: Any, table: Table) -> None:
        calls.append(table.name)
        real_create_table(target_engine, table)

    monkeypatch.setattr(index_module, "_create_table", track_create_table)

    index_module.IndexManager(engine, index, "first").ensure_catalog()
    index_module.IndexManager(engine, index, "second").ensure_catalog()

    assert sorted(calls) == sorted(
        [index_module.CATALOG_DEFINITIONS, index_module.CATALOG_NAMESPACES, index_module.CATALOG_LEASES]
    )


def test_indexed_storage_does_not_define_record_file_locks() -> None:
    assert "FileLock" not in vars(indexed_storage_module)
    assert "_ReentrantFileLock" not in vars(indexed_storage_module)


def test_indexed_crud_query_and_hydration(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    first = storage.write(record(None, "pending", 1), "a")
    storage.write(record("b", "done", 2))
    storage.write(record("c", "pending", 3))

    assert first.key == "a"
    assert storage.query().keys() == ["a", "b", "c"]
    query = storage.query().where(storage.index.status == "pending").order_by(storage.index.created_at.desc())
    assert query.keys() == ["c", "a"]
    assert storage.query().order_by(storage.index.key).keys() == ["a", "b", "c"]
    assert query.page(2, 1).keys() == ["a"]
    assert query.page(2, 1).count() == 2
    first_row = query.first()
    assert first_row is not None
    assert first_row["key"] == "c"
    assert storage.query().where(storage.index.status == "missing").first() is None
    assert [item.key for item in query.models(max_workers=2)] == ["c", "a"]
    assert dict(query.all()[0]) == {
        "key": "c",
        "status": "pending",
        "created_at": datetime(2026, 1, 1, 3),
        "not_before": None,
        "count": 0,
        "ratio": 0.0,
        "active": False,
        "amount": Decimal("0E-10"),
        "day": date(2026, 1, 1),
        "identifier": storage.read("c").identifier,
        "state": "ready",
        "kind": "a",
    }

    storage.update(record("a", "done", 4))
    assert storage.query().where(storage.index.status == "pending").keys() == ["c"]
    storage.delete("b")
    assert storage.query().keys() == ["a", "c"]


def test_query_validation(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    other = Table("other", MetaData(), Column("status", String))

    with pytest.raises(ValueError, match="this storage"):
        storage.query().where(other.c.status == "pending")
    with pytest.raises(ValueError, match="this storage"):
        storage.query().order_by(other.c.status)
    with pytest.raises(ValueError, match="this storage"):
        storage.query().where(cast(Any, text("1 = 1")))
    with pytest.raises(ValueError, match="this storage"):
        storage.query().order_by(cast(Any, text("status")))
    with pytest.raises(ValueError, match="non-negative"):
        storage.query().limit(-1)
    with pytest.raises(ValueError, match="non-negative"):
        storage.query().offset(-1)
    with pytest.raises(ValueError, match="positive"):
        storage.query().page(0, 1)
    with pytest.raises(ValueError, match="positive"):
        storage.query().page(1, 0)
    assert storage.query().limit(0).all() == []


def test_read_many_preserves_order_duplicates_and_errors(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    storage.write(record("b", "done", 2))

    assert [item.key for item in storage.read_many(["b", "a", "b"], max_workers=2)] == ["b", "a", "b"]
    assert storage.read_many([]) == []
    with pytest.raises(FileNotFoundError):
        storage.read_many(["a", "missing"], max_workers=2)


def test_rebuild_repairs_missing_and_stale_rows(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    storage._index_manager.delete("a")
    storage._index_manager.upsert(record("stale", "done", 2))

    result = storage.rebuild_index()

    assert result.upserted == 1
    assert result.removed == 1
    assert storage.query().keys() == ["a"]


def test_rebuild_reconciles_different_keys_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    storage.write(record("b", "done", 2))
    rendezvous = threading.Barrier(2)

    versions = {"a": "version-a", "b": "version-b"}

    def read_rebuild_key(key: str) -> tuple[str, dict[str, Any], str]:
        rendezvous.wait(timeout=1)
        return key, projection(storage, record(key, "pending", 1), versions[key]), versions[key]

    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", lambda *_: versions)
    monkeypatch.setattr(storage, "_read_rebuild_key", read_rebuild_key)

    assert storage.rebuild_index(max_workers=2) == index_module.IndexRebuildResult(upserted=2, removed=0)


def test_initialization_skips_malformed_records_and_completes_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("valid", "pending", 1))
    storage.write(record("malformed", "stale", 2))
    Path(storage._get_path("malformed")).write_text('{"key":"malformed"}', encoding="UTF-8")
    with storage._index_manager.engine.begin() as connection:
        connection.execute(delete(storage._index_manager.namespaces))

    with caplog.at_level("ERROR", logger="mx8.storage"):
        reconciled = make_storage(tmp_path)

    assert reconciled.query().keys() == ["valid"]
    assert reconciled._index_manager.namespace_ready()
    assert "malformed" in caplog.text
    assert "validation" in caplog.text.lower()
    with pytest.raises(ValidationError):
        reconciled.read("malformed")

    monkeypatch.setattr(
        indexed_storage_module.IndexedJsonFileStorage,
        "_reconcile_index",
        lambda *_: pytest.fail("completed namespace was rebuilt"),
    )
    make_storage(tmp_path)


def test_rebuild_does_not_delete_unindexed_malformed_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    Path(storage._get_path("malformed")).write_text('{"key":"malformed"}', encoding="UTF-8")
    deleted: list[str] = []
    monkeypatch.setattr(storage._index_manager, "delete", deleted.append)

    result = storage.rebuild_index()

    assert result == index_module.IndexRebuildResult(upserted=0, removed=0)
    assert deleted == []


def test_rebuild_propagates_infrastructure_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("valid", "pending", 1))

    def storage_failure(_: str) -> tuple[str, IndexedRecord, str]:
        raise OSError("storage unavailable")

    monkeypatch.setattr(storage, "_read_rebuild_key", storage_failure)
    with pytest.raises(OSError, match="storage unavailable"):
        storage.rebuild_index()

    monkeypatch.undo()

    def database_failure(_: Any) -> None:
        raise RuntimeError("DSQL unavailable")

    monkeypatch.setattr(storage._index_manager, "replace_namespace", database_failure)
    with pytest.raises(RuntimeError, match="DSQL unavailable"):
        storage.rebuild_index()


def test_update_failures_leave_canonical_json_and_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)

    def fail(_: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(storage._index_manager, "upsert", fail)
    with pytest.raises(IndexUpdateError, match="key a"):
        storage.write(record("a", "pending", 1))
    assert storage.read("a").status == "pending"


def test_update_if_version_rejects_stale_content(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    current, version = storage.read_with_version("a")
    assert current.status == "pending"

    updated = storage.update_if_version(record("a", "done", 2), version)
    assert updated.status == "done"
    indexed = storage.query().first()
    assert indexed is not None
    assert indexed["status"] == "done"

    with pytest.raises(VersionMismatchError):
        storage.update_if_version(record("a", "stale", 3), version)
    assert storage.read("a").status == "done"


def test_mutate_rereads_and_recomputes_after_version_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    real_update = storage_module.update_file_if_version_matches
    attempts = 0

    def conflict_once(path: str, data: str, version: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            Path(path).write_text(record("a", "concurrent", 2).model_copy(update={"count": 10}).model_dump_json())
            raise VersionMismatchError("changed")
        return real_update(path, data, version)

    monkeypatch.setattr(storage_module, "update_file_if_version_matches", conflict_once)

    result = storage.mutate("a", lambda current: current.model_copy(update={"count": current.count + 1}))

    assert attempts == 2
    assert result.count == 11
    assert storage.read("a").count == 11
    indexed = storage.query().first()
    assert indexed is not None
    assert indexed["count"] == 11


def test_mutate_validates_attempts_key_and_exhausted_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))

    with pytest.raises(ValueError, match="positive"):
        storage.mutate("a", lambda current: current, max_attempts=0)
    with pytest.raises(ValueError, match="storage key"):
        storage.mutate("a", lambda current: current.model_copy(update={"key": "b"}))

    def conflict(*_: Any) -> str:
        raise VersionMismatchError("changed")

    monkeypatch.setattr(storage_module, "update_file_if_version_matches", conflict)
    with pytest.raises(VersionMismatchError, match="all 2"):
        storage.mutate("a", lambda current: current, max_attempts=2)


def test_mutate_reports_index_failure_after_canonical_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))

    def fail(*_: Any) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(storage, "_synchronize_written", fail)

    with pytest.raises(IndexUpdateError, match="key a"):
        storage.mutate("a", lambda current: current.model_copy(update={"status": "done"}))

    assert storage.read("a").status == "done"


def test_update_if_version_reports_index_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    _, version = storage.read_with_version("a")

    def fail(*_: Any) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(storage, "_synchronize_written", fail)

    with pytest.raises(IndexUpdateError, match="key a"):
        storage.update_if_version(record("a", "done", 2), version)

    assert storage.read("a").status == "done"


def test_uncontended_update_indexes_known_version_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    monkeypatch.setattr(storage, "_synchronize_key", lambda _: pytest.fail("canonical JSON reread"))

    storage.update(record("a", "done", 2))

    indexed = storage.query().first()
    assert indexed is not None
    assert indexed["status"] == "done"


def test_written_version_missing_during_check_falls_back_to_full_synchronization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    synchronized: list[str] = []
    monkeypatch.setattr(indexed_storage_module, "_get_file_version", lambda _: (_ for _ in ()).throw(FileNotFoundError))
    monkeypatch.setattr(storage, "_synchronize_key", synchronized.append)

    storage._synchronize_written("a", record("a", "done", 2), "written")

    assert synchronized == ["a"]


def test_index_synchronization_repeats_when_canonical_json_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    real_upsert = storage._index_manager.upsert
    calls = 0

    def change_after_first_upsert(model: IndexedRecord, version: str | None = None) -> None:
        nonlocal calls
        calls += 1
        real_upsert(model, version)
        if calls == 1:
            Path(storage._get_path("a")).write_text(record("a", "newer", 3).model_dump_json())

    monkeypatch.setattr(storage._index_manager, "upsert", change_after_first_upsert)

    storage.update(record("a", "done", 2))

    assert calls == 2
    indexed = storage.query().first()
    assert indexed is not None
    assert indexed["status"] == "newer"


def test_index_synchronization_retries_missing_and_unstable_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    _, version = storage.read_with_version("a")
    checks = 0

    def version_after_temporary_missing(_: str) -> str:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise FileNotFoundError
        return version

    monkeypatch.setattr(indexed_storage_module, "_get_file_version", version_after_temporary_missing)
    storage._synchronize_key("a")

    monkeypatch.setattr(indexed_storage_module, "_get_file_version", lambda _: "always-different")
    with pytest.raises(IndexUpdateError, match="changed repeatedly"):
        storage._synchronize_key("a")


def test_delete_failures_remove_canonical_json_and_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))

    def fail(_: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(storage._index_manager, "delete", fail)
    with pytest.raises(IndexUpdateError, match="key a"):
        storage.delete("a")
    assert not os.path.exists(storage._get_path("a"))


def test_delete_indexes_a_concurrent_recreation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    real_delete = storage._index_manager.delete
    calls = 0

    def recreate_after_delete(key: str) -> None:
        nonlocal calls
        calls += 1
        real_delete(key)
        if calls == 1:
            Path(storage._get_path(key)).write_text(record(key, "recreated", 2).model_dump_json())

    monkeypatch.setattr(storage._index_manager, "delete", recreate_after_delete)

    storage.delete("a")

    assert storage.read("a").status == "recreated"
    indexed = storage.query().first()
    assert indexed is not None
    assert indexed["status"] == "recreated"


def test_naive_datetime_fails_after_json_write(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    content = record("a", "pending", 1).model_copy(update={"created_at": datetime(2026, 1, 1)})

    with pytest.raises(IndexUpdateError) as error:
        storage.update(content)

    assert isinstance(error.value.__cause__, ValueError)
    assert storage.read("a").created_at.tzinfo is None


def test_namespaces_share_one_sql_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.sqlite3'}")
    monkeypatch.setattr(indexed_storage_module, "_create_index_engine", lambda _: (engine, None))
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first = make_storage(first_path)
    second = make_storage(second_path)
    first.write(record("same", "first", 1))
    second.write(record("same", "second", 2))

    assert first.index.table_name == second.index.table_name
    first_row = first.query().first()
    second_row = second.query().first()
    assert first_row is not None
    assert second_row is not None
    assert first_row["status"] == "first"
    assert second_row["status"] == "second"


def test_missing_table_is_recreated_and_rebuilt(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    storage.index.table.drop(storage._index_manager.engine)

    replacement = make_storage(tmp_path)

    assert replacement.query().keys() == ["a"]


def test_schema_creation_rechecks_after_taking_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    checks = iter([False, True])

    class Inspector:
        @staticmethod
        def has_table(_: str) -> bool:
            return next(checks)

    monkeypatch.setattr(index_module, "inspect", lambda _: Inspector())
    monkeypatch.setattr(storage._index_manager, "_recreate_schema", lambda: pytest.fail("duplicate creation"))

    storage._index_manager.create_schema_if_missing()


def test_schema_validation_reports_missing_table(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.query().all()
    storage.index.table.drop(storage._index_manager.engine)

    with pytest.raises(MissingIndexError, match="Missing index table"):
        storage._index_manager.validate_schema()


def test_invalid_index_requires_explicit_migration(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    secondary = next(iter(storage.index.table.indexes))
    secondary.drop(storage._index_manager.engine)

    index = json_index_factory(IndexedRecord, INDEX_FIELDS, table_name="records")
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)
    replacement = storage_type(str(tmp_path))

    with pytest.raises(InvalidIndexError, match="migrate_index"):
        replacement.query().keys()

    assert secondary.name not in {
        item["name"] for item in inspect(storage._index_manager.engine).get_indexes(storage.index.table_name)
    }

    replacement.migrate_index()

    assert replacement.query().keys() == ["a"]


def test_lease_expiry_renewal_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    manager = storage._index_manager
    lease = manager.lease("test")
    lease.__enter__()
    monkeypatch.setenv("MX8FS_INDEX_LOCK_TIMEOUT", "0")
    with pytest.raises(TimeoutError):
        with manager.lease("test"):
            pass

    with manager.engine.begin() as connection:
        connection.execute(update(manager.leases).where(manager.leases.c.lock_name == "test").values(expires_at=0))
    replacement = manager.lease("test")
    replacement.__enter__()
    replacement._renew_at = 0
    replacement.maybe_renew()
    replacement.__exit__()
    lease.__exit__()

    with manager.engine.begin() as connection:
        connection.execute(insert(manager.leases).values(lock_name="lost", owner="other", expires_at=9999999999.0))
    lost = manager.lease("lost")
    lost.owner = "not-owner"
    lost._renew_at = 0
    with pytest.raises(RuntimeError, match="Lost"):
        lost.maybe_renew()

    waiting = manager.lease("eventual")
    waiting.timeout = 1
    attempts = iter([False, True])
    monkeypatch.setattr(waiting, "_try_acquire", lambda: next(attempts))
    monkeypatch.setattr(index_module.time, "sleep", lambda _: None)
    waiting.__enter__()


def test_schema_validation_detects_every_kind_of_drift(  # noqa: C901
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    storage.query().all()
    manager = storage._index_manager

    with manager.engine.begin() as connection:
        connection.execute(delete(manager.definitions))
    with pytest.raises(MissingIndexError, match="catalog"):
        manager._validate_schema()
    with manager.engine.begin() as connection:
        connection.execute(
            insert(manager.definitions).values(
                physical_table=storage.index.table_name,
                fingerprint="wrong",
                generation=1,
            )
        )
    with pytest.raises(InvalidIndexError, match="Fingerprint"):
        manager._validate_schema()
    with manager.engine.begin() as connection:
        connection.execute(
            update(manager.definitions)
            .where(manager.definitions.c.physical_table == storage.index.table_name)
            .values(fingerprint=storage.index.fingerprint)
        )

    real = inspect(manager.engine)

    class InspectorProxy:
        def __init__(self, failure: str) -> None:
            self.failure = failure

        def has_table(self, name: str) -> bool:
            return bool(real.has_table(name))

        def get_columns(self, name: str) -> list[dict[str, Any]]:
            columns = [dict(column) for column in real.get_columns(name)]
            if self.failure == "columns":
                columns.pop()
            elif self.failure == "type":
                columns[0]["type"] = BigInteger()
            elif self.failure == "nullable":
                columns[0]["nullable"] = not columns[0]["nullable"]
            return columns

        def get_pk_constraint(self, name: str) -> dict[str, Any]:
            if self.failure == "primary":
                return {"constrained_columns": ["key"]}
            return cast(dict[str, Any], real.get_pk_constraint(name))

        def get_indexes(self, name: str) -> list[dict[str, Any]]:
            if self.failure == "indexes":
                return []
            return [dict(index) for index in real.get_indexes(name)]

    messages = {
        "columns": "Column mismatch",
        "type": "Column type mismatch",
        "nullable": "nullability",
        "primary": "Primary key",
        "indexes": "Secondary index",
    }
    for failure, message in messages.items():
        monkeypatch.setattr(index_module, "inspect", lambda _, failure=failure: InspectorProxy(failure))
        with pytest.raises(InvalidIndexError, match=message):
            manager._validate_schema()


def test_namespace_recheck_avoids_duplicate_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage._index_ready = False
    readiness = iter([False, True])
    monkeypatch.setattr(storage._index_manager, "namespace_ready", lambda: next(readiness))
    monkeypatch.setattr(storage, "_reconcile_index", lambda _: pytest.fail("duplicate rebuild"))

    storage._ensure_index()


def test_new_storage_instance_reuses_ready_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    index = json_index_factory(IndexedRecord, INDEX_FIELDS, table_name="records")
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)
    replacement = storage_type(str(tmp_path))
    monkeypatch.setattr(replacement, "_reconcile_index", lambda _: pytest.fail("namespace rebuilt"))

    assert replacement.query().keys() == ["a"]


def test_ready_index_is_not_revalidated_on_every_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    monkeypatch.setattr(storage._index_manager, "validate_schema", lambda: pytest.fail("schema revalidated"))

    storage.update(record("a", "done", 2))
    assert storage.query().keys() == ["a"]


def test_rebuild_handles_files_changing_after_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("current", "pending", 1))

    listings = iter([{"current": "old"}, {}])
    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", lambda *_: next(listings))
    monkeypatch.setattr(storage, "_read_rebuild_key", lambda key: (key, None, None))
    result = storage.rebuild_index()
    assert result == index_module.IndexRebuildResult(upserted=0, removed=1)

    monkeypatch.undo()
    storage._index_manager.upsert(record("appeared", "done", 2))
    listings = iter([{}, {"appeared": "new"}, {"appeared": "new"}, {"appeared": "new"}])
    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", lambda *_: next(listings))
    monkeypatch.setattr(
        storage,
        "_read_rebuild_key",
        lambda key: (key, projection(storage, record(key, "done", 2), "new"), "new"),
    )
    result = storage.rebuild_index()
    assert result.upserted == 1


def test_rebuild_repairs_only_changed_projection_after_bulk_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    listings = iter(
        [
            {"a": "1", "b": "1"},
            {"a": "2", "b": "1"},
            {"a": "2", "b": "1"},
        ]
    )
    read_versions = {"a": iter(["1", "2"]), "b": iter(["1"])}
    reads: list[str] = []
    replacements = 0
    delta_keys: list[str] = []
    real_replace = storage._index_manager.replace_namespace
    real_upsert = storage._index_manager.upsert_projections

    def read_projection(key: str) -> tuple[str, dict[str, Any], str]:
        reads.append(key)
        version = next(read_versions[key])
        return key, projection(storage, record(key, f"version-{version}", 1), version), version

    def replace(projections: Any) -> None:
        nonlocal replacements
        replacements += 1
        real_replace(projections)

    def upsert(projections: Any, **kwargs: Any) -> None:
        delta_keys.extend(item[storage._key_field] for item in projections)
        real_upsert(projections, **kwargs)

    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", lambda *_: next(listings))
    monkeypatch.setattr(storage, "_read_rebuild_key", read_projection)
    monkeypatch.setattr(storage._index_manager, "replace_namespace", replace)
    monkeypatch.setattr(storage._index_manager, "upsert_projections", upsert)

    result = storage.rebuild_index(max_workers=2)

    assert result.upserted == 2
    assert replacements == 1
    assert reads.count("a") == 2
    assert reads.count("b") == 1
    assert delta_keys == ["a"]


def test_rebuild_delta_handles_disappearing_and_malformed_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    listings = iter(
        [
            {"a": "1", "b": "1"},
            {"a": "2"},
            {"a": "2"},
            {"a": "2"},
        ]
    )
    reads = {
        "a": iter(
            [
                (projection(storage, record("a", "initial", 1), "1"), "1"),
                (None, None),
                (None, "2"),
            ]
        ),
        "b": iter([(projection(storage, record("b", "initial", 1), "1"), "1")]),
    }

    def read_projection(key: str) -> tuple[str, dict[str, Any] | None, str | None]:
        projected, version = next(reads[key])
        return key, projected, version

    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", lambda *_: next(listings))
    monkeypatch.setattr(storage, "_read_rebuild_key", read_projection)

    result = storage.rebuild_index(max_workers=1)

    assert result.upserted == 0
    assert storage.query().keys() == []


def test_rebuild_handles_a_file_disappearing_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)

    def missing(*_: Any) -> tuple[str, str]:
        raise FileNotFoundError

    monkeypatch.setattr(indexed_storage_module, "read_file_with_version", missing)

    assert storage._read_rebuild_key("missing") == ("missing", None, None)


def test_rebuild_fails_when_source_versions_never_stabilize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    version = 0

    def changing_versions(*_: Any) -> dict[str, str]:
        nonlocal version
        version += 1
        return {"a": str(version)}

    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", changing_versions)
    monkeypatch.setattr(
        storage,
        "_read_rebuild_key",
        lambda key: (key, projection(storage, record(key, "pending", 1), "read"), "read"),
    )

    with pytest.raises(IndexUpdateError, match="changed repeatedly"):
        storage.rebuild_index()


def test_projection_batch_validation_and_empty_operations(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    manager = storage._index_manager

    with pytest.raises(ValueError, match="batch_size"):
        manager.upsert_projections([], batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        manager.replace_namespace([], batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        manager.delete_many([], batch_size=0)
    manager.upsert_projections([])
    manager.delete_many([])


def test_namespace_replacement_commits_each_bounded_batch(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.migrate_schema()
    manager = storage._index_manager
    manager.upsert(record("stale", "done", 1), "stale-version")
    projections = [
        projection(storage, record(f"record-{number}", "pending", number), str(number)) for number in range(5)
    ]
    commits = 0

    def count_commit(_: Any) -> None:
        nonlocal commits
        commits += 1

    event.listen(manager.engine, "commit", count_commit)
    try:
        manager.replace_namespace(projections, batch_size=2)
    finally:
        event.remove(manager.engine, "commit", count_commit)

    assert commits == 4
    assert manager.indexed_keys() == {f"record-{number}" for number in range(5)}


def test_create_manager_propagates_corrupt_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = json_index_factory(IndexedRecord, ["status"])
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)
    storage = storage_type.__new__(storage_type)
    storage.base_path = str(tmp_path)
    storage.index = index
    storage._namespace = namespace_for(str(tmp_path))
    engine = create_engine("sqlite://")
    missing_path = str(tmp_path / "missing.sqlite3")
    monkeypatch.setattr(indexed_storage_module, "_create_index_engine", lambda _: (engine, missing_path))

    def manager(*_: Any) -> Any:
        raise DatabaseError("statement", {}, RuntimeError("file is not a database"))

    monkeypatch.setattr(indexed_storage_module, "IndexManager", manager)
    with pytest.raises(DatabaseError):
        storage._create_manager()


def test_optional_export_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mx8fs._OPTIONAL_EXPORTS.isdisjoint(mx8fs.__all__)

    with pytest.raises(AttributeError):
        mx8fs.__getattr__("not_an_export")

    def missing_module(*_: Any) -> Any:
        raise ModuleNotFoundError()

    monkeypatch.setattr(mx8fs, "_load_optional_export", missing_module)
    with pytest.raises(ModuleNotFoundError, match="indexed-storage extra"):
        mx8fs.__getattr__("JsonIndex")


def test_dsql_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MX8FS_DSQL_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="MX8FS_DSQL_ENDPOINT"):
        indexed_storage_module._create_index_engine("s3://bucket/path")

    sentinel = create_engine("sqlite://")
    calls: dict[str, Any] = {}
    call_count = 0

    def fake_engine(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        calls.update(kwargs)
        return sentinel

    import aurora_dsql_sqlalchemy

    monkeypatch.setattr(aurora_dsql_sqlalchemy, "create_dsql_engine", fake_engine)
    monkeypatch.setenv("MX8FS_DSQL_ENDPOINT", "cluster.example")
    monkeypatch.setenv("MX8FS_DSQL_USER", "writer")
    monkeypatch.setenv("MX8FS_DSQL_DATABASE", "indexes")

    engine, path = indexed_storage_module._create_index_engine("s3://bucket/path")
    shared_engine, shared_path = indexed_storage_module._create_index_engine("s3://other/path")

    assert engine is sentinel
    assert path is None
    assert shared_engine is sentinel
    assert shared_path is None
    assert call_count == 1
    assert calls["host"] == "cluster.example"
    assert calls["user"] == "writer"
    assert calls["dbname"] == "indexes"
    assert calls["driver"] == "psycopg"
    assert calls["sslrootcert"].endswith("botocore/cacert.pem")
    assert calls["pool_pre_ping"] is True


def test_dsql_engine_replaces_stale_pooled_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aurora_dsql_sqlalchemy

    def fake_engine(**kwargs: Any) -> Any:
        return create_engine(
            f"sqlite:///{tmp_path / 'dsql-pool.sqlite3'}",
            pool_pre_ping=kwargs.get("pool_pre_ping", False),
        )

    monkeypatch.setattr(aurora_dsql_sqlalchemy, "create_dsql_engine", fake_engine)
    monkeypatch.setenv("MX8FS_DSQL_ENDPOINT", "stale-pool.example")
    engine, _ = indexed_storage_module._create_index_engine("s3://bucket/path")

    with engine.connect() as connection:
        stale_connection = connection.connection.driver_connection
        connection.execute(text("SELECT 1"))
    assert stale_connection is not None
    stale_connection.close()

    with engine.connect() as connection:
        replacement_connection = connection.connection.driver_connection
        assert connection.scalar(text("SELECT 1")) == 1

    assert replacement_connection is not stale_connection


def test_sqlite_engine_does_not_enable_pre_ping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    real_create_engine = create_engine

    def tracking_create_engine(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real_create_engine(*args, **kwargs)

    monkeypatch.setattr(indexed_storage_module, "create_engine", tracking_create_engine)

    engine, _ = indexed_storage_module._create_index_engine(str(tmp_path))

    assert calls == [{"connect_args": {"timeout": 30}}]
    engine.dispose()


def test_non_corrupt_database_errors_are_not_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = json_index_factory(IndexedRecord, ["status"])
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)

    class BrokenManager:
        def __init__(self, *_: Any) -> None:
            raise DatabaseError("statement", {}, RuntimeError("database is locked"))

    monkeypatch.setattr(indexed_storage_module, "IndexManager", BrokenManager)
    with pytest.raises(DatabaseError):
        storage_type(str(tmp_path))


def test_corrupt_sqlite_is_not_automatically_healed(tmp_path: Path) -> None:
    database = tmp_path / ".mx8fs-index.sqlite3"
    database.write_text("not a sqlite database")

    storage = make_storage(tmp_path)
    with pytest.raises(DatabaseError):
        storage.query().all()

    assert database.read_text() == "not a sqlite database"
    assert not list(tmp_path.glob(".mx8fs-index.sqlite3.invalid-*"))


def test_sql_type_metadata_for_nullable_enum_and_literals() -> None:
    _, enum_name, nullable, enum_type = _sql_type(State | None)
    assert enum_name.endswith(":string")
    assert nullable
    assert enum_type is State
    _, literal_name, _, _ = _sql_type(Literal[1, 2])
    assert literal_name == "integer"
