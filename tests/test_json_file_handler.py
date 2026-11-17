"""JSON handler integration with compression, custom model codecs and versioning."""

import gzip
import json
from datetime import UTC, datetime
from io import UnsupportedOperation
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict

from mx8fs import (
    FileVersionMetadata,
    JsonFileHandler,
    VersionMismatchError,
    file_io,
    json_file_storage_factory,
    json_index_factory,
)


@pytest.mark.parametrize("compression", [None, "gzip"])
def test_json_round_trip_history_and_conditional_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compression: Literal["gzip"] | None
) -> None:
    monkeypatch.setenv("MX8FS_LOCAL_VERSIONING", "true")
    path = str(tmp_path / "document")
    original: dict[str, Any] = {"name": "café ☕", "values": [1, True, None, {"nested": "value"}]}
    writer = JsonFileHandler[dict[str, Any]](path, "w", compression=compression)
    with writer as output:
        output.write(original)
        assert not Path(path).exists()
    assert writer.version is not None
    metadata = file_io.list_file_versions(path)[0]
    raw = Path(path).read_bytes()
    assert json.loads(gzip.decompress(raw) if compression else raw) == original

    reader = JsonFileHandler[dict[str, Any]](path, compression=compression)
    with reader as source:
        assert source.read() == original
    assert reader.version == writer.version

    updated: dict[str, Any] = {"updated": True}
    conditional = JsonFileHandler[dict[str, Any]](path, "w", compression=compression, expected_version=reader.version)
    with conditional as output:
        output.write(updated)
    assert conditional.version != reader.version
    with pytest.raises(VersionMismatchError):
        with JsonFileHandler[dict[str, Any]](
            path, "w", compression=compression, expected_version=reader.version
        ) as output:
            output.write(original)

    versions: list[str | FileVersionMetadata] = [metadata.version_id, metadata]
    for version in versions:
        historical = JsonFileHandler[dict[str, Any]](path, compression=compression, version_id=version)
        with historical as source:
            assert source.read() == original
        assert historical.version == reader.version

    with pytest.raises(TypeError):
        with JsonFileHandler[dict[str, Any]](path, "w", compression=compression) as output:
            output.write({"unsupported": object()})
    with JsonFileHandler[dict[str, Any]](path, compression=compression) as source:
        assert source.read() == updated
    assert len(file_io.list_file_versions(path)) == 2


class StrictRecord(BaseModel):
    model_config = ConfigDict(strict=True)
    key: str
    timestamp: datetime


@pytest.mark.parametrize("compression", [None, "gzip"])
def test_custom_model_codecs_preserve_strict_json_validation(
    tmp_path: Path, compression: Literal["gzip"] | None
) -> None:
    content = StrictRecord(key="a", timestamp=datetime(2026, 11, 9, tzinfo=UTC))
    path = str(tmp_path / "document")
    writer = JsonFileHandler[StrictRecord](
        path,
        "w",
        compression=compression,
        decoder=StrictRecord.model_validate_json,
        encoder=lambda value: value.model_dump_json(),
    )
    with writer as output:
        output.write(content)
    reader = JsonFileHandler[StrictRecord](
        path,
        compression=compression,
        decoder=StrictRecord.model_validate_json,
    )
    with reader as source:
        assert source.read() == content
    storage = json_file_storage_factory("json", StrictRecord, compression=compression)(str(tmp_path / "storage"))
    storage.write(content)
    assert storage.read("a") == content
    assert storage.read_with_version("a")[0] == content


@pytest.mark.parametrize(
    "options,error",
    [
        ({"mode": "rb"}, NotImplementedError),
        ({"compression": "zip"}, ValueError),
        ({"compression_level": -1}, ValueError),
        ({"compression_level": 10}, ValueError),
    ],
)
def test_invalid_json_handler_options(tmp_path: Path, options: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        JsonFileHandler[Any](str(tmp_path / "document"), **options)


def test_json_read_write_modes_and_parse_errors(tmp_path: Path) -> None:
    path = str(tmp_path / "document")
    with JsonFileHandler[dict[str, Any]](path, "w") as output:
        output.write({"keep": True})
    before = Path(path).read_bytes()
    with pytest.raises(UnsupportedOperation, match="not readable"):
        with JsonFileHandler[Any](path, "w") as output:
            output.read()
    assert Path(path).read_bytes() == before
    with pytest.raises(UnsupportedOperation, match="not writable"):
        with JsonFileHandler[Any](path) as source:
            source.write({})
    assert Path(path).read_bytes() == before
    version = file_io.write_file(path, "invalid JSON")
    reader = JsonFileHandler[Any](path)
    with pytest.raises(json.JSONDecodeError):
        with reader as source:
            source.read()
    assert reader.version == version


def test_model_decoder_failure_does_not_trigger_plain_fallback(tmp_path: Path) -> None:
    plain_type = json_file_storage_factory("json", StrictRecord)
    gzip_type = json_file_storage_factory("json", StrictRecord, compression="gzip", fallback_uncompressed=True)
    calls: list[str] = []

    class CustomStorage(gzip_type):  # type: ignore[misc, valid-type]
        @staticmethod
        def _json_to_model(contents: str) -> StrictRecord:
            calls.append(contents)
            raise FileNotFoundError("model decoder failed")

    content = StrictRecord(key="a", timestamp=datetime(2026, 11, 9, tzinfo=UTC))
    gzip_type(str(tmp_path)).write(content)
    plain_type(str(tmp_path)).write(content)
    with pytest.raises(FileNotFoundError, match="model decoder failed"):
        CustomStorage(str(tmp_path)).read("a")
    assert calls == [content.model_dump_json()]


def test_index_decoder_error_is_not_treated_as_an_absent_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = StrictRecord(key="a", timestamp=datetime(2026, 11, 9, tzinfo=UTC))
    definition = json_index_factory(StrictRecord, ["timestamp"], table_name="json_handler_records")
    storage_type = json_file_storage_factory("json", StrictRecord, index=definition)
    store = storage_type(str(tmp_path))
    store.write(content)

    def fail_decoder(text: str) -> StrictRecord:
        raise FileNotFoundError("model decoder failed")

    monkeypatch.setattr(store, "_json_to_model", fail_decoder)
    with pytest.raises(FileNotFoundError, match="model decoder failed"):
        store._read_rebuild_key("a")
