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
from contextlib import nullcontext
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
import urllib3
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from mx8fs import (
    BinaryFileHandler,
    FileMetadata,
    FileNotDeletedError,
    FileVersionDeletedError,
    FileVersionMetadata,
    GzipFileHandler,
    VersioningNotEnabledError,
    VersioningNotSupportedError,
    VersionMismatchError,
    VersionNotFoundError,
    copy_file,
    delete_file,
    file_exists,
    get_folders,
    get_public_url,
    list_file_versions,
    list_files,
    list_files_with_metadata,
    most_recent_timestamp,
    move_file,
    read_file,
    read_file_version,
    read_file_with_version,
    restore_file_version,
    undelete_file,
    update_file_if_version_matches,
    write_file,
)
from mx8fs.file_io import (
    _get_file_version,
    _list_current_file_versions,
    get_bucket_key,
    get_files,
    purge_folder,
    s3_client,
)

TEST_BUCKET_NAME = "mx8-test-bucket/mx8fs"


def _test_read_file(file: str) -> None:
    """Test the read_file function"""
    delete_file(file)
    with pytest.raises(FileNotFoundError):
        read_file(file)
    with pytest.raises(FileNotFoundError):
        _get_file_version(file)


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
    ignored_file = os.path.join(path, "ignored.json")
    write_file(ignored_file, "ignored")

    versions = _list_current_file_versions(path, "txt")
    assert set(versions) == {"test1", "test2"}
    assert versions["test1"] == _get_file_version(os.path.join(path, TEST_FILE_1))

    metadata = sorted(list_files_with_metadata(path, "txt"), key=lambda file: file.name)
    assert [file.name for file in metadata] == ["test1", "test2"]
    assert all(isinstance(file, FileMetadata) for file in metadata)
    assert all(file.last_modified.tzinfo is UTC for file in metadata)
    assert [file.size_bytes for file in metadata] == [5, 5]
    assert metadata[0].version == versions["test1"]
    assert [file.name for file in list_files_with_metadata(path, "txt", "test1")] == ["test1"]

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
    delete_file(ignored_file)

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


def test_local_versioning_defaults_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MX8FS_LOCAL_VERSIONING", raising=False)
    file = str(tmp_path / "test.txt")

    write_file(file, "one")

    assert not (tmp_path / ".mx8fs-versions").exists()
    with pytest.raises(VersioningNotEnabledError):
        list_file_versions(file)
    with pytest.raises(VersioningNotEnabledError):
        list_files(str(tmp_path), "txt", include_deleted=True)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_local_versioning_enabled_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", value)
    file = str(tmp_path / "test.txt")

    write_file(file, "one")

    assert list_file_versions(file)[0].is_latest


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", ""])
def test_local_versioning_disabled_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", value)
    write_file(str(tmp_path / "test.txt"), "one")
    assert not (tmp_path / ".mx8fs-versions").exists()


def test_local_versioning_rejects_invalid_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "perhaps")
    with pytest.raises(ValueError, match="Invalid MX8FS_LOCAL_VERSIONING"):
        write_file(str(tmp_path / "test.txt"), "one")


def test_local_version_history_restore_and_undelete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "test.txt"
    file.write_text("existing", encoding="utf-8")
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")

    write_file(str(file), "updated")
    versions = list_file_versions(str(file))
    assert len(versions) == 2
    assert all(isinstance(version, FileVersionMetadata) for version in versions)
    assert read_file(str(file), version_id=versions[0].version_id) == "updated"
    assert read_file(str(file), version_id=versions[1].version_id) == "existing"
    assert read_file_with_version(str(file), version_id=versions[1].version_id) == (
        "existing",
        versions[1].revision,
    )

    with monkeypatch.context() as context:
        context.setattr(
            "mx8fs.file_io.list_file_versions",
            lambda _: pytest.fail("known-version reads must not list versions"),
        )
        assert read_file_version(str(file), versions[1]) == "existing"

    original_version = versions[1].version_id
    restored = restore_file_version(str(file), original_version)
    assert restored.is_latest
    assert read_file(str(file)) == "existing"

    delete_file(str(file))
    deleted_versions = list_file_versions(str(file))
    assert deleted_versions[0].is_deleted
    assert list_files(str(tmp_path), "txt") == []
    deleted_metadata = list_files_with_metadata(str(tmp_path), "txt", include_deleted=True)
    assert [(item.name, item.is_deleted) for item in deleted_metadata] == [("test", True)]
    assert deleted_metadata[0].latest_readable_version == deleted_versions[1]
    assert read_file_version(str(file), deleted_metadata[0].latest_readable_version) == "existing"
    assert list_files(str(tmp_path), "txt", include_deleted=True) == ["test"]
    with pytest.raises(FileVersionDeletedError):
        read_file(str(file), version_id=deleted_versions[0].version_id)
    with pytest.raises(FileVersionDeletedError):
        read_file_version(str(file), deleted_versions[0])

    undeleted = undelete_file(str(file))
    assert undeleted.is_latest
    assert not undeleted.is_deleted
    assert read_file(str(file)) == "existing"
    with pytest.raises(FileNotDeletedError):
        undelete_file(str(file))

    assert ".mx8fs-versions" not in get_folders(str(tmp_path))
    assert all(not path.startswith(".mx8fs-versions") for path in get_files(str(tmp_path)))


