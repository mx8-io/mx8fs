"""Typed logical files, including compression, history and collection metadata."""

from __future__ import annotations

import builtins
import json
import os
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from .file_io import (
    BinaryFileHandler,
    FileMetadata,
    FileNotDeletedError,
    FileVersionMetadata,
    GzipFileHandler,
    JsonFileHandler,
    TextFileHandler,
    VersioningNotSupportedError,
    VersionMismatchError,
    VersionNotFoundError,
    _validate_compression,
)

if TYPE_CHECKING:  # pragma: no cover
    from .lock import FileLock


@dataclass(frozen=True)
class FileReader[ContentT]:
    """A deferred typed read and the revision of the exact bytes opened."""

    read: Callable[[], ContentT]
    version: str | None


class FileHandler[ContentT]:
    """
    Operate on typed logical paths, optionally backed by gzip and plain files.

    Paths omit the compression suffix. Compression appends .gz to the physical file;
    directory operations and joinpath use the logical path unchanged.
    """

    def __init__(
        self,
        path: str,
        data_type: type[ContentT],
        *,
        compression: Literal["gzip"] | None = None,
        compression_level: int = 1,
        fallback_uncompressed: bool = False,
        encoding: str = "utf-8",
        newline: str | None = "auto",
        decoder: Callable[[str], ContentT] | None = None,
        encoder: Callable[[ContentT], str] | None = None,
    ) -> None:
        """Configure a logical file without opening it or probing its backend."""
        self.validate_options(compression, compression_level)
        self.path = path
        self.data_type = data_type
        self.compression = compression
        self.compression_level = compression_level
        self.fallback_uncompressed = compression == "gzip" and fallback_uncompressed
        self.encoding = encoding
        self.newline = newline
        self.decoder = decoder
        self.encoder = encoder
        self.version: str | None = None

    @staticmethod
    def validate_options(compression: str | None, compression_level: int) -> None:
        """Validate configuration without opening or probing any file."""
        _validate_compression(compression, compression_level)

    @property
    def _representations(self) -> tuple[bool, ...]:
        return (True, False) if self.fallback_uncompressed else (self.compression is not None,)

    @property
    def primary_path(self) -> str:
        """Return the configured physical destination, including its compression suffix."""
        return self._physical_path(self.compression is not None)

    def _physical_path(self, compressed: bool) -> str:
        return self.path + (".gz" if compressed else "")

    def joinpath(self, name: str) -> FileHandler[ContentT]:
        """Create a child logical path with the same type and I/O configuration."""
        return FileHandler(
            os.path.join(self.path, name),
            self.data_type,
            compression=self.compression,
            compression_level=self.compression_level,
            fallback_uncompressed=self.fallback_uncompressed,
            encoding=self.encoding,
            newline=self.newline,
            decoder=self.decoder,
            encoder=self.encoder,
        )

    def _decode_json(self, text: str) -> ContentT:
        content = json.loads(text)
        if not isinstance(content, self.data_type):
            raise TypeError(f"Expected {self.data_type.__name__}, got {type(content).__name__}")
        return cast(ContentT, content)

    def _physical_handler(
        self,
        mode: str,
        compressed: bool,
        *,
        version_id: str | FileVersionMetadata | None = None,
        expected_version: str | None = None,
    ) -> BinaryFileHandler | GzipFileHandler | TextFileHandler | JsonFileHandler[ContentT]:
        path = self._physical_path(compressed)
        if self.data_type is bytes:
            if compressed:
                return GzipFileHandler(
                    path,
                    mode + "b",
                    compression_level=self.compression_level,
                    versioned=True,
                    version_id=version_id,
                    expected_version=expected_version,
                )
            return BinaryFileHandler(
                path, mode + "b", versioned=True, version_id=version_id, expected_version=expected_version
            )
        if self.data_type is str:
            return TextFileHandler(
                path,
                mode,
                compression="gzip" if compressed else None,
                compression_level=self.compression_level,
                encoding=self.encoding,
                newline=self.newline,
                version_id=version_id,
                expected_version=expected_version,
            )
        return JsonFileHandler(
            path,
            mode,
            compression="gzip" if compressed else None,
            compression_level=self.compression_level,
            version_id=version_id,
            expected_version=expected_version,
            decoder=cast(
                Callable[[str], ContentT],
                self.decoder or getattr(self.data_type, "model_validate_json", self._decode_json),
            ),
            encoder=cast(
                Callable[[ContentT], str], self.encoder or getattr(self.data_type, "model_dump_json", json.dumps)
            ),
        )

    def _version_token(self, version: str, compressed: bool) -> str:
        return f"{'gzip' if compressed else 'plain'}:{version}" if self.compression else version

    def _parse_version(self, version: str) -> tuple[bool, str]:
        if self.compression is None:
            return False, version
        representation, separator, identifier = version.partition(":")
        compressed = representation == "gzip"
        if not separator or (not compressed and (representation != "plain" or not self.fallback_uncompressed)):
            raise ValueError("Invalid storage version representation")
        return compressed, identifier

    def _read_target(
        self,
        reference: str | FileVersionMetadata | None,
    ) -> tuple[bool, str | FileVersionMetadata | None, bool]:
        if reference is None:
            return self.compression is not None, None, self.fallback_uncompressed
        identifier = reference.version_id if isinstance(reference, FileVersionMetadata) else reference
        if self.compression and ":" not in identifier:
            return True, reference, self.fallback_uncompressed
        compressed, identifier = self._parse_version(identifier)
        if isinstance(reference, FileVersionMetadata):
            prefix = "gzip:" if compressed else "plain:"
            revision = reference.revision.removeprefix(prefix) if reference.revision is not None else None
            return compressed, replace(reference, version_id=identifier, revision=revision), False
        return compressed, identifier, False

    @contextmanager
    def open_read(
        self,
        *,
        version_id: str | FileVersionMetadata | None = None,
    ) -> Generator[FileReader[ContentT], None, None]:
        """Open bytes and their revision before decoding, falling back only on absence."""
        compressed, reference, fallback = self._read_target(version_id)
        missing = FileNotFoundError if version_id is None else VersionNotFoundError
        with ExitStack() as contexts:
            try:
                handler = self._physical_handler("r", compressed, version_id=reference)
                source = contexts.enter_context(handler)
            except missing:
                if not fallback:
                    raise
                compressed = False
                handler = self._physical_handler("r", False, version_id=reference)
                source = contexts.enter_context(handler)
            self.version = self._version_token(handler.version, compressed) if handler.version is not None else None
            yield FileReader(read=cast(Callable[[], ContentT], source.read), version=self.version)

    def read(self, *, version_id: str | FileVersionMetadata | None = None) -> ContentT:
        """Read current or historical content using the configured type."""
        with self.open_read(version_id=version_id) as source:
            return source.read()

    def read_with_version(self, *, version_id: str | FileVersionMetadata | None = None) -> tuple[ContentT, str]:
        """Read content and its representation-aware concurrency revision."""
        with self.open_read(version_id=version_id) as source:
            content = source.read()
        if source.version is None:
            raise VersioningNotSupportedError("HTTPS paths do not expose concurrency versions")
        return content, source.version

    def _require_plain_current(self) -> None:
        if self.current_version().startswith("gzip:"):
            raise VersionMismatchError(f"File {self.path} has migrated to gzip")

    def write(self, content: ContentT, *, expected_version: str | None = None) -> str:
        """Publish content, keeping conditional updates on the representation read."""
        compressed, expected = self.compression is not None, None
        if expected_version is not None:
            compressed, expected = self._parse_version(expected_version)
        retain_plain = self.compression is not None and not compressed
        try:
            if retain_plain:
                self._require_plain_current()
            handler = self._physical_handler("w", compressed, expected_version=expected)
            with handler as destination:
                cast(Any, destination).write(content)
            if retain_plain:
                self._require_plain_current()
        except FileNotFoundError as error:
            if expected_version is None or self.compression is None or isinstance(error, VersionMismatchError):
                raise
            raise VersionMismatchError(f"File {self.path} changed before its conditional update") from error
        assert handler.version is not None
        self.version = self._version_token(handler.version, compressed)
        if expected_version is None and self.fallback_uncompressed:
            BinaryFileHandler.delete(self.path)
        return self.version

    def current_version(self) -> str:
        """Read the current revision without downloading content."""
        if self.path.startswith("https://"):
            raise VersioningNotSupportedError("HTTPS paths do not expose concurrency versions")
        try:
            version = BinaryFileHandler.current_version(self.primary_path)
            return self._version_token(version, self.compression is not None)
        except FileNotFoundError:
            if not self.fallback_uncompressed:
                raise
            return self._version_token(BinaryFileHandler.current_version(self.path), False)

    def exists(self) -> bool:
        """Check both configured names without treating permission failures as absence."""
        try:
            if self.path.startswith("https://"):
                with self.open_read():
                    return True
            self.current_version()
        except FileNotFoundError:
            return False
        return True

    def delete(self) -> None:
        """Delete configured current files, removing the fallback before gzip."""
        for compressed in reversed(self._representations):
            BinaryFileHandler.delete(self._physical_path(compressed))

    def _tag_history(self, version: FileVersionMetadata, compressed: bool) -> FileVersionMetadata:
        return replace(
            version,
            version_id=self._version_token(version.version_id, compressed),
            revision=self._version_token(version.revision, compressed) if version.revision is not None else None,
        )

    def history(self) -> builtins.list[FileVersionMetadata]:
        """Return logical history with source-aware IDs and one latest state."""
        versions = [
            self._tag_history(version, compressed)
            for compressed in self._representations
            for version in BinaryFileHandler.history(self._physical_path(compressed))
        ]
        versions.sort(key=lambda version: version.last_modified, reverse=True)
        current = next(
            (v for v in versions if v.is_latest and not v.is_deleted and v.version_id.startswith("gzip:")), None
        )
        current = current or next((v for v in versions if v.is_latest and not v.is_deleted), None)
        current = current or next((v for v in versions if v.is_latest), None)
        return [replace(version, is_latest=version is current) for version in versions]

    def _live_metadata(self) -> FileMetadata:
        try:
            metadata = BinaryFileHandler.metadata(self.primary_path)
            compressed = self.compression is not None
        except FileNotFoundError:
            if not self.fallback_uncompressed:
                raise
            metadata = BinaryFileHandler.metadata(self.path)
            compressed = False
        return replace(
            metadata, name=os.path.basename(self.path), version=self._version_token(metadata.version, compressed)
        )

    def metadata(self, *, include_deleted: bool = False) -> FileMetadata:
        """Read live or deleted metadata for this logical file."""
        if not include_deleted:
            return self._live_metadata()
        versions = self.history()
        readable = next((version for version in versions if not version.is_deleted), None)
        try:
            return replace(self._live_metadata(), latest_readable_version=readable)
        except FileNotFoundError:
            current = next((version for version in versions if version.is_latest), None)
            if current is None:
                raise
            return FileMetadata(
                name=os.path.basename(self.path),
                last_modified=current.last_modified,
                size_bytes=current.size_bytes,
                version=current.version_id,
                is_deleted=True,
                latest_readable_version=readable,
            )

    def list(self, extension: str, prefix: str = "", *, include_deleted: bool = False) -> builtins.list[str]:
        """List logical keys below this directory without downloading their content."""
        if include_deleted:
            return [item.name for item in self.list_with_metadata(extension, prefix, include_deleted=True)]
        return list(
            dict.fromkeys(
                key
                for compressed in self._representations
                for key in BinaryFileHandler.list_files(self.path, extension + (".gz" if compressed else ""), prefix)
            )
        )

    def list_with_metadata(
        self, extension: str, prefix: str = "", *, include_deleted: bool = False
    ) -> builtins.list[FileMetadata]:
        """List logical metadata using bulk backend listings and source-aware revisions."""
        files: dict[str, FileMetadata] = {}
        for compressed in self._representations:
            suffix = extension + (".gz" if compressed else "")
            for item in BinaryFileHandler.list_files_with_metadata(
                self.path, suffix, prefix, include_deleted=include_deleted
            ):
                item = replace(
                    item,
                    version=self._version_token(item.version, compressed),
                    latest_readable_version=(
                        self._tag_history(item.latest_readable_version, compressed)
                        if item.latest_readable_version is not None
                        else None
                    ),
                )
                previous = files.get(item.name)
                if previous is None:
                    files[item.name] = item
                    continue
                chosen = previous
                if previous.is_deleted and (not item.is_deleted or item.last_modified > previous.last_modified):
                    chosen = item
                readable = [
                    v for v in (previous.latest_readable_version, item.latest_readable_version) if v is not None
                ]
                files[item.name] = replace(
                    chosen,
                    latest_readable_version=max(readable, key=lambda v: v.last_modified) if readable else None,
                )
        return list(files.values())

    def restore_version(self, version_id: str | FileVersionMetadata) -> ContentT:
        """Publish a readable historical value as a new current version."""
        content = self.read(version_id=version_id)
        self.write(content)
        return content

    def undelete(self) -> ContentT:
        """Restore deleted content, preserving native undelete for uncompressed files."""
        if self.compression is None:
            BinaryFileHandler.undelete(self.path)
            return self.read()
        if self.exists():
            raise FileNotDeletedError(f"File {self.path} is not deleted")
        readable = next((version for version in self.history() if not version.is_deleted), None)
        if readable is None:
            raise FileNotFoundError(f"File {self.path} has no recoverable versions")
        return self.restore_version(readable)

    def get_lock(
        self,
        wait_period: float = 0.1,
        time_out_seconds: int = 840,
        maximum_age: int = 900,
    ) -> FileLock:
        """Create a caller-owned lock for this logical file's configured destination."""
        from .lock import FileLock

        return FileLock(
            self.primary_path,
            wait_period=wait_period,
            time_out_seconds=time_out_seconds,
            maximum_age=maximum_age,
        )
