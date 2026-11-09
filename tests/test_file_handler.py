"""Integration tests for typed files, collections and backend-safe lifecycle operations."""

import gzip
from contextlib import nullcontext
from datetime import UTC, datetime
from io import BytesIO, UnsupportedOperation
from pathlib import Path
from typing import Any, Literal

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import BaseModel, ConfigDict

from mx8fs import (
    BinaryFileHandler,
    FileHandler,
    FileNotDeletedError,
    IndexUpdateError,
    TextFileHandler,
    VersioningNotSupportedError,
    VersionMismatchError,
    VersionNotFoundError,
    file_io,
    json_file_storage_factory,
    json_index_factory,
)


class Record(BaseModel):
    model_config = ConfigDict(strict=True)
    key: str = "a"
    value: str
    timestamp: datetime = datetime(2026, 11, 9, tzinfo=UTC)


@pytest.mark.parametrize("compression", [None, "gzip"])
@pytest.mark.parametrize(
    "data_type, original, updated",
    [
        (bytes, b"\x00\xff", b"\xfe\x00"),
        (str, "café\ntext", "updated text"),
        (dict, {"value": [1, True, None]}, {"updated": "value"}),
        (Record, Record(value="original"), Record(value="updated")),
    ],
)
def test_typed_file_lifecycle_and_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compression: Literal["gzip"] | None,
    data_type: type[Any],
    original: Any,
    updated: Any,
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    root = FileHandler[Any](str(tmp_path), data_type, compression=compression, fallback_uncompressed=True)
    handler = root.joinpath("record.data")
    assert not handler.exists()
    written = handler.write(original)
    assert handler.exists()
    assert handler.current_version() == written == handler.version
    assert handler.read_with_version() == (original, written)
    assert root.list("data") == ["record"]
    metadata = root.list_with_metadata("data")[0]
    assert metadata.version == written
    info = handler.metadata()
    assert info.name == "record.data" and info.size_bytes > 0 and info.version == written
    live_info = handler.metadata(include_deleted=True)
    assert live_info.latest_readable_version is not None
    first = handler.history()[0]
    handler.write(updated, expected_version=written)
    with pytest.raises(VersionMismatchError):
        handler.write(original, expected_version=written)
    assert handler.read(version_id=first) == original
    assert handler.read(version_id=first.version_id) == original
    assert handler.read_with_version(version_id=first)[0] == original
    assert handler.restore_version(first) == original
    with handler.get_lock():
        assert handler.read() == original
    handler.delete()
    assert not handler.exists()
    assert root.list("data") == []
    assert root.list("data", include_deleted=True) == ["record"]
    deleted = handler.metadata(include_deleted=True)
    assert deleted.is_deleted and deleted.latest_readable_version is not None
    assert handler.read(version_id=deleted.latest_readable_version) == original
    with pytest.raises(FileNotFoundError):
        handler.metadata()
    assert handler.undelete() == original
    assert handler.exists()
    with pytest.raises(FileNotDeletedError):
        handler.undelete()


@pytest.mark.parametrize("compression", [None, "gzip"])
def test_text_encoding_newlines_and_context_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compression: Literal["gzip"] | None,
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    path = str(tmp_path / "document")
    handler = FileHandler(path, str, compression=compression, encoding="utf-16", newline="")
    writer_version = handler.write("café\r\nsecond\rthird")
    assert handler.read() == "café\r\nsecond\rthird"
    normalized = FileHandler(path, str, compression=compression, encoding="utf-16")
    assert normalized.read() == "café\nsecond\nthird"
    raw = Path(handler.primary_path).read_bytes()
    assert (gzip.decompress(raw) if compression else raw).decode("utf-16") == "café\r\nsecond\rthird"
    assert handler.current_version() == writer_version
    with pytest.raises(RuntimeError, match="abort"):
        with TextFileHandler(handler.primary_path, "w", encoding="utf-16", compression=compression) as output:
            output.write("partial")
            raise RuntimeError("abort")
    assert Path(handler.primary_path).read_bytes() == raw
    with TextFileHandler(handler.primary_path, encoding="utf-16", compression=compression) as source:
        assert source.read(2) == "ca"
    with pytest.raises(UnsupportedOperation, match="not readable"):
        with TextFileHandler(handler.primary_path, "w", compression=compression) as output:
            output.read()
    with pytest.raises(UnsupportedOperation, match="not writable"):
        with TextFileHandler(handler.primary_path, compression=compression) as source:
            source.write("wrong mode")
    assert Path(handler.primary_path).read_bytes() == raw
    with pytest.raises(LookupError):
        with TextFileHandler(handler.primary_path, "w", encoding="invalid-encoding", compression=compression):
            pass
    assert Path(handler.primary_path).read_bytes() == raw
    handler.delete()
    assert handler.undelete() == "café\r\nsecond\rthird"