def test_local_version_history_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    file = str(tmp_path / "missing.txt")

    assert list_file_versions(file) == []
    delete_file(file)
    with pytest.raises(VersionNotFoundError):
        read_file(file, version_id="missing")
    with pytest.raises(FileNotFoundError, match="no recoverable versions"):
        undelete_file(file)
    with pytest.raises(VersioningNotSupportedError):
        list_file_versions("https://example.test/file.txt")
    with pytest.raises(VersioningNotSupportedError):
        read_file("https://example.test/file.txt", version_id="version")

    deleted = FileVersionMetadata(
        name="missing.txt",
        version_id="deleted",
        last_modified=datetime.now(UTC),
        size_bytes=0,
        is_latest=True,
        is_deleted=True,
        revision=None,
    )
    monkeypatch.setattr("mx8fs.file_io.list_file_versions", lambda _: [deleted])
    with pytest.raises(FileNotFoundError, match="no recoverable versions"):
        undelete_file(file)
    no_current = FileVersionMetadata(
        name="missing.txt",
        version_id="stale",
        last_modified=datetime.now(UTC),
        size_bytes=0,
        is_latest=False,
        is_deleted=True,
        revision=None,
    )
    monkeypatch.setattr("mx8fs.file_io.list_file_versions", lambda _: [no_current])
    with pytest.raises(FileNotFoundError, match="no current version"):
        undelete_file(file)


def test_version_selection_uses_explicit_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = str(tmp_path / "test.txt")
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 2, tzinfo=UTC)
    recoverable = FileVersionMetadata(
        name="test.txt",
        version_id="recoverable",
        last_modified=newer,
        size_bytes=3,
        is_latest=False,
        is_deleted=False,
        revision="etag",
    )
    deleted = FileVersionMetadata(
        name="test.txt",
        version_id="deleted",
        last_modified=older,
        size_bytes=0,
        is_latest=True,
        is_deleted=True,
        revision=None,
    )
    stale = FileVersionMetadata(
        name="test.txt",
        version_id="stale",
        last_modified=older,
        size_bytes=3,
        is_latest=False,
        is_deleted=False,
        revision="etag",
    )

    monkeypatch.setattr("mx8fs.file_io._list_versions", lambda *_args, **_kwargs: [recoverable, deleted])
    assert list_files_with_metadata(str(tmp_path), "txt", include_deleted=True)[0].is_deleted

    monkeypatch.setattr("mx8fs.file_io.list_file_versions", lambda _: [stale, deleted, recoverable])
    monkeypatch.setattr("mx8fs.file_io._version_metadata", lambda *_: recoverable)
    monkeypatch.setattr("mx8fs.file_io._current_version_metadata", lambda _: deleted)
    monkeypatch.setattr("mx8fs.file_io.read_file_version", lambda *_args, **_kwargs: "old")
    monkeypatch.setattr("mx8fs.file_io.write_file", lambda *_args, **_kwargs: "new")
    assert restore_file_version(file, "recoverable") == deleted

    restored: list[str] = []

    def restore(_: str, version: FileVersionMetadata) -> FileVersionMetadata:
        restored.append(version.version_id)
        return deleted

    monkeypatch.setattr("mx8fs.file_io._restore_file_version", restore)
    undelete_file(file)
    assert restored == ["recoverable"]


def test_local_binary_write_open_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    real_open = open

    def fail_open(file: str, *args: Any, **kwargs: Any) -> Any:
        if file == str(tmp_path / "blocked.bin"):
            raise PermissionError("blocked")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_open)
    with pytest.raises(PermissionError, match="blocked"):
        BinaryFileHandler(str(tmp_path / "blocked.bin"), "wb")


