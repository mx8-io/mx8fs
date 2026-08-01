"""Guarded live tests for Aurora DSQL indexed JSON storage."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from mx8fs import json_file_storage_factory, json_index_factory
from mx8fs.file_io import delete_files

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
