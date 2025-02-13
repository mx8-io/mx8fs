"""
Test the AWS function

Copyright (c) 2023-2025 MX8 Inc
All rights reserved.

This software is confidential and proprietary information of MX8.
You shall not disclose such Confidential Information and shall use it only
in accordance with the terms of the agreement you entered into with MX8.
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import urllib3

from mx8fs import (
    BinaryFileHandler,
    copy_file,
    delete_file,
    file_exists,
    get_public_url,
    list_files,
    most_recent_timestamp,
    read_file,
    write_file,
)
from mx8fs import move_file

TEST_BUCKET_NAME = "s3://mx8-test-bucket/mx8fs"


def _test_read_file(file: str) -> None:
    """Test the read_file function"""
    delete_file(file)
    with pytest.raises(FileNotFoundError):
        read_file(file)


def _test_read_binary_file(file: str) -> None:
    """Test the read_file function"""
    delete_file(file)
    with pytest.raises(FileNotFoundError):
        with BinaryFileHandler(file, "rb") as f:
            f.read()

    with pytest.raises(NotImplementedError):
        with BinaryFileHandler(file, "r") as f:
            f.read()


def _test_write_binary_file(file: str) -> None:
    """Test the write_file function"""
    delete_file(file)

    with BinaryFileHandler(file, "wb") as f:
        f.write(b"test")
    assert file_exists(file) is True
    with BinaryFileHandler(file, "rb") as f:
        assert f.read() == b"test"

    with pytest.raises(NotImplementedError):
        with BinaryFileHandler(file, "r") as f:
            f.read()

    # Delete the file
    delete_file(file)


def _test_write_file(file: str) -> None:
    """Test the write_file function"""
    assert file_exists(file) is False
    write_file(file, "test")
    assert file_exists(file) is True
    assert read_file(file) == "test"

    # Delete the file
    delete_file(file)

    with pytest.raises(FileNotFoundError):
        read_file(file)


TEST_FILE_1 = "test1.txt"
TEST_FILE_2 = "test2.txt"


def _test_list_files(path: str) -> None:
    """Test the list_files function"""
    files = list_files(path, "txt")
    assert len(files) == 0

    write_file(os.path.join(path, TEST_FILE_1), "test1")
    write_file(os.path.join(path, TEST_FILE_2), "test2")

    for files in [sorted(list_files(path, "txt")), sorted(list_files(path, "txt", "test"))]:
        assert len(files) == 2
        assert files[0] == "test1"
        assert files[1] == "test2"

    assert len(list_files(path, "txt", "test1")) == 1
    assert len(list_files(path, "txt", "notest")) == 0

    # Delete the files
    delete_file(os.path.join(path, TEST_FILE_1))
    delete_file(os.path.join(path, TEST_FILE_2))

    # Delete the file again with no error
    delete_file(os.path.join(path, TEST_FILE_2))


def _test_most_recent_timestamp(path: str) -> None:
    """Test the most_recent_timestamp function"""
    # Create a file
    file_1 = os.path.join(path, "test.txt")
    write_file(file_1, "test")

    # Get the timestamp
    timestamp_1 = most_recent_timestamp(path, "txt")
    assert timestamp_1 > 0

    time.sleep(1)

    # Create another file
    file_2 = os.path.join(path, "test2.txt")
    write_file(file_2, "test2")

    # Get the timestamp again
    timestamp_2 = most_recent_timestamp(path, "txt")
    assert timestamp_2 > 0
    assert timestamp_2 > timestamp_1

    # Delete the files
    delete_file(file_1)
    delete_file(file_2)

    # Get the timestamp again
    timestamp = most_recent_timestamp(path, "txt")
    assert timestamp == 0


def test_local(tmp_path: Path) -> None:
    """Test the local file"""
    local_file = os.path.join(tmp_path, "test.txt")

    _test_read_file(local_file)
    _test_write_file(local_file)

    _test_read_binary_file(local_file)
    _test_write_binary_file(local_file)

    _test_list_files(str(tmp_path))

    _test_most_recent_timestamp(str(tmp_path))


def test_local_public_url(tmp_path: Path) -> None:
    """Test the local public URLs"""
    local_file = os.path.join(tmp_path, "test.txt")
    write_file(local_file, "test")
    assert get_public_url(local_file) == local_file


def _fetch_url(url: str) -> urllib3.BaseHTTPResponse:
    """Fetch a URL"""
    http = urllib3.PoolManager()
    return http.request("GET", url)


def test_s3() -> None:
    """Test the S3 file"""

    s3_file = f"s3://{TEST_BUCKET_NAME}/test.txt"
    _test_read_file(s3_file)
    _test_write_file(s3_file)

    _test_read_binary_file(s3_file)
    _test_write_binary_file(s3_file)

    _test_list_files(f"s3://{TEST_BUCKET_NAME}/test_path/")

    _test_most_recent_timestamp(f"s3://{TEST_BUCKET_NAME}/test_path/")


def test_s3_public_url() -> None:
    """Test the S3 public URLs"""
    s3_file = f"s3://{TEST_BUCKET_NAME}/test.txt"

    with BinaryFileHandler(s3_file, "wb", content_type="application/json") as f:
        f.write(b"test")
    response = _fetch_url(get_public_url(s3_file))
    assert response.status == 200
    assert response.data == b"test"
    assert response.headers["Content-Type"] == "application/json"
    delete_file(s3_file)


def test_copy_file(tmp_path: Path) -> None:
    """Test the copy_file function"""

    for path in [f"s3://{TEST_BUCKET_NAME}/", str(tmp_path)]:
        src_file = os.path.join(path, "src.txt")
        dst_file = os.path.join(path, "dest.txt")

        # Copy a file that does not exist
        write_file(src_file, "test")
        copy_file(src_file, dst_file)
        assert read_file(dst_file) == "test"

        # Copy a file that exists
        write_file(src_file, "test 2")
        copy_file(src_file, dst_file)
        assert read_file(dst_file) == "test 2"

        # Delete the files
        delete_file(src_file)
        delete_file(dst_file)


def test_move_file(tmp_path: Path) -> None:
    """Test the move_file function"""

    for path in [f"s3://{TEST_BUCKET_NAME}/", str(tmp_path)]:
        src_file = os.path.join(path, "src.txt")
        dst_file = os.path.join(path, "dest.txt")

        # Move a file that does not exist
        write_file(src_file, "test")
        move_file(src_file, dst_file)
        assert read_file(dst_file) == "test"
        assert file_exists(src_file) is False

        # Move a file that exists
        write_file(src_file, "test 2")
        move_file(src_file, dst_file)
        assert read_file(dst_file) == "test 2"
        assert file_exists(src_file) is False

        # Delete the files
        delete_file(dst_file)


def test_list_files_pagination(monkeypatch: pytest.MonkeyPatch) -> None:

    mock_s3_client = MagicMock()
    monkeypatch.setattr("mx8fs.file_io.s3_client", mock_s3_client)
    mock_s3_client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": f"file{i}.txt"} for i in range(5)],
            "IsTruncated": True,
            "NextContinuationToken": "token1",
        },
        {
            "Contents": [{"Key": f"file{i}.txt"} for i in range(5, 10)],
            "IsTruncated": False,
        },
    ]

    # Act
    files = list_files("s3://bucket/path", "txt")

    # Assert
    expected_files = [f"file{i}" for i in range(10)]
    assert files == expected_files