def test_local_binary_copy_and_move_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    source = str(tmp_path / "source.bin")
    copied = str(tmp_path / "copied.bin")
    moved = str(tmp_path / "moved.bin")

    with BinaryFileHandler(source, "wb") as output:
        output.write(b"binary")
    copy_file(source, copied)
    move_file(copied, moved)

    assert len(list_file_versions(source)) == 1
    assert len(list_file_versions(moved)) == 1
    assert list_file_versions(copied)[0].is_deleted


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


def test_s3_version_lookup_propagates_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadObject")

    def access_denied(**_: Any) -> None:
        raise error

    monkeypatch.setattr(s3_client, "head_object", access_denied)

    with pytest.raises(ClientError, match="AccessDenied"):
        _get_file_version("s3://bucket/key")


def test_s3_version_history_and_deleted_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    modified = datetime(2026, 1, 2, tzinfo=UTC)
    version_page = {
        "Versions": [
            {
                "Key": "root/live.txt",
                "VersionId": "live-version",
                "LastModified": modified,
                "Size": 4,
                "IsLatest": True,
                "ETag": '"live-etag"',
            },
            {
                "Key": "root/deleted.txt",
                "VersionId": "old-version",
                "LastModified": modified.replace(day=1),
                "Size": 3,
                "IsLatest": False,
                "ETag": '"old-etag"',
            },
            {
                "Key": "root/deleted.txt.extra",
                "VersionId": "unrelated",
                "LastModified": modified,
                "Size": 1,
                "IsLatest": True,
                "ETag": '"unrelated"',
            },
        ],
        "DeleteMarkers": [
            {
                "Key": "root/deleted.txt",
                "VersionId": "delete-version",
                "LastModified": modified,
                "IsLatest": True,
            }
        ],
    }
    current_page = {
        "Contents": [
            {
                "Key": "root/live.txt",
                "LastModified": modified,
                "Size": 4,
                "ETag": '"live-etag"',
            }
        ]
    }

    class Paginator:
        def __init__(self, pages: list[dict[str, Any]]) -> None:
            self.pages = pages

        def paginate(self, **_: Any) -> list[dict[str, Any]]:
            return self.pages

    paginator_calls: list[str] = []

    def get_paginator(name: str) -> Paginator:
        paginator_calls.append(name)
        return Paginator([version_page if name == "list_object_versions" else current_page])

    monkeypatch.setattr(s3_client, "get_paginator", get_paginator)
    monkeypatch.setattr(
        s3_client,
        "get_object",
        lambda **_: {"Body": BytesIO(b"old"), "ETag": '"old-etag"'},
    )

    history = list_file_versions("s3://bucket/root/deleted.txt")
    assert [version.version_id for version in history] == ["delete-version", "old-version"]
    assert history[0].is_deleted
    monkeypatch.setattr(
        "mx8fs.file_io.list_file_versions",
        lambda _: pytest.fail("version reads must not enumerate history"),
    )
    assert read_file("s3://bucket/root/deleted.txt", version_id="old-version") == "old"
    assert read_file_with_version("s3://bucket/root/deleted.txt", version_id="old-version") == ("old", "old-etag")

    metadata = sorted(
        list_files_with_metadata("s3://bucket/root/", "txt", include_deleted=True),
        key=lambda item: item.name,
    )
    assert [(item.name, item.is_deleted) for item in metadata] == [
        ("deleted", True),
        ("live", False),
    ]
    assert metadata[0].latest_readable_version is not None
    assert metadata[0].latest_readable_version.version_id == "old-version"
    assert read_file_version("s3://bucket/root/deleted.txt", metadata[0].latest_readable_version) == "old"
    assert metadata[1].latest_readable_version is not None
    assert metadata[1].latest_readable_version.version_id == "live-version"
    assert paginator_calls == ["list_object_versions", "list_object_versions"]