@pytest.mark.parametrize("backend", ["local", "s3", "https"])
@pytest.mark.parametrize("compression", [None, "gzip"])
@pytest.mark.parametrize("newline", ["auto", None, ""])
@pytest.mark.parametrize("typed", [False, True])
def test_text_newline_defaults_and_overrides_across_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    compression: Literal["gzip"] | None,
    newline: str | None,
    typed: bool,
) -> None:
    content = "first\r\nsecond\rthird\n"
    data = content.encode()
    if compression:
        data = gzip.compress(data)
    root = {"local": str(tmp_path), "s3": "s3://bucket", "https": "https://example.test"}[backend]
    name = "document.txt" + (".gz" if compression else "")
    path = f"{root}/{name}"
    if backend == "local":
        Path(path).write_bytes(data)
    elif backend == "https":
        monkeypatch.setattr(file_io, "_get_response", lambda _: nullcontext(BytesIO(data)))
    with Stubber(file_io.s3_client) if backend == "s3" else nullcontext() as stub:
        if stub is not None:
            stub.add_response(
                "get_object", {"Body": BytesIO(data), "ETag": '"version"'}, {"Bucket": "bucket", "Key": name}
            )
        if typed:
            handler = FileHandler(root, str, compression=compression, newline=newline).joinpath("document.txt")
            result = handler.read()
        else:
            with TextFileHandler(path, compression=compression, newline=newline) as source:
                result = source.read()
        expected = (
            "first\nsecond\nthird\n" if newline is None or (newline == "auto" and backend == "local") else content
        )
        assert result == expected
        if stub is not None:
            stub.assert_no_pending_responses()


def test_legacy_remote_text_helpers_preserve_current_and_historical_line_endings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "first\r\nsecond\rthird\n"
    data = content.encode()
    version = file_io.FileVersionMetadata(
        name="document.txt",
        version_id="history",
        last_modified=datetime(2026, 11, 9, tzinfo=UTC),
        size_bytes=len(data),
        is_latest=False,
        is_deleted=False,
        revision="etag",
    )
    with Stubber(file_io.s3_client) as stub:
        for historical in (False, False, True, True):
            params = {"Bucket": "bucket", "Key": "document.txt"}
            if historical:
                params["VersionId"] = version.version_id
            stub.add_response("get_object", {"Body": BytesIO(data), "ETag": '"etag"'}, params)
        path = "s3://bucket/document.txt"
        assert file_io.read_file(path) == content
        assert file_io.read_file_with_version(path) == (content, "etag")
        assert file_io.read_file(path, version_id=version.version_id) == content
        assert file_io.read_file_version(path, version) == content
        stub.assert_no_pending_responses()
    monkeypatch.setattr(file_io, "_get_response", lambda _: nullcontext(BytesIO(data)))
    assert file_io.read_file("https://example.test/document.txt") == content


