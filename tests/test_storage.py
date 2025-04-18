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

from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import BaseModel

from mx8fs import JsonFileStorage, json_file_storage_factory


class StorageTestClass(BaseModel):
    """Mock model for testing."""

    value: str
    key: Optional[str] = None


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


def test_read(file_storage: JsonFileStorage) -> None:
    """Test the read method."""
    file_storage.write(StorageTestClass(value="content1"), "file1")

    content = file_storage.read("file1")
    assert content == StorageTestClass(value="content1", key="file1")


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


def test_get_unique_key(file_storage: JsonFileStorage) -> None:
    """Test creation of unique survey_key"""
    previous_ids = []
    i = 0

    try:
        for i in range(40):
            unique_key = file_storage._get_unique_key(key_length=1)
            file_storage.write(StorageTestClass(value="content1"), unique_key)
            assert unique_key not in previous_ids
            previous_ids.append(unique_key)
    except RecursionError:
        assert i == 36

    assert len(previous_ids) == 36


def test_aws_lambda(tmpdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test the __init__ method in AWS Lambda environment without randomizer."""
    base_path = str(tmpdir)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test_lambda")

    with pytest.raises(ValueError, match="Cannot use random.seed as a randomizer in AWS Lambda environment"):
        JsonFileStorage(base_path)
