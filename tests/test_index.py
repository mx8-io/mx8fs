"""Tests for SQL-backed JSON storage indexes."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, MetaData, String, Table, create_engine, delete, insert, inspect, text, update
from sqlalchemy.exc import DatabaseError

import mx8fs
import mx8fs.index as index_module
import mx8fs.indexed_storage as indexed_storage_module
from mx8fs import (
    IndexedJsonFileStorage,
    IndexUpdateError,
    JsonFileStorage,
    json_file_storage_factory,
    json_index_factory,
)
from mx8fs.index import (
    NAMESPACE_COLUMN,
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


def test_index_factory_and_fingerprint() -> None:
    index = json_index_factory(IndexedRecord, INDEX_FIELDS, table_name="records")

    assert index.logical_table_name == "records"
    assert index.table_name.startswith("records__")
    assert len(index.table_name) <= 63
    assert index.status is index.table.c.status
    assert index.table.c[NAMESPACE_COLUMN].primary_key
    assert index.table.c.key.primary_key
    assert len(index.table.indexes) == len(INDEX_FIELDS)
    with pytest.raises(AttributeError):
        _ = index.missing

    class ChangedRecord(IndexedRecord):
        status: str = Field(min_length=3)

    changed = json_index_factory(ChangedRecord, INDEX_FIELDS, table_name="records")
    assert changed.fingerprint != index.fingerprint
    assert changed.table_name != index.table_name


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
        model_fields = {"key": object(), NAMESPACE_COLUMN: object()}

    with pytest.raises(ValueError, match="reserved"):
        _validate_index_fields(cast(type[BaseModel], FakeModel), [NAMESPACE_COLUMN], "key")


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


def test_corrupt_engine_replacement_uses_existing_replacement(tmp_path: Path) -> None:
    current, database_path = indexed_storage_module._create_index_engine(str(tmp_path))
    failed = create_engine("sqlite://")

    assert database_path is not None
    replacement, replacement_path = indexed_storage_module._replace_corrupt_index_engine(
        str(tmp_path), failed, database_path
    )

    assert replacement is current
    assert replacement_path == database_path
    failed.dispose()


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

    with storage.get_lock("a"):
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


def test_update_failures_leave_canonical_json_and_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)

    def fail(_: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(storage._index_manager, "upsert", fail)
    with pytest.raises(IndexUpdateError, match="key a"):
        storage.write(record("a", "pending", 1))
    assert storage.read("a").status == "pending"


def test_delete_failures_remove_canonical_json_and_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))

    def fail(_: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(storage._index_manager, "delete", fail)
    with pytest.raises(IndexUpdateError, match="key a"):
        storage.delete("a")
    assert not os.path.exists(storage._get_path("a"))


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


def test_invalid_index_is_recreated_and_rebuilt(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("a", "pending", 1))
    secondary = next(iter(storage.index.table.indexes))
    secondary.drop(storage._index_manager.engine)

    replacement = make_storage(tmp_path)

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
    readiness = iter([False, True])
    monkeypatch.setattr(storage._index_manager, "namespace_ready", lambda: next(readiness))
    monkeypatch.setattr(storage, "_reconcile_index", lambda _: pytest.fail("duplicate rebuild"))

    storage._ensure_index()


def test_rebuild_handles_files_changing_after_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storage(tmp_path)
    storage.write(record("current", "pending", 1))
    monkeypatch.setattr(indexed_storage_module, "file_exists", lambda _: False)
    result = storage.rebuild_index()
    assert result == index_module.IndexRebuildResult(upserted=0, removed=0)

    monkeypatch.undo()
    storage._index_manager.upsert(record("appeared", "done", 2))
    monkeypatch.setattr(indexed_storage_module, "file_exists", lambda _: True)
    monkeypatch.setattr(storage, "read", lambda key: record(key, "done", 2))
    monkeypatch.setattr(storage, "list", lambda: [])
    result = storage.rebuild_index()
    assert result.upserted == 1


def test_corrupt_database_may_disappear_before_preservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = json_index_factory(IndexedRecord, ["status"])
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)
    storage = storage_type.__new__(storage_type)
    storage.base_path = str(tmp_path)
    storage.index = index
    storage._namespace = namespace_for(str(tmp_path))
    engine = create_engine("sqlite://")
    missing_path = str(tmp_path / "missing.sqlite3")
    monkeypatch.setattr(indexed_storage_module, "_create_index_engine", lambda _: (engine, missing_path))
    calls = 0

    def manager(*_: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DatabaseError("statement", {}, RuntimeError("file is not a database"))
        return "replacement"

    monkeypatch.setattr(indexed_storage_module, "IndexManager", manager)
    assert storage._create_manager() == "replacement"


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


def test_non_corrupt_database_errors_are_not_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = json_index_factory(IndexedRecord, ["status"])
    storage_type = json_file_storage_factory("json", IndexedRecord, index=index)

    class BrokenManager:
        def __init__(self, *_: Any) -> None:
            raise DatabaseError("statement", {}, RuntimeError("database is locked"))

    monkeypatch.setattr(indexed_storage_module, "IndexManager", BrokenManager)
    with pytest.raises(DatabaseError):
        storage_type(str(tmp_path))


def test_corrupt_sqlite_is_preserved_and_rebuilt(tmp_path: Path) -> None:
    database = tmp_path / ".mx8fs-index.sqlite3"
    database.write_text("not a sqlite database")

    storage = make_storage(tmp_path)

    assert storage.query().all() == []
    assert list(tmp_path.glob(".mx8fs-index.sqlite3.invalid-*"))


def test_sql_type_metadata_for_nullable_enum_and_literals() -> None:
    _, enum_name, nullable, enum_type = _sql_type(State | None)
    assert enum_name.endswith(":string")
    assert nullable
    assert enum_type is State
    _, literal_name, _, _ = _sql_type(Literal[1, 2])
    assert literal_name == "integer"
