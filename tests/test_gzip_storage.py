"""Versioned handler and optional gzip JSON storage integration tests."""

import gzip
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import BaseModel, ValidationError

from mx8fs import (
    BinaryFileHandler,
    FileLock,
    FileNotDeletedError,
    FileVersionDeletedError,
    GzipFileHandler,
    VersioningNotSupportedError,
    VersionMismatchError,
    VersionNotFoundError,
    file_io,
    json_file_storage_factory,
    json_index_factory,
)


class Record(BaseModel):
    key: str | None = None
    value: str


PlainStorage = json_file_storage_factory("record.json", Record)
GzipStorage = json_file_storage_factory(
    "record.json", Record, compression="gzip", compression_level=1, fallback_uncompressed=True
)


@pytest.mark.parametrize("handler_type", [BinaryFileHandler, GzipFileHandler])
def test_handler_version_history_cas_and_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler_type: Any
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    path = str(tmp_path / "file")
    writer = handler_type(path, "wb", versioned=True)
    with writer as output:
        output.write(b"\x00\xff")
        assert not Path(path).exists()
    assert writer.version is not None
    first = file_io.list_file_versions(path)[0].version_id
    reader = handler_type(path, versioned=True)
    with reader as source:
        assert source.read() == b"\x00\xff"
    assert reader.version == writer.version
    with FileLock(path):
        conditional = handler_type(path, "wb", expected_version=reader.version)
        with conditional as output:
            output.write(b"\xfe\x00")
    assert conditional.version != reader.version
    historical = handler_type(path, version_id=first)
    with historical as source:
        assert source.read() == b"\x00\xff"
    assert historical.version == reader.version
    with pytest.raises(VersionMismatchError):
        with handler_type(path, "wb", expected_version=reader.version) as output:
            output.write(b"stale")
    with pytest.raises(RuntimeError, match="abort"):
        with handler_type(path, "wb", versioned=True) as output:
            output.write(b"partial")
            raise RuntimeError("abort")
    with handler_type(path, versioned=True) as source:
        assert source.read() == b"\xfe\x00"
    assert len(file_io.list_file_versions(path)) == 2


def test_gzip_text_mode_compression_level_and_close_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(tmp_path / "file.gz")
    writer = GzipFileHandler(path, "wt", encoding="utf-8", compression_level=1, versioned=True)
    with writer as output:
        output.write("café" * 1000)
    assert gzip.decompress(Path(path).read_bytes()).decode() == "café" * 1000
    reader = GzipFileHandler(path, "rt", encoding="utf-8", versioned=True)
    with reader as source:
        assert source.read() == "café" * 1000
    assert reader.version == writer.version
    current = Path(path).read_bytes()
    real_close = gzip.GzipFile.close

    def fail_close(stream: gzip.GzipFile) -> None:
        was_closed = stream.closed
        real_close(stream)
        if not was_closed:
            raise OSError("gzip finalization failed")

    with monkeypatch.context() as patch:
        patch.setattr(gzip.GzipFile, "close", fail_close)
        with pytest.raises(OSError, match="finalization"):
            with GzipFileHandler(path, "wb", versioned=True) as output:
                output.write(b"partial")
    assert Path(path).read_bytes() == current