def test_encoding_failure_and_custom_model_serializers_preserve_destination(tmp_path: Path) -> None:
    text = FileHandler(str(tmp_path / "text"), str)
    text.write("keep")
    with pytest.raises(UnicodeEncodeError):
        text.write("\ud800")
    assert text.read() == "keep"
    custom = FileHandler(
        str(tmp_path / "model"),
        Record,
        decoder=lambda raw: Record.model_validate_json(raw),
        encoder=lambda value: value.model_dump_json(),
    )
    content = Record(value="custom")
    custom.write(content)
    assert custom.read() == content
    wrong_type = FileHandler(str(tmp_path / "wrong"), dict)
    file_io.write_file(wrong_type.path, "[]")
    assert wrong_type.exists()
    with pytest.raises(TypeError, match="Expected dict"):
        wrong_type.read()
    version = wrong_type.current_version()
    with wrong_type.open_read() as snapshot:
        assert snapshot.version == version
        with pytest.raises(TypeError):
            snapshot.read()


def test_missing_and_untracked_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    handler = FileHandler(str(tmp_path / "document"), str)
    with pytest.raises(FileNotFoundError):
        handler.metadata(include_deleted=True)
    Path(handler.path).write_text("external", encoding="utf-8")
    metadata = handler.metadata(include_deleted=True)
    assert not metadata.is_deleted and metadata.latest_readable_version is None
    gzip_handler = FileHandler(handler.path, str, compression="gzip", fallback_uncompressed=True)
    assert gzip_handler.metadata().version.startswith("plain:")
    assert gzip_handler.metadata().size_bytes == len(b"external")


@pytest.mark.parametrize("data_type", [bytes, str, dict])
def test_https_read_exists_and_metadata_limits(
    monkeypatch: pytest.MonkeyPatch,
    data_type: type[Any],
) -> None:
    payload = b'{"value":"https"}'
    monkeypatch.setattr(file_io, "_get_response", lambda _: nullcontext(BytesIO(payload)))
    handler = FileHandler[Any]("https://example.test/document", data_type)
    assert handler.exists()
    assert handler.read() in (payload, payload.decode(), {"value": "https"})
    assert handler.version is None
    for operation in ["current_version", "metadata", "read_with_version"]:
        with pytest.raises(VersioningNotSupportedError):
            getattr(handler, operation)()

    def missing(_: str) -> Any:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(file_io, "_get_response", missing)
    assert not handler.exists()


@pytest.mark.parametrize("operation", ["exists", "current_version", "metadata", "read"])
def test_s3_permission_errors_never_fall_back(operation: str) -> None:
    handler = FileHandler("s3://bucket/record.json", dict, compression="gzip", fallback_uncompressed=True)
    method = "get_object" if operation == "read" else "head_object"
    with Stubber(file_io.s3_client) as stub:
        stub.add_client_error(method, "AccessDenied", expected_params={"Bucket": "bucket", "Key": "record.json.gz"})
        with pytest.raises(ClientError, match="AccessDenied"):
            getattr(handler, operation)()
        stub.assert_no_pending_responses()


def test_s3_single_file_metadata_prefers_gzip_and_falls_back_on_absence() -> None:
    handler = FileHandler("s3://bucket/record.json", dict, compression="gzip", fallback_uncompressed=True)
    modified = datetime(2026, 11, 9, tzinfo=UTC)
    response = {"ETag": '"etag"', "LastModified": modified, "ContentLength": 12}
    with Stubber(file_io.s3_client) as stub:
        stub.add_response("head_object", response, {"Bucket": "bucket", "Key": "record.json.gz"})
        metadata = handler.metadata()
        assert metadata.name == "record.json" and metadata.version == "gzip:etag"
        stub.add_client_error("head_object", "404", expected_params={"Bucket": "bucket", "Key": "record.json.gz"})
        stub.add_response("head_object", response, {"Bucket": "bucket", "Key": "record.json"})
        metadata = handler.metadata()
        assert metadata.version == "plain:etag" and metadata.last_modified == modified
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("compression", [None, "gzip"])
def test_indexed_undelete_synchronizes_the_restored_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compression: Literal["gzip"] | None,
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    definition = json_index_factory(Record, ["value"], table_name="handler_undelete")
    storage_type = json_file_storage_factory(
        "json",
        Record,
        index=definition,
        compression=compression,
        fallback_uncompressed=True,
    )
    store = storage_type(str(tmp_path))
    content = store.write(Record(value="restored"))
    store.delete("a")
    assert store.query().keys() == []
    assert store.undelete("a") == content
    assert store.query().keys() == ["a"]
    store.delete("a")

    def fail(_: str) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(store, "_synchronize_key", fail)
    with pytest.raises(IndexUpdateError, match="JSON was restored"):
        store.undelete("a")
    assert store.read("a") == content


