"""SQL-backed secondary indexes for JSON file storage."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import types
import uuid
import weakref
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    Uuid,
    and_,
    delete,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable, DropTable
from sqlalchemy.sql import ColumnElement, Select, visitors

logger = logging.getLogger("mx8.index")

INDEX_FORMAT_VERSION = 2
MAX_IDENTIFIER_LENGTH = 63
NAMESPACE_COLUMN = "_mx8fs_namespace"
SOURCE_VERSION_COLUMN = "_mx8fs_source_version"
CATALOG_DEFINITIONS = "_mx8fs_index_definitions"
CATALOG_NAMESPACES = "_mx8fs_index_namespaces"
CATALOG_LEASES = "_mx8fs_index_leases"

_catalog_initialized: weakref.WeakSet[Engine] = weakref.WeakSet()
_catalog_initialized_lock = threading.Lock()


def _create_table(engine: Engine, table: Table) -> None:
    """Create a table and each index in separate DDL transactions."""
    with engine.begin() as connection:
        connection.execute(CreateTable(table, if_not_exists=True))
    for index in sorted(table.indexes, key=lambda item: item.name or ""):
        with engine.begin() as connection:
            connection.execute(CreateIndex(index, if_not_exists=True))


class IndexSchemaError(RuntimeError):
    """Base error for an unusable JSON index schema."""


class MissingIndexError(IndexSchemaError):
    """Raised when an expected index table or namespace is missing."""


class InvalidIndexError(IndexSchemaError):
    """Raised when an existing index does not match its definition."""


class IndexUpdateError(RuntimeError):
    """Raised after canonical JSON changed but its SQL index did not."""


@dataclass(frozen=True)
class IndexRebuildResult:
    """Counts returned after reconciling a JSON namespace."""

    upserted: int
    removed: int


def _safe_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not name:
        raise ValueError("Index table name must contain a letter or number")
    if name[0].isdigit():
        name = f"index_{name}"
    return name


def _referenced_definitions(field_schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """Return only JSON Schema definitions reachable from selected fields."""
    selected: dict[str, Any] = {}
    pending: list[Any] = [field_schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.rsplit("/", 1)[-1]
                if name not in selected and name in definitions:
                    selected[name] = definitions[name]
                    pending.append(definitions[name])
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return selected


def _unwrap_annotation(annotation: Any) -> tuple[Any, bool]:
    """Return the scalar annotation and whether it accepts None."""
    origin = get_origin(annotation)
    if origin is Literal:
        values = get_args(annotation)
        value_types = {type(value) for value in values}
        if len(value_types) != 1:
            raise TypeError("Indexed Literal fields must contain one scalar type")
        return next(iter(value_types)), False
    if origin in (Union, types.UnionType):
        args = get_args(annotation)
        nullable = type(None) in args
        scalar_args = tuple(arg for arg in args if arg is not type(None))
        if len(scalar_args) != 1:
            raise TypeError("Indexed unions may only add None to one scalar type")
        scalar, nested_nullable = _unwrap_annotation(scalar_args[0])
        return scalar, nullable or nested_nullable
    return annotation, False


def _sql_type(annotation: Any) -> tuple[Any, str, bool, type[Enum] | None]:
    scalar, nullable = _unwrap_annotation(annotation)
    enum_type: type[Enum] | None = None
    if isinstance(scalar, type) and issubclass(scalar, Enum):
        enum_type = scalar
        value_types = {type(member.value) for member in scalar}
        if len(value_types) != 1:
            raise TypeError("Indexed Enum values must contain one scalar type")
        scalar = next(iter(value_types))

    mappings: dict[Any, tuple[Any, str]] = {
        str: (String(), "string"),
        int: (BigInteger(), "integer"),
        float: (Float(), "float"),
        bool: (Boolean(), "boolean"),
        Decimal: (Numeric(), "decimal"),
        date: (Date(), "date"),
        datetime: (DateTime(timezone=True), "datetime"),
        UUID: (Uuid(as_uuid=True), "uuid"),
    }
    if scalar not in mappings:
        raise TypeError(f"Unsupported indexed field type: {annotation!r}")
    sql_type, type_name = mappings[scalar]
    if enum_type is not None:
        type_name = f"enum:{enum_type.__module__}.{enum_type.__qualname__}:{type_name}"
    return sql_type, type_name, nullable, enum_type


def _validate_index_fields(model: type[BaseModel], fields: Sequence[str], key_field: str) -> None:
    """Validate index field names before building SQL metadata."""
    if isinstance(fields, str):
        raise TypeError("Indexed fields must be a sequence of field names, not a string")
    if not fields:
        raise ValueError("At least one indexed field is required")
    if len(set(fields)) != len(fields):
        raise ValueError("Indexed fields must be unique")
    if key_field not in model.model_fields:
        raise ValueError(f"Unknown key field: {key_field}")
    unknown = [field for field in fields if field not in model.model_fields]
    if unknown:
        raise ValueError(f"Unknown indexed fields: {', '.join(unknown)}")
    reserved = {NAMESPACE_COLUMN, SOURCE_VERSION_COLUMN}
    selected_reserved = reserved.intersection((*fields, key_field))
    if selected_reserved:
        raise ValueError(f"{', '.join(sorted(selected_reserved))} is reserved by mx8fs")


class JsonIndex[ModelT]:
    """Pydantic-derived definition for a SQL secondary index."""

    def __init__(
        self,
        model: type[ModelT],
        fields: Sequence[str],
        table_name: str | None = None,
        key_field: str = "key",
    ) -> None:
        """Validate fields and construct a versioned SQLAlchemy table."""
        pydantic_model = cast(type[BaseModel], model)
        _validate_index_fields(pydantic_model, fields, key_field)

        self.model = model
        self.fields = tuple(fields)
        self.key_field = key_field
        default_name = f"{model.__module__}_{model.__qualname__}"
        self.logical_table_name = _safe_name(table_name or default_name)
        if table_name is not None and self.logical_table_name != table_name:
            raise ValueError("Explicit index table names must contain only lowercase letters, numbers, and underscores")
        self._field_types: dict[str, tuple[Any, str, bool, type[Enum] | None]] = {}
        for field_name in (key_field, *self.fields):
            annotation = pydantic_model.model_fields[field_name].annotation
            self._field_types[field_name] = _sql_type(annotation)

        schema = pydantic_model.model_json_schema()
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        field_schema = {
            name: {
                "schema": properties.get(name),
                "required": name in required,
                "sql_type": self._field_types[name][1],
                "nullable": self._field_types[name][2],
            }
            for name in (key_field, *self.fields)
        }
        definitions = _referenced_definitions(field_schema, schema.get("$defs", {}))
        fingerprint_input = {
            "format": INDEX_FORMAT_VERSION,
            "fields": list(self.fields),
            "key_field": key_field,
            "field_schema": field_schema,
            "definitions": definitions,
        }
        encoded = json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":"), default=str).encode()
        self.fingerprint = hashlib.sha256(encoded).hexdigest()
        suffix = self.fingerprint[:16]
        prefix_length = MAX_IDENTIFIER_LENGTH - len(suffix) - 2
        self.table_name = f"{self.logical_table_name[:prefix_length]}__{suffix}"
        self.metadata = MetaData()
        columns = [
            Column(NAMESPACE_COLUMN, String(64), primary_key=True),
            self._column(key_field, primary_key=True),
            Column(SOURCE_VERSION_COLUMN, String(128), nullable=True),
        ]
        columns.extend(self._column(field) for field in self.fields if field != key_field)
        self.table = Table(self.table_name, self.metadata, *columns)
        for field in self.fields:
            if field == key_field:
                continue
            index_name = _versioned_name(f"{self.table_name}_{field}_idx", self.fingerprint)
            Index(index_name, self.table.c[NAMESPACE_COLUMN], self.table.c[field], self.table.c[key_field])

    def _column(self, field: str, primary_key: bool = False) -> Column[Any]:
        sql_type, _, nullable, _ = self._field_types[field]
        return Column(field, sql_type, primary_key=primary_key, nullable=False if primary_key else nullable)

    def __getattr__(self, name: str) -> Column[Any]:
        """Expose generated SQLAlchemy columns by indexed field name."""
        if name in self.table.c:
            return cast(Column[Any], self.table.c[name])
        raise AttributeError(name)

    def values_for(self, model: ModelT, namespace: str, source_version: str | None = None) -> dict[str, Any]:
        """Extract SQL-safe projected values from a Pydantic model."""
        values: dict[str, Any] = {
            NAMESPACE_COLUMN: namespace,
            SOURCE_VERSION_COLUMN: source_version,
        }
        for field in (self.key_field, *self.fields):
            value = getattr(model, field)
            _, type_name, _, enum_type = self._field_types[field]
            if enum_type is not None and value is not None:
                value = value.value
            if type_name == "datetime" and value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError(f"Indexed datetime field {field} must be timezone-aware")
            values[field] = value
        return values


def _versioned_name(name: str, fingerprint: str) -> str:
    if len(name) <= MAX_IDENTIFIER_LENGTH:
        return name
    suffix = fingerprint[:12]
    return f"{name[: MAX_IDENTIFIER_LENGTH - len(suffix) - 1]}_{suffix}"


def json_index_factory[ModelT](
    model: type[ModelT],
    fields: Sequence[str],
    table_name: str | None = None,
    key_field: str = "key",
) -> JsonIndex[ModelT]:
    """Create a SQL index definition from selected Pydantic fields."""
    return JsonIndex(model, fields, table_name, key_field)


class _Lease(AbstractContextManager["_Lease"]):
    def __init__(self, manager: IndexManager[Any], name: str) -> None:
        self.manager = manager
        self.name = name
        self.owner = str(uuid.uuid4())
        self.duration = float(os.getenv("MX8FS_INDEX_LEASE_SECONDS", "900"))
        self.timeout = float(os.getenv("MX8FS_INDEX_LOCK_TIMEOUT", "840"))
        self._renew_at = 0.0

    def __enter__(self) -> _Lease:
        deadline = time.monotonic() + self.timeout
        while not self._try_acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for index lease {self.name}")
            time.sleep(0.1)
        self._renew_at = time.monotonic() + self.duration / 3
        return self

    def _try_acquire(self) -> bool:
        now = time.time()
        try:
            with self.manager.engine.begin() as connection:
                connection.execute(
                    insert(self.manager.leases).values(
                        lock_name=self.name,
                        owner=self.owner,
                        expires_at=now + self.duration,
                    )
                )
            return True
        except IntegrityError:
            with self.manager.engine.begin() as connection:
                result = connection.execute(
                    update(self.manager.leases)
                    .where(
                        self.manager.leases.c.lock_name == self.name,
                        self.manager.leases.c.expires_at < now,
                    )
                    .values(owner=self.owner, expires_at=now + self.duration)
                )
            return bool(result.rowcount == 1)

    def maybe_renew(self) -> None:
        """Renew a long-running lease when one third of its lifetime remains."""
        if time.monotonic() < self._renew_at:
            return
        with self.manager.engine.begin() as connection:
            result = connection.execute(
                update(self.manager.leases)
                .where(self.manager.leases.c.lock_name == self.name, self.manager.leases.c.owner == self.owner)
                .values(expires_at=time.time() + self.duration)
            )
        if result.rowcount != 1:
            raise RuntimeError(f"Lost index lease {self.name}")
        self._renew_at = time.monotonic() + self.duration / 3

    def __exit__(self, *_: Any) -> None:
        with self.manager.engine.begin() as connection:
            connection.execute(
                delete(self.manager.leases).where(
                    self.manager.leases.c.lock_name == self.name,
                    self.manager.leases.c.owner == self.owner,
                )
            )


class IndexManager[ModelT]:
    """Own index schema validation, catalog state, leases, and SQL writes."""

    def __init__(self, engine: Engine, index: JsonIndex[ModelT], namespace: str) -> None:
        """Initialize catalog tables for one physical index and namespace."""
        self.engine = engine
        self.index = index
        self.namespace = namespace
        catalog = MetaData()
        self.definitions = Table(
            CATALOG_DEFINITIONS,
            catalog,
            Column("physical_table", String(63), primary_key=True),
            Column("fingerprint", String(64), nullable=False),
            Column("generation", BigInteger, nullable=False),
        )
        self.namespaces = Table(
            CATALOG_NAMESPACES,
            catalog,
            Column("physical_table", String(63), primary_key=True),
            Column("namespace", String(64), primary_key=True),
            Column("fingerprint", String(64), nullable=False),
            Column("generation", BigInteger, nullable=False),
        )
        self.leases = Table(
            CATALOG_LEASES,
            catalog,
            Column("lock_name", String(255), primary_key=True),
            Column("owner", String(36), nullable=False),
            Column("expires_at", Float, nullable=False),
        )
        self._catalog_tables = tuple(catalog.sorted_tables)

    def ensure_catalog(self) -> None:
        """Create shared catalog tables once per process-local engine."""
        with _catalog_initialized_lock:
            if self.engine in _catalog_initialized:
                return
            for table in self._catalog_tables:
                _create_table(self.engine, table)
            _catalog_initialized.add(self.engine)

    def lease(self, name: str) -> _Lease:
        """Return a shared SQL-backed lease."""
        self.ensure_catalog()
        return _Lease(self, name)

    def create_schema_if_missing(self) -> None:
        """Create a physical index that does not yet exist."""
        self.ensure_catalog()
        if inspect(self.engine).has_table(self.index.table_name):
            return
        with self.lease(f"schema:{self.index.table_name}"):
            if not inspect(self.engine).has_table(self.index.table_name):
                self._recreate_schema()

    def migrate_schema(self) -> None:
        """Explicitly replace a missing or incompatible physical index."""
        self.ensure_catalog()
        try:
            self._validate_schema()
        except (MissingIndexError, InvalidIndexError):
            logger.exception("Migrating missing or incompatible JSON index %s", self.index.table_name)
            with self.lease(f"schema:{self.index.table_name}"):
                try:
                    self._validate_schema()
                except (MissingIndexError, InvalidIndexError):
                    self._recreate_schema()

    def validate_schema(self) -> None:
        """Validate the existing physical index without changing it."""
        self._validate_schema()

    def _validate_schema(self) -> None:
        inspector = inspect(self.engine)
        if not inspector.has_table(self.index.table_name):
            raise MissingIndexError(f"Missing index table {self.index.table_name}")
        with self.engine.connect() as connection:
            definition = (
                connection.execute(
                    select(self.definitions).where(self.definitions.c.physical_table == self.index.table_name)
                )
                .mappings()
                .first()
            )
        if definition is None:
            raise MissingIndexError(f"Missing catalog entry for {self.index.table_name}")
        if definition["fingerprint"] != self.index.fingerprint:
            raise InvalidIndexError(f"Fingerprint mismatch for {self.index.table_name}")

        expected_columns = set(self.index.table.c.keys())
        reflected_columns = {column["name"]: column for column in inspector.get_columns(self.index.table_name)}
        actual_columns = set(reflected_columns)
        if actual_columns != expected_columns:
            raise InvalidIndexError(f"Column mismatch for {self.index.table_name}")
        for expected in self.index.table.c:
            reflected = reflected_columns[expected.name]
            expected_type = str(expected.type.compile(dialect=self.engine.dialect)).upper()
            reflected_type = str(reflected["type"].compile(dialect=self.engine.dialect)).upper()
            if reflected_type != expected_type:
                raise InvalidIndexError(f"Column type mismatch for {self.index.table_name}.{expected.name}")
            if bool(reflected["nullable"]) != bool(expected.nullable):
                raise InvalidIndexError(f"Column nullability mismatch for {self.index.table_name}.{expected.name}")
        primary_key = set(inspector.get_pk_constraint(self.index.table_name).get("constrained_columns") or [])
        if primary_key != {NAMESPACE_COLUMN, self.index.key_field}:
            raise InvalidIndexError(f"Primary key mismatch for {self.index.table_name}")
        expected_indexes = {
            (NAMESPACE_COLUMN, field, self.index.key_field)
            for field in self.index.fields
            if field != self.index.key_field
        }
        actual_indexes = {
            tuple(item.get("column_names") or []) for item in inspector.get_indexes(self.index.table_name)
        }
        if not expected_indexes.issubset(actual_indexes):
            raise InvalidIndexError(f"Secondary index mismatch for {self.index.table_name}")

    def _recreate_schema(self) -> None:
        inspector = inspect(self.engine)
        with self.engine.connect() as connection:
            old_generation = connection.execute(
                select(self.definitions.c.generation).where(self.definitions.c.physical_table == self.index.table_name)
            ).scalar_one_or_none()
        if inspector.has_table(self.index.table_name):
            with self.engine.begin() as connection:
                connection.execute(DropTable(self.index.table, if_exists=True))
        with self.engine.begin() as connection:
            connection.execute(delete(self.namespaces).where(self.namespaces.c.physical_table == self.index.table_name))
            connection.execute(
                delete(self.definitions).where(self.definitions.c.physical_table == self.index.table_name)
            )
        _create_table(self.engine, self.index.table)
        with self.engine.begin() as connection:
            connection.execute(
                insert(self.definitions).values(
                    physical_table=self.index.table_name,
                    fingerprint=self.index.fingerprint,
                    generation=(old_generation or 0) + 1,
                )
            )

    def namespace_ready(self) -> bool:
        """Return whether this source namespace has been fully reconciled."""
        with self.engine.connect() as connection:
            definition = connection.execute(
                select(self.definitions.c.generation).where(self.definitions.c.physical_table == self.index.table_name)
            ).scalar_one()
            namespace_generation = connection.execute(
                select(self.namespaces.c.generation).where(
                    self.namespaces.c.physical_table == self.index.table_name,
                    self.namespaces.c.namespace == self.namespace,
                    self.namespaces.c.fingerprint == self.index.fingerprint,
                )
            ).scalar_one_or_none()
        return bool(namespace_generation == definition)

    def mark_namespace_ready(self) -> None:
        """Mark this namespace reconciled for the current table generation."""
        with self.engine.begin() as connection:
            generation = connection.execute(
                select(self.definitions.c.generation).where(self.definitions.c.physical_table == self.index.table_name)
            ).scalar_one()
            connection.execute(
                delete(self.namespaces).where(
                    self.namespaces.c.physical_table == self.index.table_name,
                    self.namespaces.c.namespace == self.namespace,
                )
            )
            connection.execute(
                insert(self.namespaces).values(
                    physical_table=self.index.table_name,
                    namespace=self.namespace,
                    fingerprint=self.index.fingerprint,
                    generation=generation,
                )
            )

    def upsert(self, model: ModelT, source_version: str | None = None) -> None:
        """Insert or update one projected model row."""
        self.upsert_projection(self.index.values_for(model, self.namespace, source_version))

    def _upsert_statement(self) -> Any:
        insert_factory = sqlite_insert if self.engine.dialect.name == "sqlite" else postgresql_insert
        statement = insert_factory(self.index.table)
        excluded = statement.excluded
        updated = {
            name: excluded[name]
            for name in self.index.table.c.keys()
            if name not in {NAMESPACE_COLUMN, self.index.key_field}
        }
        return statement.on_conflict_do_update(
            index_elements=[NAMESPACE_COLUMN, self.index.key_field],
            set_=updated,
        )

    def _execute_upserts(self, connection: Any, projections: Sequence[dict[str, Any]], batch_size: int) -> None:
        statement = self._upsert_statement()
        for start in range(0, len(projections), batch_size):
            connection.execute(statement, projections[start : start + batch_size])

    def upsert_projection(self, projection: dict[str, Any]) -> None:
        """Insert or update one precomputed index projection."""
        self.upsert_projections([projection], batch_size=1)

    def upsert_projections(self, projections: Sequence[dict[str, Any]], batch_size: int = 1000) -> None:
        """Insert or update precomputed projections in bounded batches."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if not projections:
            return
        with self.engine.begin() as connection:
            self._execute_upserts(connection, projections, batch_size)

    def replace_namespace(self, projections: Sequence[dict[str, Any]], batch_size: int = 1000) -> None:
        """Replace one namespace with a versioned canonical snapshot."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        values_by_key = {projection[self.index.key_field]: projection for projection in projections}
        values = list(values_by_key.values())
        with self.engine.begin() as connection:
            connection.execute(delete(self.index.table).where(self.index.table.c[NAMESPACE_COLUMN] == self.namespace))
            if values:
                self._execute_upserts(connection, values, batch_size)

    def delete_many(self, keys: Sequence[str]) -> None:
        """Delete several projected rows from this namespace."""
        if not keys:
            return
        with self.engine.begin() as connection:
            connection.execute(
                delete(self.index.table).where(
                    self.index.table.c[NAMESPACE_COLUMN] == self.namespace,
                    self.index.table.c[self.index.key_field].in_(keys),
                )
            )

    def delete(self, key: str) -> None:
        """Delete one projected model row."""
        with self.engine.begin() as connection:
            connection.execute(
                delete(self.index.table).where(
                    self.index.table.c[NAMESPACE_COLUMN] == self.namespace,
                    self.index.table.c[self.index.key_field] == key,
                )
            )

    def indexed_keys(self) -> set[str]:
        """Return all indexed keys for this namespace."""
        with self.engine.connect() as connection:
            return set(
                connection.execute(
                    select(self.index.table.c[self.index.key_field]).where(
                        self.index.table.c[NAMESPACE_COLUMN] == self.namespace
                    )
                ).scalars()
            )


class StorageQuery[ModelT]:
    """Immutable, storage-scoped SQLAlchemy query builder."""

    def __init__(
        self,
        storage: Any,
        conditions: tuple[ColumnElement[bool], ...] = (),
        ordering: tuple[ColumnElement[Any], ...] = (),
        limit_value: int | None = None,
        offset_value: int | None = None,
    ) -> None:
        """Store immutable query components until execution."""
        self._storage = storage
        self._conditions = conditions
        self._ordering = ordering
        self._limit = limit_value
        self._offset = offset_value

    def _copy(self, **changes: Any) -> StorageQuery[ModelT]:
        values = {
            "storage": self._storage,
            "conditions": self._conditions,
            "ordering": self._ordering,
            "limit_value": self._limit,
            "offset_value": self._offset,
        }
        values.update(changes)
        return StorageQuery(**values)

    def _validate_expression(self, expression: ColumnElement[Any]) -> None:
        table = self._storage.index.table
        columns = [element for element in visitors.iterate(expression) if isinstance(element, Column)]
        if not columns or any(column.table is not table for column in columns):
            raise ValueError("Query expressions must use columns from this storage index")

    def where(self, *conditions: ColumnElement[bool]) -> StorageQuery[ModelT]:
        """Add SQLAlchemy filter expressions."""
        for condition in conditions:
            self._validate_expression(condition)
        return self._copy(conditions=(*self._conditions, *conditions))

    def order_by(self, *expressions: ColumnElement[Any]) -> StorageQuery[ModelT]:
        """Add SQLAlchemy ordering expressions."""
        for expression in expressions:
            self._validate_expression(expression)
        return self._copy(ordering=(*self._ordering, *expressions))

    def limit(self, value: int) -> StorageQuery[ModelT]:
        """Limit returned rows."""
        if value < 0:
            raise ValueError("limit must be non-negative")
        return self._copy(limit_value=value)

    def offset(self, value: int) -> StorageQuery[ModelT]:
        """Offset returned rows."""
        if value < 0:
            raise ValueError("offset must be non-negative")
        return self._copy(offset_value=value)

    def page(self, number: int, size: int) -> StorageQuery[ModelT]:
        """Apply one-based offset pagination."""
        if number < 1 or size < 1:
            raise ValueError("page number and size must be positive")
        return self.limit(size).offset((number - 1) * size)

    def _statement(self, include_paging: bool = True) -> Select[Any]:
        index = self._storage.index
        selected = [index.table.c[index.key_field]]
        selected.extend(index.table.c[field] for field in index.fields if field != index.key_field)
        statement = select(*selected).where(index.table.c[NAMESPACE_COLUMN] == self._storage._namespace)
        if self._conditions:
            statement = statement.where(and_(*self._conditions))
        ordering = list(self._ordering)
        key_column = index.table.c[index.key_field]
        key_is_ordered = any(
            any(element is key_column for element in visitors.iterate(expression)) for expression in ordering
        )
        if not ordering or not key_is_ordered:
            ordering.append(key_column)
        statement = statement.order_by(*ordering)
        if include_paging:
            statement = statement.limit(self._limit).offset(self._offset)
        return statement

    def all(self) -> list[RowMapping]:
        """Return lightweight index rows."""
        self._storage._ensure_index()
        with self._storage._index_manager.engine.connect() as connection:
            return list(connection.execute(self._statement()).mappings())

    def first(self) -> RowMapping | None:
        """Return the first matching index row."""
        rows = self.limit(1).all()
        return rows[0] if rows else None

    def keys(self) -> list[str]:
        """Return matching keys in query order."""
        key_field = self._storage.index.key_field
        return [cast(str, row[key_field]) for row in self.all()]

    def count(self) -> int:
        """Count filtered rows before pagination."""
        self._storage._ensure_index()
        statement = self._statement(include_paging=False).order_by(None).subquery()
        with self._storage._index_manager.engine.connect() as connection:
            return cast(int, connection.execute(select(func.count()).select_from(statement)).scalar_one())

    def models(self, max_workers: int | None = None) -> list[ModelT]:
        """Concurrently hydrate matching JSON models."""
        return cast(list[ModelT], self._storage.read_many(self.keys(), max_workers=max_workers))


def namespace_for(base_path: str) -> str:
    """Return a stable, private namespace for a storage path."""
    normalized = base_path.rstrip("/")
    if not normalized.startswith("s3://"):
        normalized = os.path.abspath(normalized)
    return hashlib.sha256(normalized.encode()).hexdigest()