@pytest.mark.parametrize("options", [{"version_id": "old", "mode": "wb"}, {"expected_version": "old", "mode": "rb"}])
def test_handler_rejects_incompatible_version_options(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BinaryFileHandler(str(tmp_path / "file"), **options)


@pytest.mark.parametrize("versioned", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_gzip_writer_opens_the_destination_only_on_context_entry(
    tmp_path: Path, versioned: bool, existing: bool
) -> None:
    path = tmp_path / "document.gz"
    original = gzip.compress(b"original")
    if existing:
        path.write_bytes(original)
    writer = GzipFileHandler(str(path), "wb", versioned=versioned)
    assert writer.version is None
    assert path.read_bytes() == original if existing else not path.exists()
    with writer as output:
        output.write(b"updated")
    assert gzip.decompress(path.read_bytes()) == b"updated"


def test_gzip_rejects_invalid_level(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="compression_level"):
        GzipFileHandler(str(tmp_path / "file.gz"), compression_level=10)


def test_gzip_storage_migration_model_semantics_and_cas(tmp_path: Path) -> None:
    plain, store = PlainStorage(str(tmp_path)), GzipStorage(str(tmp_path))
    original = plain.write_dict({"value": "café", "ignored": True}, "a")
    assert store.read("a") == original
    _, version = store.read_with_version("a")
    assert version.startswith("plain:")
    store.update_if_version(original.model_copy(update={"value": "derived"}), version)
    assert plain.read("a").value == "derived"
    assert not (tmp_path / "a.record.json.gz").exists()
    with pytest.raises(VersionMismatchError):
        store.update_if_version(original, version)
    store.write(Record(value="gzip"), "a")
    assert not (tmp_path / "a.record.json").exists()
    assert Record.model_validate_json(gzip.decompress((tmp_path / "a.record.json.gz").read_bytes())).value == "gzip"
    plain.write(Record(value="stale"), "a")
    assert store.list() == ["a"]
    assert store.read("a").value == "gzip"
    assert len(store.list_with_metadata()) == 1
    assert store.list_with_metadata()[0].version == store.read_with_version("a")[1]
    with pytest.raises(VersionMismatchError, match="migrated"):
        store.update_if_version(original, version)
    updated = store.mutate("a", lambda model: model.model_copy(update={"value": model.value + "!"}))
    assert updated.value == "gzip!"
    assert store.read_many(["a", "a"]) == [updated, updated]
    with pytest.raises(ValidationError):
        store.write_dict({"key": "bad"})
    _, version = store.read_with_version("a")
    store.delete("a")
    assert store.list() == []
    with pytest.raises(FileNotFoundError):
        store.read("a")
    with pytest.raises(VersionMismatchError):
        store.update_if_version(updated, version)


@pytest.mark.parametrize("payload", [b"corrupt", gzip.compress(b"bad JSON"), gzip.compress(b'{"key":"a"}')])
def test_corrupt_gzip_never_falls_back(tmp_path: Path, payload: bytes) -> None:
    PlainStorage(str(tmp_path)).write(Record(value="valid"), "a")
    (tmp_path / "a.record.json.gz").write_bytes(payload)
    store = GzipStorage(str(tmp_path))
    for reader in [store.read, store.read_with_version]:
        with pytest.raises((gzip.BadGzipFile, ValidationError)):
            reader("a")


def test_publish_failure_retains_plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    PlainStorage(str(tmp_path)).write(Record(value="old"), "a")

    def fail(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(BinaryFileHandler, "publish", staticmethod(fail))
    with pytest.raises(OSError, match="disk full"):
        GzipStorage(str(tmp_path)).write(Record(value="new"), "a")
    assert GzipStorage(str(tmp_path)).read("a").value == "old"


def test_migration_racing_with_plain_mutation_recomputes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    PlainStorage(str(tmp_path)).write(Record(value="old"), "a")
    store = GzipStorage(str(tmp_path))
    real_publish = BinaryFileHandler.publish
    calls = 0

    def migrate_after_update(
        path: str,
        data: str | bytes,
        *,
        expected_version: str | None = None,
        content_type: str | None = None,
    ) -> str:
        nonlocal calls
        if expected_version is None:
            return real_publish(path, data, content_type=content_type)
        calls += 1
        written = real_publish(path, data, expected_version=expected_version, content_type=content_type)
        if calls == 1:
            store.write(Record(value="concurrent"), "a")
        return written

    monkeypatch.setattr(BinaryFileHandler, "publish", staticmethod(migrate_after_update))
    assert store.mutate("a", lambda model: model.model_copy(update={"value": model.value + "!"})).value == "concurrent!"
    assert calls == 2


def test_key_allocation_preserves_randomizer_and_checks_both_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    keys = iter(["PLAIN", "GZIP", "NEW"])
    monkeypatch.setattr("mx8fs.storage.random.choices", lambda *args, **kwargs: next(keys))
    monkeypatch.setattr(GzipStorage, "_randomizer", staticmethod(lambda: calls.append(True)))
    store = GzipStorage(str(tmp_path))
    PlainStorage(str(tmp_path)).write(Record(value="occupied"), "PLAIN")
    store.write(Record(value="occupied"), "GZIP")
    assert store.write(Record(value="new")).key == "NEW"
    assert calls == [True, True, True]


@pytest.mark.parametrize("options", [{"compression": "zip"}, {"compression_level": -1}, {"compression_level": 10}])
def test_factory_validates_compression(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        json_file_storage_factory("json", Record, **options)


def test_strict_gzip_and_invalid_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    PlainStorage(str(tmp_path)).write(Record(value="old"), "a")
    strict = json_file_storage_factory("record.json", Record, compression="gzip")(str(tmp_path))
    assert strict.list() == []
    for reader in [strict.read, strict.read_with_version, strict._current_version]:
        with pytest.raises(FileNotFoundError):
            reader("a")
    strict.write(Record(value="new"), "a")
    assert (tmp_path / "a.record.json").exists()
    for token in ["plain:old", "unknown:old", "old"]:
        with pytest.raises(ValueError):
            strict.update_if_version(Record(key="a", value="new"), token)
        with pytest.raises(ValueError if ":" in token else VersionNotFoundError):
            strict.read_version("a", token)
    assert strict.list_with_metadata()[0].version.startswith("gzip:")
    with pytest.raises(FileNotFoundError):
        PlainStorage(str(tmp_path)).update_if_version(Record(key="missing", value="missing"), "old")


def test_history_metadata_restore_and_undelete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    plain, store = PlainStorage(str(tmp_path)), GzipStorage(str(tmp_path))
    plain.write(Record(value="old"), "a")
    old = plain.history("a")[0].version_id
    assert store._current_version("a").startswith("plain:")
    assert store.list_with_metadata(include_deleted=True)[0].version.startswith("plain:")
    store.write(Record(value="new"), "a")
    assert store.read_version("a", old).value == "old"
    history = store.history("a")
    assert sum(v.is_latest for v in history) == 1
    assert next(v for v in history if v.is_latest).version_id.startswith("gzip:")
    for version in history:
        if version.is_deleted:
            with pytest.raises(FileVersionDeletedError):
                store.read_version("a", version.version_id)
        else:
            assert store.read_version("a", version.version_id).value in {"new", "old"}
    gzip_version = next(v for v in history if v.version_id.startswith("gzip:") and not v.is_deleted)
    assert store.read_version("a", gzip_version.version_id.removeprefix("gzip:")).value == "new"
    assert store.restore_version("a", f"plain:{old}").value == "old"
    with pytest.raises(FileNotDeletedError):
        store.undelete("a")
    store.delete("a")
    assert store.list(include_deleted=True) == ["a"]
    item = store.list_with_metadata(include_deleted=True)[0]
    assert item.is_deleted and item.latest_readable_version is not None
    assert store.read_version("a", item.latest_readable_version.version_id).value == "old"
    assert store.undelete("a").value == "old"
    with pytest.raises(FileNotFoundError, match="no recoverable"):
        store.undelete("missing")
    with pytest.raises(VersionNotFoundError):
        store.read_version("a", "missing")
    store.delete("a")
    plain.write(Record(value="recreated"), "a")
    item = store.list_with_metadata(include_deleted=True)[0]
    assert not item.is_deleted and item.version.startswith("plain:")
    assert next(v for v in store.history("a") if v.is_latest).version_id.startswith("plain:")


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "AccessDenied"])
def test_versioned_s3_read_preserves_missing_vs_permission_errors(code: str) -> None:
    with Stubber(file_io.s3_client) as stub:
        stub.add_client_error("get_object", code, expected_params={"Bucket": "bucket", "Key": "file"})
        with pytest.raises(ClientError if code == "AccessDenied" else FileNotFoundError):
            with BinaryFileHandler("s3://bucket/file", versioned=True):
                pass


def test_s3_handler_etag_historical_bytes_and_content_type() -> None:
    with Stubber(file_io.s3_client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"first"'},
            {"Bucket": "bucket", "Key": "file", "Body": b"\xff", "ContentType": "application/octet-stream"},
        )
        writer = BinaryFileHandler("s3://bucket/file", "wb", "application/octet-stream", versioned=True)
        with writer as output:
            output.write(b"\xff")
        assert writer.version == "first"
        stub.add_response(
            "get_object", {"Body": BytesIO(b"\xff"), "ETag": '"first"'}, {"Bucket": "bucket", "Key": "file"}
        )
        reader = BinaryFileHandler("s3://bucket/file", versioned=True)
        with reader as source:
            assert source.read() == b"\xff"
        assert reader.version == writer.version
        stub.add_response(
            "put_object",
            {"ETag": '"second"'},
            {
                "Bucket": "bucket",
                "Key": "file",
                "Body": b"new",
                "IfMatch": "first",
                "ContentType": "application/octet-stream",
            },
        )
        conditional = BinaryFileHandler("s3://bucket/file", "wb", "application/octet-stream", expected_version="first")
        with conditional as output:
            output.write(b"new")
        assert conditional.version == "second"
        stub.add_response(
            "get_object",
            {"Body": BytesIO(b"\x00"), "ETag": '"old"'},
            {"Bucket": "bucket", "Key": "file", "VersionId": "old-id"},
        )
        historical = BinaryFileHandler("s3://bucket/file", version_id="old-id")
        with historical as source:
            assert source.read() == b"\x00"
        assert historical.version == "old"
        stub.assert_no_pending_responses()


def test_gzip_https_current_reads_without_concurrency_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = gzip.compress(Record(key="a", value="https").model_dump_json().encode())
    monkeypatch.setattr(file_io, "_get_response", lambda _: nullcontext(BytesIO(payload)))
    store = GzipStorage("https://example.test")
    assert store.read("a").value == "https"
    with pytest.raises(VersioningNotSupportedError):
        store.read_with_version("a")
    monkeypatch.setattr(file_io, "_get_response", lambda _: nullcontext(BytesIO(b"text")))
    with pytest.raises(VersioningNotSupportedError):
        file_io.read_file_with_version("https://example.test/file")


def test_indexed_storage_uses_handler_based_storage_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    PlainStorage(str(tmp_path)).write(Record(value="plain"), "a")
    definition = json_index_factory(Record, ["value"], table_name="gzip_records")
    storage_type = json_file_storage_factory(
        "record.json", Record, index=definition, compression="gzip", fallback_uncompressed=True
    )
    store = storage_type(str(tmp_path))
    assert store.query().models() == [Record(key="a", value="plain")]
    _, version = store.read_with_version("a")
    store.update_if_version(Record(key="a", value="derived"), version)
    store.write(Record(value="gzip"), "a")
    store.mutate("a", lambda model: model.model_copy(update={"value": "mutated"}))
    assert store.query().where(definition.value == "mutated").keys() == ["a"]
    assert store.rebuild_index().upserted == 1
    store.delete("a")
    assert store.query().keys() == []
    assert store.undelete("a").value == "mutated"
    assert store.query().keys() == ["a"]


def test_live_s3_gzip_storage_migration_and_cas() -> None:
    root = f"s3://mx8-test-bucket/mx8fs/gzip-storage/{uuid4().hex}"
    plain, store = PlainStorage(root), GzipStorage(root)
    try:
        plain.write(Record(value="plain"), "a")
        original, version = store.read_with_version("a")
        assert version.startswith("plain:")
        store.update_if_version(original.model_copy(update={"value": "derived"}), version)
        assert plain.read("a").value == "derived"
        store.write(Record(value="gzip"), "a")
        assert store.list() == ["a"]
        current, version = store.read_with_version("a")
        assert version.startswith("gzip:")
        assert store.mutate("a", lambda model: model.model_copy(update={"value": model.value + "!"})).value == "gzip!"
        with pytest.raises(VersionMismatchError):
            store.update_if_version(current, version)
        with pytest.raises(FileNotFoundError):
            plain.read("a")
        plain.write(Record(value="stale"), "a")
        assert store.list() == ["a"]
        assert len(store.list_with_metadata()) == 1
        assert store.list_with_metadata()[0].version == store.read_with_version("a")[1]
    finally:
        store.delete("a")


def test_text_helpers_preserve_local_newline_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    path = str(tmp_path / "text")
    with BinaryFileHandler(path, "wb", versioned=True) as output:
        output.write(b"one\r\ntwo\rthree\n")
    assert file_io.read_file(path) == "one\ntwo\nthree\n"
    assert file_io.read_file_with_version(path)[0] == "one\ntwo\nthree\n"
    assert file_io.read_file_version(path, file_io.list_file_versions(path)[0]) == "one\ntwo\nthree\n"


@pytest.mark.parametrize("handler_type", [BinaryFileHandler, GzipFileHandler])
def test_listed_versions_reuse_metadata_through_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler_type: Any
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    path = str(tmp_path / "versioned")
    with handler_type(path, "wb", versioned=True) as output:
        output.write(b"historical")
    metadata = file_io.list_file_versions(path)[0]
    monkeypatch.setattr(file_io, "_load_local_version", lambda *args: pytest.fail("known metadata was fetched again"))
    reader = handler_type(path, version_id=metadata)
    with reader as source:
        assert source.read() == b"historical"
    assert reader.version == metadata.revision
    if handler_type is BinaryFileHandler:
        assert file_io.read_file_version(path, metadata) == "historical"