def test_s3_historical_read_translates_missing_version(monkeypatch: pytest.MonkeyPatch) -> None:
    modified = datetime(2026, 1, 2, tzinfo=UTC)

    class Paginator:
        def paginate(self, **_: Any) -> list[dict[str, Any]]:
            return [
                {
                    "Versions": [
                        {
                            "Key": "key.txt",
                            "VersionId": "version",
                            "LastModified": modified,
                            "Size": 4,
                            "IsLatest": True,
                            "ETag": '"etag"',
                        }
                    ]
                }
            ]

    monkeypatch.setattr(s3_client, "get_paginator", lambda _: Paginator())
    error = ClientError({"Error": {"Code": "NoSuchVersion", "Message": "missing"}}, "GetObject")
    monkeypatch.setattr(s3_client, "get_object", lambda **_: (_ for _ in ()).throw(error))

    with pytest.raises(VersionNotFoundError):
        read_file("s3://bucket/key.txt", version_id="version")

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
    monkeypatch.setattr(s3_client, "get_object", lambda **_: (_ for _ in ()).throw(denied))
    with pytest.raises(ClientError, match="AccessDenied"):
        read_file("s3://bucket/key.txt", version_id="version")

    deleted = ClientError({"Error": {"Code": "MethodNotAllowed", "Message": "deleted"}}, "GetObject")
    monkeypatch.setattr(s3_client, "get_object", lambda **_: (_ for _ in ()).throw(deleted))
    with pytest.raises(FileVersionDeletedError):
        read_file("s3://bucket/key.txt", version_id="version")


def test_s3_restore_uses_server_side_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    modified = datetime(2026, 1, 2, tzinfo=UTC)
    copied: list[dict[str, Any]] = []
    monkeypatch.setattr(s3_client, "copy_object", lambda **kwargs: copied.append(kwargs))
    monkeypatch.setattr(
        s3_client,
        "head_object",
        lambda **_: {
            "VersionId": "restored",
            "ETag": '"etag"',
            "LastModified": modified,
            "ContentLength": 4,
        },
    )

    restored = restore_file_version("s3://bucket/key.txt", "historical")

    assert restored.version_id == "restored"
    assert copied == [
        {
            "Bucket": "bucket",
            "Key": "key.txt",
            "CopySource": {"Bucket": "bucket", "Key": "key.txt", "VersionId": "historical"},
        }
    ]


def test_s3_restore_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = ClientError({"Error": {"Code": "NoSuchVersion", "Message": "missing"}}, "CopyObject")
    monkeypatch.setattr(s3_client, "copy_object", lambda **_: (_ for _ in ()).throw(missing))
    with pytest.raises(VersionNotFoundError):
        restore_file_version("s3://bucket/key.txt", "missing")

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CopyObject")
    monkeypatch.setattr(s3_client, "copy_object", lambda **_: (_ for _ in ()).throw(denied))
    with pytest.raises(ClientError, match="AccessDenied"):
        restore_file_version("s3://bucket/key.txt", "denied")

    monkeypatch.setattr(s3_client, "copy_object", lambda **_: {})
    not_found = ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
    monkeypatch.setattr(s3_client, "head_object", lambda **_: (_ for _ in ()).throw(not_found))
    with pytest.raises(FileNotFoundError):
        restore_file_version("s3://bucket/key.txt", "version")

    monkeypatch.setattr(s3_client, "head_object", lambda **_: (_ for _ in ()).throw(denied))
    with pytest.raises(ClientError, match="AccessDenied"):
        restore_file_version("s3://bucket/key.txt", "version")


def test_s3_undelete_removes_current_delete_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    modified = datetime(2026, 1, 2, tzinfo=UTC)
    deleted = ClientError(
        cast(
            Any,
            {
                "Error": {"Code": "404", "Message": "delete marker"},
                "ResponseMetadata": {
                    "HTTPHeaders": {
                        "x-amz-delete-marker": "true",
                        "x-amz-version-id": "deleted-version",
                    }
                },
            },
        ),
        "HeadObject",
    )
    older_deleted = ClientError(
        cast(
            Any,
            {
                "Error": {"Code": "404", "Message": "delete marker"},
                "ResponseMetadata": {
                    "HTTPHeaders": {
                        "x-amz-delete-marker": "true",
                        "x-amz-version-id": "older-deleted-version",
                    }
                },
            },
        ),
        "HeadObject",
    )
    responses: list[Any] = [
        deleted,
        older_deleted,
        {
            "VersionId": "revealed-version",
            "ETag": '"etag"',
            "LastModified": modified,
            "ContentLength": 4,
        },
    ]

    def head_object(**_: Any) -> dict[str, Any]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(dict[str, Any], response)

    removed: list[dict[str, Any]] = []
    monkeypatch.setattr(s3_client, "head_object", head_object)
    monkeypatch.setattr(s3_client, "delete_object", lambda **kwargs: removed.append(kwargs))

    restored = undelete_file("s3://bucket/key.txt")

    assert restored.version_id == "revealed-version"
    assert removed == [
        {"Bucket": "bucket", "Key": "key.txt", "VersionId": "deleted-version"},
        {"Bucket": "bucket", "Key": "key.txt", "VersionId": "older-deleted-version"},
    ]