def test_failed_conditional_publication_preserves_content_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    handler = FileHandler(str(tmp_path / "document"), bytes)
    version = handler.write(b"original")
    before = set(tmp_path.iterdir())
    real_replace = file_io.os.replace

    def fail_publication(source: str, destination: str) -> None:
        if destination == handler.primary_path:
            raise OSError("cannot publish")
        real_replace(source, destination)

    monkeypatch.setattr(file_io.os, "replace", fail_publication)
    with pytest.raises(OSError, match="cannot publish"):
        handler.write(b"updated", expected_version=version)
    assert handler.read() == b"original"
    assert handler.current_version() == version
    assert len(handler.history()) == 1
    assert set(tmp_path.iterdir()) == before


def test_physical_version_metadata_and_non_directory_history_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    with pytest.raises(VersionNotFoundError):
        BinaryFileHandler.version_metadata(str(tmp_path / "missing"))
    history_root = tmp_path / ".mx8fs-versions"
    history_root.mkdir()
    (history_root / "unrelated-file").write_text("not a history directory")
    assert FileHandler(str(tmp_path), dict).list_with_metadata("json", include_deleted=True) == []


@pytest.mark.parametrize("compression", [None, "gzip"])
def test_namespace_prefix_filters_live_and_deleted_representations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compression: Literal["gzip"] | None
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    root = FileHandler(str(tmp_path), dict, compression=compression, fallback_uncompressed=True)
    FileHandler(str(tmp_path / "selected-plain.json"), dict).write({"plain": True})
    root.joinpath("selected-current.json").write({"current": True})
    root.joinpath("other.json").write({"other": True})
    expected = {"selected-plain", "selected-current"}
    assert set(root.list("json", prefix="selected-")) == expected
    assert {item.name for item in root.list_with_metadata("json", prefix="selected-")} == expected
    root.joinpath("selected-current.json").delete()
    assert root.list("json", prefix="selected-") == ["selected-plain"]
    assert set(root.list("json", prefix="selected-", include_deleted=True)) == expected
    assert {item.name for item in root.list_with_metadata("json", "selected-", include_deleted=True)} == expected


def test_metadata_listing_tolerates_a_file_deleted_after_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "live.json"
    live.write_text("{}")
    monkeypatch.setattr(file_io, "glob", lambda _: [str(tmp_path / "deleted.json"), str(live)])
    assert [item.name for item in FileHandler(str(tmp_path), dict).list_with_metadata("json")] == ["live"]


@pytest.mark.parametrize("expected_version", [None, "etag"])
def test_physical_s3_mutation_errors_propagate(expected_version: str | None) -> None:
    params = {"Bucket": "bucket", "Key": "record", "Body": b"updated"}
    if expected_version is not None:
        params["IfMatch"] = expected_version
    with Stubber(file_io.s3_client) as stub:
        stub.add_client_error(
            "put_object",
            "AccessDenied",
            expected_params=params,
        )
        with pytest.raises(ClientError, match="AccessDenied"):
            FileHandler("s3://bucket/record", bytes).write(b"updated", expected_version=expected_version)
        stub.add_client_error(
            "head_object",
            "404",
            response_meta={"HTTPHeaders": {"x-amz-delete-marker": "true"}},
            expected_params={"Bucket": "bucket", "Key": "record"},
        )
        with pytest.raises(VersionNotFoundError, match="no version ID"):
            BinaryFileHandler.undelete("s3://bucket/record")
        stub.assert_no_pending_responses()


def test_legacy_existence_suppresses_permission_errors() -> None:
    with Stubber(file_io.s3_client) as stub:
        stub.add_client_error("head_object", "AccessDenied", expected_params={"Bucket": "bucket", "Key": "record"})
        assert not file_io.file_exists("s3://bucket/record")
        stub.assert_no_pending_responses()
