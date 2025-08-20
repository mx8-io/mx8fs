"""
Test the AWS function

Copyright (c) 2023-2025 MX8 Inc
All rights reserved.

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

import os
import time
from pathlib import Path

import pytest
import urllib3

from mx8fs import (
    BinaryFileHandler,
    GzipFileHandler,
    VersionMismatchError,
    copy_file,
    delete_file,
    file_exists,
    get_folders,
    get_public_url,
    list_files,
    most_recent_timestamp,
    move_file,
    read_file,
    read_file_with_version,
    update_file_if_version_matches,
    write_file,
)
from mx8fs.file_io import get_files, purge_folder

TEST_BUCKET_NAME = "mx8-test-bucket/mx8fs"


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
    delete_file(file)
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
    for f in files:
        delete_file(path + f + ".txt")
    assert len(files) == 0

    write_file(os.path.join(path, TEST_FILE_1), "test1")
    write_file(os.path.join(path, TEST_FILE_2), "test2")

    for files in [sorted(list_files(path, "txt")), sorted(list_files(path, "txt", "test"))]:
        assert len(files) == 2
        assert files[0] == "test1"
        assert files[1] == "test2"

    assert len(list_files(path, "txt", "test1")) == 1
    assert len(list_files(path, "txt", "notest")) == 0

    assert len(get_files(path, "test1")) == 1
    assert len(get_files(path, "notest")) == 0

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


def test_s3_public_url_get_then_put() -> None:
    """Test the S3 public URLs"""
    s3_file = f"s3://{TEST_BUCKET_NAME}/test_get_then_put.txt"

    with BinaryFileHandler(s3_file, "wb", content_type="application/json") as f:
        f.write(b"test")
    get_url = get_public_url(s3_file)

    get_response = _fetch_url(get_url)
    assert get_response.status == 200
    assert get_response.data == b"test"
    assert get_response.headers["Content-Type"] == "application/json"

    put_url = get_public_url(s3_file, method="put_object")
    assert put_url != get_url  # Ensure PUT URL is different

    http = urllib3.PoolManager()
    put_response = http.request("PUT", put_url, body=b"test_put", headers={"Content-Type": "application/json"})
    assert put_response.status == 200

    get_response = _fetch_url(get_url)
    assert get_response.status == 200
    assert get_response.data == b"test_put"

    delete_file(s3_file)


def test_s3_public_url_put_then_get() -> None:
    """Test the S3 public URLs"""
    s3_file = f"s3://{TEST_BUCKET_NAME}/test_put_then_get.txt"

    put_url = get_public_url(s3_file, method="put_object")
    http = urllib3.PoolManager()
    put_response = http.request("PUT", put_url, body=b"test_put", headers={"Content-Type": "application/json"})
    assert put_response.status == 200

    get_url = get_public_url(s3_file)

    get_response = _fetch_url(get_url)
    assert get_response.status == 200
    assert get_response.data == b"test_put"
    assert get_response.headers["Content-Type"] == "application/json"

    assert put_url != get_url  # Ensure PUT URL is different

    delete_file(s3_file)


def test_https_binary_file_handler() -> None:
    """Test BinaryFileHandler with https:// URLs (read-only) and edge cases."""
    s3_file = f"s3://{TEST_BUCKET_NAME}/test_https.txt"
    test_data = b"https test data"
    # Write to S3 and get public URL
    with BinaryFileHandler(s3_file, "wb") as f:
        f.write(test_data)
    https_url = get_public_url(s3_file)
    # Read from HTTPS using BinaryFileHandler
    with BinaryFileHandler(https_url, "rb") as f:
        assert f.read() == test_data
    # Attempt to open in write mode (should fail)
    with pytest.raises(NotImplementedError):
        with BinaryFileHandler(https_url, "wb") as f:
            raise AssertionError("Expected NotImplementedError")
    # Attempt to open in text mode (should fail)
    with pytest.raises(NotImplementedError):
        with BinaryFileHandler(https_url, "r") as f:
            raise AssertionError("Expected NotImplementedError")
    # Edge case: 404 URL
    bad_url = https_url + ".notfound"
    with pytest.raises(FileNotFoundError):
        with BinaryFileHandler(bad_url, "rb") as f:
            f.read()
    # Edge case: invalid URL
    with pytest.raises(FileNotFoundError):
        with BinaryFileHandler("https://invalid.invaliddomain/test.txt", "rb") as f:
            f.read()
    # Clean up
    delete_file(s3_file)


