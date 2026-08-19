"""
Tests for the AI File Storage

Copyright (c) 2023-2025 MX8 Inc, all rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the “Software”), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions
of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

# pylint: disable=protected-access

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from mx8fs import FileLock, JsonFileStorage, VersionMismatchError, json_file_storage_factory
from mx8fs import storage as storage_module


class StorageTestClass(BaseModel):
    """Mock model for testing."""

    value: str
    key: str | None = None


@pytest.fixture(name="file_storage")
def fixture_file_storage(tmpdir: Path) -> Any:
    """Return a file storage object."""
    base_path = str(tmpdir)
    extension = "txt"
    return json_file_storage_factory(extension, StorageTestClass)(base_path)


def test_list(file_storage: JsonFileStorage) -> None:
    """Test the list method."""
    assert file_storage.list() == []

    # Create some files using the write method
    file_storage.write(StorageTestClass(value="content1"), "file1")
    file_storage.write(StorageTestClass(value="content2"), "file2")

    assert sorted(file_storage.list()) == sorted(["file1", "file2"])

    metadata = sorted(file_storage.list_with_metadata(), key=lambda file: file.name)
    assert [file.name for file in metadata] == ["file1", "file2"]
    assert all(file.size_bytes > 0 for file in metadata)


def test_read(file_storage: JsonFileStorage) -> None:
    """Test the read method."""
    file_storage.write(StorageTestClass(value="content1"), "file1")

    content = file_storage.read("file1")
    assert content == StorageTestClass(value="content1", key="file1")


def test_read_many_rejects_non_positive_workers(file_storage: JsonFileStorage) -> None:
    with pytest.raises(ValueError, match="max_workers must be positive"):
        file_storage.read_many([], max_workers=0)


def test_read_and_update_with_version(file_storage: JsonFileStorage) -> None:
    file_storage.write(StorageTestClass(value="content1"), "file1")
    current, version = file_storage.read_with_version("file1")

    assert current == StorageTestClass(value="content1", key="file1")
    updated = file_storage.update_if_version(StorageTestClass(value="content2", key="file1"), version)
    assert updated.value == "content2"

    with pytest.raises(VersionMismatchError):
        file_storage.update_if_version(StorageTestClass(value="stale", key="file1"), version)


def test_mutate_retries_version_conflicts(file_storage: JsonFileStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    file_storage.write(StorageTestClass(value="content1"), "file1")
    real_update = storage_module.update_file_if_version_matches
    attempts = 0

    def conflict_once(path: str, data: str, version: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            file_storage.update(StorageTestClass(value="concurrent", key="file1"))
            raise VersionMismatchError("changed")
        return real_update(path, data, version)

    monkeypatch.setattr(storage_module, "update_file_if_version_matches", conflict_once)

    result = file_storage.mutate("file1", lambda current: current.model_copy(update={"value": current.value + "!"}))

    assert attempts == 2
    assert result.value == "concurrent!"
    assert file_storage.read("file1") == result


def test_mutate_validates_attempts_and_key(file_storage: JsonFileStorage) -> None:
    file_storage.write(StorageTestClass(value="content1"), "file1")

    with pytest.raises(ValueError, match="positive"):
        file_storage.mutate("file1", lambda current: current, max_attempts=0)
    with pytest.raises(ValueError, match="storage key"):
        file_storage.mutate("file1", lambda current: current.model_copy(update={"key": "file2"}))


def test_write(file_storage: JsonFileStorage) -> None:
    """Test the write method."""
    content = file_storage.write(StorageTestClass(value="content1"))

    assert file_storage.read(content.key) == StorageTestClass(value="content1", key=content.key)


def test_write_key(file_storage: JsonFileStorage) -> None:
    content = StorageTestClass(value="content1")

    file_storage.write(content, "file1")

    assert file_storage.read("file1") == StorageTestClass(value="content1", key="file1")


def test_delete(file_storage: JsonFileStorage) -> None:
    """Test the delete method."""
    file_storage.write(StorageTestClass(value="content1"), "file1")

    assert "file1" in file_storage.list()

    file_storage.delete("file1")

    assert "file1" not in file_storage.list()


def test_get_path(file_storage: JsonFileStorage) -> None:
    """Test the _get_path method."""
    path = file_storage._get_path("file1")
    expected_path = file_storage.base_path + "/file1.txt"
    assert path == expected_path


def test_get_lock(file_storage: JsonFileStorage) -> None:
    """Test the get_lock method."""
    lock = file_storage.get_lock("file1")

    assert isinstance(lock, FileLock)
    assert lock.file == file_storage._get_path("file1")


def test_get_lock_custom_parameters(file_storage: JsonFileStorage) -> None:
    """Test the get_lock method with custom parameters."""
    lock = file_storage.get_lock(
        "file1",
        wait_period=0.5,
        time_out_seconds=10,
        maximum_age=20,
    )

    assert lock.file == file_storage._get_path("file1")
    assert lock.waiter.wait_period == 0.5
    assert lock.waiter.time_out_seconds == 10
    assert lock.maximum_age == timedelta(seconds=20)


def test_get_unique_key(file_storage: JsonFileStorage) -> None:
    """Test creation of unique survey_key"""
    previous_ids = []
    last_index = -1

    try:
        for i in range(40):
            last_index = i
            unique_key = file_storage._get_unique_key(key_length=1)
            file_storage.write(StorageTestClass(value="content1"), unique_key)
            assert unique_key not in previous_ids
            previous_ids.append(unique_key)
    except RecursionError:
        assert last_index == 36

    assert len(previous_ids) == 36


def test_aws_lambda(tmpdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test the __init__ method in AWS Lambda environment without randomizer."""
    base_path = str(tmpdir)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test_lambda")

    with pytest.raises(ValueError, match="Cannot use random.seed as a randomizer in AWS Lambda environment"):
        JsonFileStorage(base_path)
