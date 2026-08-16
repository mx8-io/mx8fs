"""Guarded live tests for Aurora DSQL indexed JSON storage."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import delete

import mx8fs.indexed_storage as indexed_storage_module
from mx8fs import json_file_storage_factory, json_index_factory
from mx8fs.file_io import delete_files, write_file
from mx8fs.index import NAMESPACE_COLUMN

pytestmark = pytest.mark.skipif(
    not os.getenv("MX8FS_DSQL_INTEGRATION_ENDPOINT"),
    reason="MX8FS_DSQL_INTEGRATION_ENDPOINT is not configured",
)


class LiveRecord(BaseModel):
    """Live DSQL integration model."""

    key: str | None = None
    status: str
    created_at: datetime


class ChangedLiveRecord(BaseModel):
    """Schema-changed version of the live model."""

    key: str | None = None
    status: str = Field(min_length=2)
    created_at: datetime


def _write_live_record(task: tuple[str, LiveRecord]) -> None:
    path, record = task
    write_file(path, record.model_dump_json())


def test_dsql_shared_namespaces_and_schema_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise live DSQL creation, sharing, querying, and version replacement."""
    endpoint = os.environ["MX8FS_DSQL_INTEGRATION_ENDPOINT"]
    bucket = os.getenv("MX8FS_DSQL_INTEGRATION_BUCKET", "mx8-test-bucket")
    root = f"s3://{bucket}/mx8fs/indexed-integration/{uuid4()}"
    first_path = f"{root}/first"
    second_path = f"{root}/second"
    monkeypatch.setenv("MX8FS_DSQL_ENDPOINT", endpoint)

    index = json_index_factory(LiveRecord, ["status", "created_at"], table_name="mx8fs_live_records")
    storage_type = json_file_storage_factory("json", LiveRecord, index=index)
    first = storage_type(first_path)
    second = storage_type(second_path)
    now = datetime.now(UTC)

    try:
        first.write(LiveRecord(status="pending", created_at=now), "same")
        second.write(LiveRecord(status="complete", created_at=now), "same")
        assert first.query().where(first.index.status == "pending").keys() == ["same"]
        assert second.query().where(second.index.status == "complete").keys() == ["same"]
        assert first.index.table_name == second.index.table_name

        changed_index = json_index_factory(
            ChangedLiveRecord,
            ["status", "created_at"],
            table_name="mx8fs_live_records",
        )
        changed_type = json_file_storage_factory("json", ChangedLiveRecord, index=changed_index)
        changed = changed_type(first_path)
        assert changed.index.table_name != first.index.table_name
        assert changed.query().keys() == ["same"]
    finally:
        delete_files([f"{first_path}/same.json", f"{second_path}/same.json"])


@pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow.*:DeprecationWarning")
def test_dsql_rebuilds_1000_s3_records_within_15_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against accidentally making the S3 rebuild sequential again."""
    endpoint = os.environ["MX8FS_DSQL_INTEGRATION_ENDPOINT"]
    bucket = os.getenv("MX8FS_DSQL_INTEGRATION_BUCKET", "mx8-test-bucket")
    root = f"s3://{bucket}/mx8fs/indexed-rebuild-performance/{uuid4()}"
    monkeypatch.setenv("MX8FS_DSQL_ENDPOINT", endpoint)

    index = json_index_factory(LiveRecord, ["status", "created_at"], table_name="mx8fs_live_records")
    storage_type = json_file_storage_factory("json", LiveRecord, index=index)
    storage = storage_type(root)
    real_list_versions = indexed_storage_module._list_file_versions
    real_replace_namespace = storage._index_manager.replace_namespace
    list_seconds = 0.0
    replace_seconds = 0.0

    def timed_list_versions(*args: str) -> dict[str, str]:
        nonlocal list_seconds
        started = perf_counter()
        result = real_list_versions(*args)
        list_seconds += perf_counter() - started
        return result

    def timed_replace_namespace(records: Any) -> None:
        nonlocal replace_seconds
        started = perf_counter()
        real_replace_namespace(records)
        replace_seconds += perf_counter() - started

    monkeypatch.setattr(indexed_storage_module, "_list_file_versions", timed_list_versions)
    monkeypatch.setattr(storage._index_manager, "replace_namespace", timed_replace_namespace)
    now = datetime.now(UTC)
    keys = [f"record-{number:04d}" for number in range(1000)]
    files = [f"{root}/{key}.json" for key in keys]
    uploads = [
        (path, LiveRecord(key=key, status="pending", created_at=now)) for path, key in zip(files, keys, strict=True)
    ]

    try:
        with ThreadPoolExecutor(max_workers=50) as executor:
            list(executor.map(_write_live_record, uploads))

        started = perf_counter()
        result = storage.rebuild_index()
        elapsed = perf_counter() - started

        assert result.upserted == 1000
        assert result.removed == 0
        assert storage.query().count() == 1000
        read_seconds = elapsed - list_seconds - replace_seconds
        assert elapsed < 15, (
            f"Rebuilding 1000 S3 records took {elapsed:.1f}s "
            f"(listing {list_seconds:.1f}s, reads {read_seconds:.1f}s, DSQL {replace_seconds:.1f}s)"
        )
    finally:
        delete_files(files)
        with storage._index_manager.engine.begin() as connection:
            connection.execute(
                delete(storage.index.table).where(storage.index.table.c[NAMESPACE_COLUMN] == storage._namespace)
            )
            connection.execute(
                delete(storage._index_manager.namespaces).where(
                    storage._index_manager.namespaces.c.physical_table == storage.index.table_name,
                    storage._index_manager.namespaces.c.namespace == storage._namespace,
                )
            )