def test_https_read_file() -> None:
    """Test read_file with https:// URLs and edge cases."""
    s3_file = f"s3://{TEST_BUCKET_NAME}/test_https_read.txt"
    test_data = "https read test data"
    # Write to S3 and get public URL
    with BinaryFileHandler(s3_file, "wb") as f:
        f.write(test_data.encode("utf-8"))
    https_url = get_public_url(s3_file)
    # Read from HTTPS using read_file
    assert read_file(https_url) == test_data
    # Edge case: 404 URL
    bad_url = https_url + ".notfound"
    with pytest.raises(FileNotFoundError):
        read_file(bad_url)
    # Edge case: invalid URL
    with pytest.raises(FileNotFoundError):
        read_file("https://invalid.invaliddomain/test.txt")
    # Clean up
    delete_file(s3_file)


def test_copy_file(tmp_path: Path) -> None:
    """Test the copy_file function"""

    for path in [f"s3://{TEST_BUCKET_NAME}/", str(tmp_path)]:
        src_file = os.path.join(path, "src.txt")
        dst_file = os.path.join(path, "dest.txt")

        # Copy a file that does not exist
        delete_file(src_file)
        with pytest.raises(FileNotFoundError):
            copy_file(src_file, dst_file)

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


@pytest.mark.parametrize("base_path", [f"s3://{TEST_BUCKET_NAME}/gzip_test/", None])
def test_gzip_file_handler(tmp_path: Path, base_path: str) -> None:
    """
    Test GzipFileHandler for both S3 and local paths, in binary and text modes.
    """
    if base_path is None:
        base_path = str(tmp_path)
    gzip_file = os.path.join(base_path, "test.gz")

    # Clean up before test
    delete_file(gzip_file)

    # Test writing and reading in binary mode
    data = b"hello gzip binary"
    with GzipFileHandler(gzip_file, "wb") as f:
        f.write(data)
    assert file_exists(gzip_file)
    with GzipFileHandler(gzip_file, "rb") as f:
        assert f.read() == data

    # Test writing and reading in text mode
    text = "hello gzip text"
    with GzipFileHandler(gzip_file, "wt", encoding="utf-8") as f:
        f.write(text)
    assert file_exists(gzip_file)
    with GzipFileHandler(gzip_file, "rt", encoding="utf-8") as f:
        assert f.read() == text

    # Test error on missing file (read)
    delete_file(gzip_file)
    with pytest.raises(FileNotFoundError):
        with GzipFileHandler(gzip_file, "rb") as f:
            f.read()
    with pytest.raises(FileNotFoundError):
        with GzipFileHandler(gzip_file, "rt", encoding="utf-8") as f:
            f.read()

    # Test unsupported mode
    with pytest.raises(NotImplementedError):
        with GzipFileHandler(gzip_file, "r") as f:
            raise AssertionError("Expected NotImplementedError")

    # Clean up after test
    delete_file(gzip_file)


@pytest.mark.parametrize("base_path", [f"s3://{TEST_BUCKET_NAME}/gzip_test_edge/", None])
def test_gzip_file_handler_edge_cases(tmp_path: Path, base_path: str) -> None:
    """
    Edge case tests for GzipFileHandler (S3 and local).
    """
    if base_path is None:
        base_path = str(tmp_path)
    gzip_file = os.path.join(base_path, "edge.gz")
    delete_file(gzip_file)

    # Empty file (binary)
    with GzipFileHandler(gzip_file, "wb") as f:
        assert f.closed is False

    with GzipFileHandler(gzip_file, "rb") as f:
        assert f.read() == b""

    # Empty file (text)
    with GzipFileHandler(gzip_file, "wt", encoding="utf-8") as f:
        assert f.closed is False

    with GzipFileHandler(gzip_file, "rt", encoding="utf-8") as f:
        assert f.read() == ""

    # Large file (binary)
    large_data = b"x" * 1024 * 1024  # 1MB
    with GzipFileHandler(gzip_file, "wb") as f:
        f.write(large_data)
    with GzipFileHandler(gzip_file, "rb") as f:
        assert f.read() == large_data

    # Invalid mode
    for bad_mode in ["a", "x", "r+", "w+", ""]:
        with pytest.raises(NotImplementedError):
            with GzipFileHandler(gzip_file, bad_mode) as f:
                raise AssertionError(f"Expected NotImplementedError for mode {bad_mode}")

    # Double close (should not raise)
    handler = GzipFileHandler(gzip_file, "rb")
    f = handler.__enter__()
    f.read()
    handler.__exit__(None, None, None)
    handler.__exit__(None, None, None)

    # Exception inside context manager (should still close)
    class CustomError(Exception):
        pass

    try:
        with GzipFileHandler(gzip_file, "rb") as f:
            raise CustomError("test")
    except CustomError:
        pass

    # Invalid encoding
    with pytest.raises(LookupError):
        with GzipFileHandler(gzip_file, "wt", encoding="not-an-encoding") as f:
            f.write("fail")

    # S3 network error (simulate by using a non-existent bucket)
    if base_path.startswith("s3://"):
        bad_s3_file = "s3://nonexistent-bucket/edge.gz"
        with pytest.raises(PermissionError):
            with GzipFileHandler(bad_s3_file, "wb") as f:
                f.write(b"fail")
        with pytest.raises(FileNotFoundError):
            with GzipFileHandler(bad_s3_file, "rb") as f:
                f.read()