def test_s3_undelete_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_client, "head_object", lambda **_: {})
    with pytest.raises(FileNotDeletedError):
        undelete_file("s3://bucket/key.txt")

    missing = ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
    monkeypatch.setattr(s3_client, "head_object", lambda **_: (_ for _ in ()).throw(missing))
    with pytest.raises(FileNotFoundError, match="no recoverable versions"):
        undelete_file("s3://bucket/key.txt")

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadObject")
    monkeypatch.setattr(s3_client, "head_object", lambda **_: (_ for _ in ()).throw(denied))
    with pytest.raises(ClientError, match="AccessDenied"):
        undelete_file("s3://bucket/key.txt")

    deleted = ClientError(
        cast(
            Any,
            {
                "Error": {"Code": "404", "Message": "delete marker"},
                "ResponseMetadata": {
                    "HTTPHeaders": {
                        "x-amz-delete-marker": "true",
                        "x-amz-version-id": "deleted-version",
                    }
                },
            },
        ),
        "HeadObject",
    )
    responses = [deleted, missing]

    def head_object(**_: Any) -> dict[str, Any]:
        raise responses.pop(0)

    monkeypatch.setattr(s3_client, "head_object", head_object)
    monkeypatch.setattr(s3_client, "delete_object", lambda **_: {})
    with pytest.raises(FileNotFoundError, match="no recoverable versions"):
        undelete_file("s3://bucket/key.txt")


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

    local_source = tmp_path / "local-source.txt"
    local_destination = tmp_path / "local-destination.txt"
    https_destination = tmp_path / "https-destination.txt"
    s3_destination = f"s3://{TEST_BUCKET_NAME}/copy-destination.txt"
    https_s3_destination = f"s3://{TEST_BUCKET_NAME}/copy-https-destination.txt"
    local_source.write_text("streamed test", encoding="UTF-8")

    copy_file(str(local_source), s3_destination)
    assert read_file(s3_destination) == "streamed test"

    copy_file(s3_destination, str(local_destination))
    assert local_destination.read_text(encoding="UTF-8") == "streamed test"

    https_source = get_public_url(s3_destination)
    copy_file(https_source, str(https_destination))
    assert https_destination.read_text(encoding="UTF-8") == "streamed test"

    copy_file(https_source, https_s3_destination)
    assert read_file(https_s3_destination) == "streamed test"

    missing_s3_source = f"s3://{TEST_BUCKET_NAME}/copy-missing-source.txt"
    delete_file(missing_s3_source)
    with pytest.raises(FileNotFoundError):
        copy_file(missing_s3_source, str(local_destination))

    delete_file(s3_destination)
    delete_file(https_s3_destination)


def test_copy_file_streams_https_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that HTTPS copies never read the complete response at once."""

    class BoundedResponse:
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.offset = 0

        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 4
            chunk = self.data[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    response = BoundedResponse(b"streamed content")
    monkeypatch.setattr("mx8fs.file_io._get_response", lambda _: nullcontext(response))
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"old content")
    destination.chmod(0o640)

    copy_file("https://example.test/source.bin", str(destination), chunk_size=4)

    assert destination.read_bytes() == b"streamed content"
    assert destination.stat().st_mode & 0o777 == 0o640


def test_copy_file_preserves_local_destination_on_interruption(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that interrupted copies do not publish partial local files."""

    class InterruptedResponse:
        calls = 0

        def read(self, size: int = -1) -> bytes:
            assert size == 4
            self.calls += 1
            if self.calls == 1:
                return b"part"
            raise OSError("connection interrupted")

    monkeypatch.setattr("mx8fs.file_io._get_response", lambda _: nullcontext(InterruptedResponse()))
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"existing content")

    with pytest.raises(OSError, match="connection interrupted"):
        copy_file("https://example.test/source.bin", str(destination), chunk_size=4)

    assert destination.read_bytes() == b"existing content"
    assert list(tmp_path.iterdir()) == [destination]