def test_update_file(tmp_path: Path) -> None:
    """Test the update_file function"""

    for path in [f"s3://{TEST_BUCKET_NAME}/", str(tmp_path)]:
        test_file = os.path.join(path, "src.txt")

        # Try to read a file that does not exist
        delete_file(test_file)
        with pytest.raises(FileNotFoundError):
            read_file_with_version(test_file)

        # Try to update a file that does not exist
        with pytest.raises(FileNotFoundError):
            update_file_if_version_matches(test_file, "test", "bad_version")  # noqa: F821

        # Update a file that does exist
        write_file(test_file, "test")
        contents, version = read_file_with_version(test_file)
        assert contents == "test"
        update_file_if_version_matches(test_file, "test 2", version)
        assert read_file(test_file) == "test 2"
        contents, version_2 = read_file_with_version(test_file)
        assert contents == "test 2"
        assert version_2 != version

        # Write and check we cannot overwrite the file
        time.sleep(1)
        write_file(test_file, "test 3")
        with pytest.raises(VersionMismatchError):
            update_file_if_version_matches(test_file, "test 4", version_2)

        assert read_file(test_file) == "test 3"

        delete_file(test_file)


def test_purge_folder_local(tmp_path: Path) -> None:
    root = str(tmp_path)
    # Create nested directories
    subdir = os.path.join(root, "sub")
    os.makedirs(subdir, exist_ok=True)
    f1 = os.path.join(root, "test1.txt")
    f2 = os.path.join(subdir, "test2.txt")

    # Create files
    write_file(f1, "one")
    write_file(f2, "two")

    # Dry run should list files and not delete them
    listed = purge_folder(root, dry_run=True)
    assert set(listed) == {f1, f2}
    assert file_exists(f1)
    assert file_exists(f2)

    # Perform deletion
    deleted = purge_folder(root, dry_run=False)
    assert set(deleted) == {f1, f2}
    assert not file_exists(f1)
    assert not file_exists(f2)

    # Subsequent calls should return empty
    assert purge_folder(root, dry_run=True) == []


def test_purge_folder_s3() -> None:
    root = f"s3://{TEST_BUCKET_NAME}/purge_test/"

    f1 = os.path.join(root, "one.txt")
    f2 = os.path.join(root, "two.txt")

    # Ensure a clean state
    delete_file(f1)
    delete_file(f2)

    # Create S3 objects
    write_file(f1, "one")
    write_file(f2, "two")

    # Dry run should list the S3 paths and not delete
    listed = purge_folder(root, dry_run=True)
    # normalize expected paths (purge_folder constructs paths with root.rstrip('/')/<key>)
    expected = {f1, f2}
    assert set(listed) == expected
    assert file_exists(f1)
    assert file_exists(f2)

    # Perform deletion
    deleted = purge_folder(root, dry_run=False)
    assert set(deleted) == expected
    assert not file_exists(f1)
    assert not file_exists(f2)

    # Ensure no leftover objects
    assert get_files(root) == []


def test_get_folders_local(tmp_path: Path) -> None:
    """Test get_folders on the local filesystem (non-recursive)."""
    root = str(tmp_path)
    a = os.path.join(root, "a")
    os.makedirs(os.path.join(a, "sub"), exist_ok=True)
    os.makedirs(os.path.join(root, "b"), exist_ok=True)

    # Create files in the directories
    write_file(os.path.join(a, "one.txt"), "one")
    write_file(os.path.join(a, "sub", "two.txt"), "two")
    write_file(os.path.join(root, "b", "three.txt"), "three")

    folders = get_folders(root)
    assert set(folders) == {"a", "b"}

    # Clean up files
    delete_file(os.path.join(a, "one.txt"))
    delete_file(os.path.join(a, "sub", "two.txt"))
    delete_file(os.path.join(root, "b", "three.txt"))


def test_get_folders_s3() -> None:
    """Test get_folders on S3 (non-recursive)."""
    root = f"s3://{TEST_BUCKET_NAME}/folders_test/"

    # Ensure a clean state
    purge_folder(root, dry_run=False)

    # Create S3 objects that imply folders
    write_file(os.path.join(root, "a/one.txt"), "one")
    write_file(os.path.join(root, "a/sub/two.txt"), "two")
    write_file(os.path.join(root, "b/three.txt"), "three")

    folders = get_folders(root)
    assert set(folders) == {"a", "b"}

    # Clean up S3 objects
    purge_folder(root, dry_run=False)


def test_get_folders_local_with_prefix(tmp_path: Path) -> None:
    """Test get_folders on the local filesystem with a prefix filter (non-recursive)."""
    root = str(tmp_path)
    a = os.path.join(root, "a")
    os.makedirs(os.path.join(a, "sub"), exist_ok=True)
    os.makedirs(os.path.join(root, "b"), exist_ok=True)
    write_file(os.path.join(a, "one.txt"), "one")
    write_file(os.path.join(root, "b", "three.txt"), "three")

    # prefix that matches 'a'
    folders = get_folders(root, "a")
    assert set(folders) == {"a"}

    # prefix that matches none
    assert get_folders(root, "nope") == []


def test_get_folders_s3_with_prefix_and_empty() -> None:
    """Test get_folders on S3 with a prefix filter and empty results (non-recursive)."""
    root = f"s3://{TEST_BUCKET_NAME}/folders_test_prefix/"

    # Ensure a clean state
    purge_folder(root, dry_run=False)

    # Create S3 objects that imply folders
    write_file(os.path.join(root, "alpha/one.txt"), "one")
    write_file(os.path.join(root, "alpha/beta/two.txt"), "two")
    write_file(os.path.join(root, "bravo/three.txt"), "three")

    # prefix that matches 'alpha'
    folders = get_folders(root, "alpha")
    assert set(folders) == {"alpha"}

    # prefix that matches none should return empty list
    assert get_folders(root, "nope") == []

    # Ensure get_folders on an empty prefix (cleaned) returns []
    purge_folder(root, dry_run=False)
    assert get_folders(root) == []


def test_get_folders_local_nonexistent(tmp_path: Path) -> None:
    """get_folders should return an empty list for a non-existent local path."""
    non_existent = os.path.join(str(tmp_path), "does_not_exist")
    assert not os.path.exists(non_existent)
    assert get_folders(non_existent) == []


def test_get_folders_local_listdir_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If os.listdir raises FileNotFoundError, get_folders should return an empty list."""
    root = str(tmp_path)
    os.makedirs(root, exist_ok=True)

    # Simulate os.listdir raising FileNotFoundError for the created directory
    original_listdir = os.listdir

    def _raise(path: Path) -> None:
        raise FileNotFoundError()

    monkeypatch.setattr("os.listdir", _raise)
    try:
        assert get_folders(root) == []
    finally:
        # restore to be safe for other tests
        monkeypatch.setattr("os.listdir", original_listdir)


def test_get_folders_local_ignores_files(tmp_path: Path) -> None:
    """get_folders should ignore files in the root directory and only return directories."""
    root = str(tmp_path)
    # Create a file at the root and a directory
    write_file(os.path.join(root, "rootfile.txt"), "root")
    os.makedirs(os.path.join(root, "dir_only"), exist_ok=True)

    folders = get_folders(root)
    assert set(folders) == {"dir_only"}


# Clean up
def test_get_folders_s3_bucket_root() -> None:
    """
    Test get_folders on S3 at the root of the bucket (non-recursive).
    Ensures folders at the bucket root are detected correctly.
    """
    # Use bucket root (no trailing slash)
    bucket_root = f"s3://{TEST_BUCKET_NAME.split('/')[0]}/"

    # Clean up any test folders at the root
    for folder in ["root_a", "root_b"]:
        purge_folder(os.path.join(bucket_root, folder), dry_run=False)

    # Create S3 objects that imply folders at the root
    write_file(os.path.join(bucket_root, "root_a/file1.txt"), "data1")
    write_file(os.path.join(bucket_root, "root_b/file2.txt"), "data2")

    folders = get_folders(bucket_root)
    # Should find both folders at the root
    assert set(folders) >= {"root_a", "root_b"}

    # Clean up S3 objects
    purge_folder(os.path.join(bucket_root, "root_a"), dry_run=False)
    purge_folder(os.path.join(bucket_root, "root_b"), dry_run=False)

    # Now confirm the folders are gone
    folders = get_folders(bucket_root)
    assert {"root_a", "root_b"}.isdisjoint(set(folders))