def test_copy_file_uses_bounded_s3_transfer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that local-to-S3 copies use a bounded sequential managed transfer."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"source content")
    captured: dict[str, Any] = {}

    def upload_fileobj(**kwargs: Any) -> None:
        captured["data"] = kwargs["Fileobj"].read()
        captured["bucket"] = kwargs["Bucket"]
        captured["key"] = kwargs["Key"]
        captured["config"] = kwargs["Config"]

    monkeypatch.setattr(s3_client, "upload_fileobj", upload_fileobj)

    copy_file(str(source), "s3://bucket/destination.bin")

    assert captured["data"] == b"source content"
    assert captured["bucket"] == "bucket"
    assert captured["key"] == "destination.bin"
    assert captured["config"].multipart_threshold == 8 * 1024 * 1024
    assert captured["config"].multipart_chunksize == 8 * 1024 * 1024
    assert captured["config"].use_threads is False


def test_copy_file_translates_s3_write_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that managed upload errors retain the existing public semantics."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"source content")

    def upload_fileobj(**_: Any) -> None:
        raise s3_client.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "CreateMultipartUpload",
        )

    monkeypatch.setattr(s3_client, "upload_fileobj", upload_fileobj)

    with pytest.raises(PermissionError, match="Cannot write to s3://bucket/destination.bin"):
        copy_file(str(source), "s3://bucket/destination.bin")


def test_copy_file_aborts_interrupted_multipart_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that interrupted HTTPS transfers abort their multipart upload."""

    class InterruptedResponse:
        calls = 0

        def read(self, size: int = -1) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"x" * size
            raise OSError("connection interrupted")

    monkeypatch.setattr("mx8fs.file_io._get_response", lambda _: nullcontext(InterruptedResponse()))

    with Stubber(s3_client) as stubber:
        stubber.add_response("create_multipart_upload", {"UploadId": "upload-id"})
        stubber.add_response("upload_part", {"ETag": "etag"})
        stubber.add_response("abort_multipart_upload", {})

        with pytest.raises(OSError, match="connection interrupted"):
            copy_file("https://example.test/source.bin", "s3://bucket/destination.bin")

        stubber.assert_no_pending_responses()


def test_copy_file_rejects_invalid_options(tmp_path: Path) -> None:
    """Test invalid copy destinations and buffer sizes."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"source content")

    with pytest.raises(ValueError, match="chunk_size must be greater than zero"):
        copy_file(str(source), str(tmp_path / "destination.bin"), chunk_size=0)

    with pytest.raises(NotImplementedError, match="Only 'rb' mode is supported"):
        copy_file(str(source), "https://example.test/destination.bin")


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
        written_version = update_file_if_version_matches(test_file, "test 2", version)
        assert read_file(test_file) == "test 2"
        contents, version_2 = read_file_with_version(test_file)
        assert contents == "test 2"
        assert written_version == version_2
        assert version_2 != version

        # Write and check we cannot overwrite the file
        time.sleep(1)
        write_file(test_file, "test 3")
        with pytest.raises(VersionMismatchError):
            update_file_if_version_matches(test_file, "test 4", version_2)

        assert read_file(test_file) == "test 3"

        delete_file(test_file)


def test_write_file_accepts_bare_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    version = write_file("relative.txt", "contents")

    assert read_file_with_version("relative.txt") == ("contents", version)


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


def test_get_bucket_key() -> None:
    """Test get_bucket_key with various S3 paths."""
    assert get_bucket_key("s3://my-bucket/my-key") == ("my-bucket", "my-key")
    assert get_bucket_key("s3://my-bucket/") == ("my-bucket", "")
    assert get_bucket_key("s3://my-bucket") == ("my-bucket", "")


@pytest.mark.parametrize("path", [f"s3://{TEST_BUCKET_NAME}/cutoff_test/", None])
def test_get_files_and_purge_local_with_cutoff(path: str, tmp_path: Path) -> None:

    root = path or str(tmp_path)

    old_path = os.path.join(root, "old.txt")
    new_path = os.path.join(root, "new.txt")

    # Create the older object
    write_file(old_path, "old")

    # Ensure different LastModified timestamps
    time.sleep(2)
    cutoff = datetime.now(UTC)
    time.sleep(2)

    # Create the newer object
    write_file(new_path, "new")

    files = sorted(get_files(root, cutoff_date=cutoff))
    assert files == ["old.txt"]

    # Dry run purge should list only the old file
    listed = purge_folder(root, dry_run=True, cutoff_date=cutoff)
    assert set(listed) == {old_path}
    assert file_exists(old_path)
    assert file_exists(new_path)

    # Actual purge should delete only the old file
    deleted = purge_folder(root, dry_run=False, cutoff_date=cutoff)
    assert set(deleted) == {old_path}
    assert not file_exists(old_path)
    assert file_exists(new_path)

    # Cleanup
    delete_file(new_path)
