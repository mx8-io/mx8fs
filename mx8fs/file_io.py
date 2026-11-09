"""
AWS file IO functions.

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

import gzip
import hashlib
import json
import os
import stat
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from glob import glob
from io import BytesIO, TextIOWrapper, UnsupportedOperation
from typing import IO, Any, Literal, Self, cast
from uuid import uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from urllib3 import HTTPResponse

boto_config = Config(
    max_pool_connections=int(os.getenv("BOTO_MAX_CONNECTIONS", 100)),
    connect_timeout=float(os.getenv("BOTO_CONNECT_TIMEOUT", 5.0)),
    read_timeout=float(os.getenv("BOTO_READ_TIMEOUT", 840.0)),  # 1 minute less than the lambda timeout
    retries={
        "total_max_attempts": int(os.getenv("BOTO_MAX_RETRIES", 10)),
        "mode": cast(Literal["legacy", "standard", "adaptive"], os.getenv("BOTO_RETRY_MODE", "adaptive")),
    },
)

s3_client = boto3.client(
    service_name="s3",
    config=boto_config,
)

S3_PREFIX = "s3://"
_HTTPS_PREFIX = "https://"
_S3_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024
_LOCAL_VERSIONING_ENV = "MX8FS_LOCAL_VERSIONING"
_LOCAL_HISTORY_DIRECTORY = ".mx8fs-versions"


class VersionMismatchError(FileNotFoundError):
    """Custom error for version mismatch when writing files."""


class VersioningNotEnabledError(RuntimeError):
    """Raised when local version history is requested while disabled."""


class VersioningNotSupportedError(RuntimeError):
    """Raised when version history is not supported for a path."""


class VersionNotFoundError(FileNotFoundError):
    """Raised when a requested historical version does not exist."""


class FileVersionDeletedError(FileNotFoundError):
    """Raised when attempting to read a delete marker."""


class FileNotDeletedError(ValueError):
    """Raised when attempting to undelete a live file."""


@dataclass(frozen=True, kw_only=True)
class FileVersionMetadata:
    """Portable metadata for one immutable file version or delete marker."""

    name: str
    version_id: str
    last_modified: datetime
    size_bytes: int
    is_latest: bool
    is_deleted: bool
    revision: str | None


@dataclass(frozen=True, kw_only=True)
class FileMetadata:
    """Portable metadata returned for a listed file."""

    name: str
    last_modified: datetime
    size_bytes: int
    version: str
    is_deleted: bool = False
    latest_readable_version: FileVersionMetadata | None = None


def _local_versioning_enabled() -> bool:
    """Return whether local sidecar versioning is enabled."""
    value = os.getenv(_LOCAL_VERSIONING_ENV, "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise ValueError(f"Invalid {_LOCAL_VERSIONING_ENV} value: {value!r}")


def _local_history_path(file: str) -> str | None:
    """Return the local history directory for a file when enabled."""
    if not _local_versioning_enabled():
        return None
    absolute = os.path.abspath(file)
    name_hash = hashlib.sha256(os.path.basename(absolute).encode("utf-8")).hexdigest()
    return os.path.join(os.path.dirname(absolute), _LOCAL_HISTORY_DIRECTORY, name_hash)


def _require_local_history_path(file: str) -> str:
    """Return the enabled local history directory or raise."""
    history_path = _local_history_path(file)
    if history_path is None:
        raise VersioningNotEnabledError(f"Set {_LOCAL_VERSIONING_ENV}=true to enable local version history")
    return history_path


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomically write bytes without invoking versioning hooks."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):  # pragma: no cover - replace normally consumes it
            os.remove(temporary_path)


def _local_version_from_json(data: dict[str, Any], *, is_latest: bool) -> FileVersionMetadata:
    """Convert persisted local version metadata to its public representation."""
    return FileVersionMetadata(
        name=str(data["name"]),
        version_id=str(data["version_id"]),
        last_modified=datetime.fromisoformat(str(data["last_modified"])),
        size_bytes=int(data["size_bytes"]),
        is_latest=is_latest,
        is_deleted=bool(data["is_deleted"]),
        revision=cast(str | None, data["revision"]),
    )


def _load_local_versions_from_path(history_path: str) -> list[FileVersionMetadata]:
    """Load newest-first versions from one local history directory."""
    metadata_paths = glob(os.path.join(history_path, "versions", "*.json"))
    raw_versions: list[dict[str, Any]] = []
    for metadata_path in metadata_paths:
        with open(metadata_path, encoding="UTF-8") as metadata_file:
            raw_versions.append(cast(dict[str, Any], json.load(metadata_file)))
    raw_versions.sort(key=lambda item: int(item["created_ns"]), reverse=True)
    return [_local_version_from_json(item, is_latest=index == 0) for index, item in enumerate(raw_versions)]


def _load_local_version(file: str, version_id: str) -> FileVersionMetadata:
    """Load one local version directly from its sidecar metadata."""
    history_path = _require_local_history_path(file)
    metadata_path = os.path.join(history_path, "versions", f"{version_id}.json")
    try:
        with open(metadata_path, encoding="UTF-8") as metadata_file:
            data = cast(dict[str, Any], json.load(metadata_file))
    except FileNotFoundError as exc:
        raise VersionNotFoundError(f"Version {version_id} of {file} not found") from exc
    return _local_version_from_json(data, is_latest=False)


def _append_local_version(file: str, *, deleted: bool, force: bool = False) -> None:
    """Append the current local state or a delete marker to its sidecar."""
    history_path = _require_local_history_path(file)
    versions = _load_local_versions_from_path(history_path)
    current_revision: str | None = None
    contents = b""
    if not deleted:
        with open(file, "rb") as current_file:
            contents = current_file.read()
        current_revision = str(os.stat(file).st_mtime_ns)
        if not force and versions and not versions[0].is_deleted and versions[0].revision == current_revision:
            return
    version_id = uuid4().hex
    timestamp = datetime.now(UTC).isoformat()
    versions_path = os.path.join(history_path, "versions")
    os.makedirs(versions_path, exist_ok=True)
    if not deleted:
        _atomic_write_bytes(os.path.join(versions_path, f"{version_id}.data"), contents)
    metadata = {
        "created_ns": time.time_ns(),
        "name": os.path.basename(file),
        "version_id": version_id,
        "last_modified": timestamp,
        "size_bytes": len(contents),
        "is_deleted": deleted,
        "revision": current_revision,
    }
    _atomic_write_bytes(
        os.path.join(versions_path, f"{version_id}.json"),
        json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )


@contextmanager
def _local_versioned_mutation(file: str) -> Generator[None, None, None]:
    """Record the state before and after one successful local mutation."""
    history_path = _local_history_path(file)
    existed = os.path.isfile(file)
    if history_path is not None and existed:
        _append_local_version(file, deleted=False)

    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        if history_path is not None:
            if os.path.isfile(file):
                _append_local_version(file, deleted=False, force=succeeded)
            elif existed:
                _append_local_version(file, deleted=True)


def get_bucket_key(path: str) -> tuple[str, str]:
    """Get the bucket and key from a S3 path."""
    path = path.replace(S3_PREFIX, "")
    if "/" in path:
        bucket, key = path.split("/", 1)
        return bucket, key
    return path, ""


def file_exists(file: str) -> bool:
    """Preserve the legacy boolean existence API over the physical handler."""
    try:
        return BinaryFileHandler.exists(file)
    except (s3_client.exceptions.ClientError, OSError, VersioningNotSupportedError):
        return False


@contextmanager
def _get_response(url: str) -> Generator[HTTPResponse, None, None]:
    """Read a file from HTTPS with UTF-8 encoding."""
    try:
        with urllib.request.urlopen(url) as resp:
            if resp.status != 200:  # pragma: no cover
                raise FileNotFoundError(f"HTTPS file {url} returned status {resp.status}")
            yield resp
    except urllib.error.URLError as exc:
        raise FileNotFoundError(f"HTTPS file {url} could not be read: {exc}") from exc


def list_file_versions(file: str) -> list[FileVersionMetadata]:
    """Return newest-first historical versions and delete markers for a file."""
    return BinaryFileHandler.history(file)


def read_file_version(file: str, version: FileVersionMetadata) -> str:
    """Read a previously listed file version without listing its metadata again."""
    with TextFileHandler(file, version_id=version) as source:
        return source.read()


def read_file(file: str, *, version_id: str | None = None) -> str:
    """Read a file from S3, HTTPS, or local storage with UTF-8 encoding."""
    with TextFileHandler(file, version_id=version_id) as source:
        return source.read()


def read_file_with_version(file: str, *, version_id: str | None = None) -> tuple[str, str]:
    """Read UTF-8 text with its S3 ETag or local modification version."""
    with TextFileHandler(file, version_id=version_id) as source:
        contents = source.read()
    if source.version is None:
        raise VersioningNotSupportedError("HTTPS paths do not expose concurrency versions")
    return contents, source.version


def _s3_version_metadata(key: str, response: Mapping[str, Any]) -> FileVersionMetadata:
    """Convert a successful S3 HeadObject response to portable metadata."""
    return FileVersionMetadata(
        name=os.path.basename(key),
        version_id=str(response.get("VersionId", response["ETag"])).strip('"'),
        last_modified=cast(datetime, response["LastModified"]).astimezone(UTC),
        size_bytes=int(response["ContentLength"]),
        is_latest=True,
        is_deleted=False,
        revision=str(response["ETag"]).strip('"'),
    )


def write_file(file: str, data: str | bytes, *, content_type: str | None = None) -> str:
    """Write a file through the binary handler and return its revision."""
    return BinaryFileHandler.publish(file, data, content_type=content_type)


def update_file_if_version_matches(
    file: str,
    data: str | bytes,
    version: str,
    *,
    content_type: str | None = None,
) -> str:
    """Conditionally write through the binary handler and return its revision."""
    return BinaryFileHandler.publish(file, data, expected_version=version, content_type=content_type)


def delete_file(file: str) -> None:
    """Delete a file through the binary handler."""
    BinaryFileHandler.delete(file)


def _delete_files_s3(files: list[str], max_workers: int = 500) -> None:

    # Split into S3 and local paths
    s3_by_bucket: dict[str, list[str]] = {}

    for f in files:
        bucket, key = get_bucket_key(f)
        if not key:  # pragma: no cover
            # Skip bucket-only paths
            continue
        s3_by_bucket.setdefault(bucket, []).append(key)

    def _s3_delete_chunk(bucket: str, keys_chunk: list[str]) -> None:
        if not keys_chunk:  # pragma: no cover
            return
        # Quiet response to minimize payload; S3 ignores non-existent keys
        s3_client.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [{"Key": k} for k in keys_chunk],
                "Quiet": True,
            },
        )

    # Execute S3 batch deletions and local deletions in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Queue S3 batches
        for bucket, keys in s3_by_bucket.items():
            for i in range(0, len(keys), 1000):
                executor.submit(_s3_delete_chunk, bucket, keys[i : i + 1000])


def _delete_files_local(files: list[str], max_workers: int = 500) -> None:
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(BinaryFileHandler.delete, files)


def delete_files(files: list[str], max_workers: int = 500) -> None:
    """
    Delete multiple files from S3 or local storage.

    - For S3 paths, uses the `delete_objects` batch API (up to 1000 keys/request)
      and groups deletions by bucket for efficiency.
    - For local paths, deletes concurrently using up to `max_workers` threads.
    """
    s3_files = [f for f in files if f.startswith(S3_PREFIX)]
    local_files = [f for f in files if not f.startswith(S3_PREFIX)]

    if s3_files:
        _delete_files_s3(s3_files, max_workers=max_workers)
    if local_files:
        _delete_files_local(local_files, max_workers=max_workers)


@contextmanager
def _open_copy_source(path: str) -> Generator[Any, None, None]:
    if path.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(path)
        try:
            body = s3_client.get_object(Bucket=bucket, Key=key)["Body"]
        except s3_client.exceptions.ClientError as exc:
            raise FileNotFoundError(f"File {path} not found") from exc

        try:
            yield body
        finally:
            body.close()
    elif path.startswith(_HTTPS_PREFIX):
        with _get_response(path) as response:
            yield response
    else:
        with open(path, "rb") as source:
            yield source


def _copy_stream(source: Any, destination: IO[bytes], chunk_size: int) -> None:
    while chunk := source.read(chunk_size):
        destination.write(chunk)


def _copy_to_s3(src: str, dst: str, chunk_size: int) -> None:
    bucket, key = get_bucket_key(dst)
    multipart_chunk_size = max(chunk_size, _S3_MULTIPART_CHUNK_SIZE)
    transfer_config = TransferConfig(
        multipart_threshold=multipart_chunk_size,
        multipart_chunksize=multipart_chunk_size,
        use_threads=False,
    )

    with _open_copy_source(src) as source:
        try:
            s3_client.upload_fileobj(
                Fileobj=source,
                Bucket=bucket,
                Key=key,
                Config=transfer_config,
            )
        except s3_client.exceptions.ClientError as exc:
            raise PermissionError(f"Cannot write to {dst}.") from exc


def _copy_to_local(src: str, dst: str, chunk_size: int) -> None:
    destination_directory = os.path.dirname(dst) or "."
    os.makedirs(destination_directory, exist_ok=True)
    temporary_path = os.path.join(destination_directory, f".{os.path.basename(dst)}.{uuid4().hex}.tmp")
    destination_mode = stat.S_IMODE(os.stat(dst).st_mode) if os.path.exists(dst) else None

    try:
        with _open_copy_source(src) as source:
            with open(temporary_path, "xb") as destination:
                _copy_stream(source, destination, chunk_size)

        if destination_mode is not None:
            os.chmod(temporary_path, destination_mode)
        with _local_versioned_mutation(dst):
            os.replace(temporary_path, dst)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def copy_file(src: str, dst: str, chunk_size: int = 131072) -> None:
    """Copy a file between S3, HTTPS, and local storage."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    if src.startswith(S3_PREFIX) and dst.startswith(S3_PREFIX):
        src_bucket, src_key = get_bucket_key(src)
        dst_bucket, dst_key = get_bucket_key(dst)

        try:
            s3_client.copy_object(
                Bucket=dst_bucket,
                Key=dst_key,
                CopySource={"Bucket": src_bucket, "Key": src_key},
            )
        except s3_client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(f"File {src} not found") from exc
    elif dst.startswith(S3_PREFIX):
        _copy_to_s3(src, dst, chunk_size)
    elif dst.startswith(_HTTPS_PREFIX):
        raise NotImplementedError("Only 'rb' mode is supported for https:// paths")
    else:
        _copy_to_local(src, dst, chunk_size)


def move_file(src: str, dst: str) -> None:
    """Move a file from S3 or local storage."""
    copy_file(src, dst)
    BinaryFileHandler.delete(src)


def _get_files_s3(
    root_path: str, prefix: str = "", cutoff_utc: datetime | None = None, cutoff_earlier: bool = True
) -> list[str]:
    bucket, key = get_bucket_key(root_path)
    key = key + "/" if key and not key.endswith("/") else key

    paginator = s3_client.get_paginator("list_objects_v2")

    # Normalize cutoff_date to timezone-aware UTC for comparison consistency
    iterator = paginator.paginate(Bucket=bucket, Prefix=key + prefix, PaginationConfig={"PageSize": 10_000})

    if cutoff_utc:
        comparator = "<" if cutoff_earlier else ">="
        search = (
            "Contents[?to_string(LastModified)"
            + comparator
            + "'\""
            + cutoff_utc.strftime("%Y-%m-%d %H:%M:%S%z")
            + "\"'].Key"
        )
    else:
        search = "Contents[].Key"
    return [file.removeprefix(key) for file in iterator.search(search) if file]


def _generate_local_files(root_path: str, prefix: str = "") -> Generator[str, None, None]:
    root_path = os.path.abspath(os.path.normpath(root_path)) + os.path.sep
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [name for name in dirnames if name != _LOCAL_HISTORY_DIRECTORY]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root_path)
            if not prefix or rel.startswith(prefix):
                yield rel


def _generate_local_files_cutoff(
    cutoff_utc: datetime, root_path: str, prefix: str = "", cutoff_earlier: bool = True
) -> Generator[str, None, None]:
    # Normalize cutoff_date to timezone-aware UTC for comparison consistency
    for rel in _generate_local_files(root_path, prefix):
        full_path = os.path.join(root_path, rel)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(full_path), tz=UTC)
        except FileNotFoundError:  # pragma: no cover
            # File may have been deleted during traversal; skip
            continue
        if (mtime < cutoff_utc) == cutoff_earlier:
            yield rel


def get_files(
    root_path: str, prefix: str = "", cutoff_date: datetime | None = None, cutoff_earlier: bool = True
) -> list[str]:
    """
    Return a list of files from S3 or local storage with the relevant prefix.

    - If `cutoff_date` is provided, only returns files whose last modified time is strictly
      earlier or later than `cutoff_date`.
    - If `cutoff_earlier` is True (default), returns files older than `cutoff_date`,
      otherwise returns files newer or equal to `cutoff_date`.
    - The prefix significantly improves performance for S3 by reducing the number of objects listed.
    """
    # Normalize cutoff_date to timezone-aware UTC for comparison consistency
    cutoff_utc: datetime | None
    if cutoff_date is None:
        cutoff_utc = None
    else:
        cutoff_utc = cutoff_date if cutoff_date.tzinfo else cutoff_date.replace(tzinfo=UTC)
        cutoff_utc = cutoff_utc.astimezone(UTC)

    if root_path.startswith(S3_PREFIX):
        return _get_files_s3(root_path, prefix, cutoff_utc, cutoff_earlier)

    if cutoff_utc:
        return list(_generate_local_files_cutoff(cutoff_utc, root_path, prefix, cutoff_earlier))
    return list(_generate_local_files(root_path, prefix))


def _s3_get_folders(root_path: str, prefix: str = "") -> list[str]:
    bucket, key = get_bucket_key(root_path)
    # Ensure the key ends with a trailing slash for prefixing
    key = key + "/" if key and not key.endswith("/") else key

    paginator = s3_client.get_paginator("list_objects_v2")
    folders: list[str] = []

    # Use Delimiter='/' to obtain top-level "folders" (CommonPrefixes)
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=(key + prefix) if prefix else key,
        Delimiter="/",
        PaginationConfig={"PageSize": 1000},
    ):
        if "CommonPrefixes" in page:
            folders.extend([p["Prefix"].removeprefix(key).rstrip("/") for p in page["CommonPrefixes"]])

    return folders


def _local_get_folders(root_path: str, prefix: str = "") -> list[str]:
    # Local filesystem: list immediate directories in root_path (non-recursive)
    root_path = os.path.abspath(root_path)
    if not os.path.isdir(root_path):
        return []

    results: list[str] = []
    try:
        for entry in os.listdir(root_path):
            if entry == _LOCAL_HISTORY_DIRECTORY:
                continue
            if prefix and not entry.startswith(prefix):
                continue
            full = os.path.join(root_path, entry)
            if os.path.isdir(full):
                results.append(entry)
    except FileNotFoundError:
        return []

    return results


def get_folders(root_path: str, prefix: str = "") -> list[str]:
    """
    Return a list of immediate subfolders from S3 or local storage with an optional prefix filter.

    Non-recursive: only immediate children are returned (no nested folder paths).
    """
    if root_path.startswith(S3_PREFIX):
        return _s3_get_folders(root_path, prefix)
    return _local_get_folders(root_path, prefix)


def list_files(root_path: str, file_type: str, prefix: str = "", *, include_deleted: bool = False) -> list[str]:
    """List matching files through the binary handler."""
    return BinaryFileHandler.list_files(root_path, file_type, prefix, include_deleted=include_deleted)


def _summarize_versions(
    versions: list[FileVersionMetadata],
) -> tuple[dict[str, FileVersionMetadata], dict[str, FileVersionMetadata]]:
    """Return each key's current state and newest readable version."""
    latest_versions = {version.name: version for version in versions if version.is_latest}
    latest_readable_versions: dict[str, FileVersionMetadata] = {}
    for version in versions:
        current = latest_readable_versions.get(version.name)
        if not version.is_deleted and (current is None or version.last_modified > current.last_modified):
            latest_readable_versions[version.name] = version
    return latest_versions, latest_readable_versions


def list_files_with_metadata(
    root_path: str,
    file_type: str,
    prefix: str = "",
    *,
    include_deleted: bool = False,
) -> list[FileMetadata]:
    """List portable file metadata through the binary handler."""
    return BinaryFileHandler.list_files_with_metadata(root_path, file_type, prefix, include_deleted=include_deleted)


def restore_file_version(file: str, version_id: str) -> FileVersionMetadata:
    """Restore historical content through the binary handler."""
    return BinaryFileHandler.restore_version(file, version_id)


def undelete_file(file: str) -> FileVersionMetadata:
    """Restore deleted content through the binary handler."""
    return BinaryFileHandler.undelete(file)


def most_recent_timestamp(root_path: str, file_type: str) -> float:
    """Return the most recent timestamp from S3 or local storage with the suffix."""
    if root_path.startswith(S3_PREFIX):
        default = datetime(1970, 1, 1, tzinfo=UTC)

        def _get_timestamps() -> Generator[datetime, Any, None]:
            """Get the max timestamp on each page in the paginator."""
            paginator = s3_client.get_paginator("list_objects_v2")
            bucket, key = get_bucket_key(root_path)
            for page in paginator.paginate(Bucket=bucket, Prefix=key, Delimiter="/"):
                if "Contents" in page:
                    yield max(
                        [obj["LastModified"] for obj in page["Contents"] if obj["Key"].endswith(file_type)],
                        default=default,
                    )

        return max(_get_timestamps(), default=default).timestamp()

    return max(
        [os.path.getmtime(f) for f in glob(os.path.join(root_path, f"*.{file_type}"))],
        default=0,
    )


def get_public_url(file: str, expires_in: int = 3600, method: str = "get_object") -> str:
    """Get a signed URL for a file on S3."""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod=method,
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )

        return str(presigned_url)

    return file


class BinaryFileHandler:
    """File handler for S3, local storage, or HTTPS (read-only)."""

    _buffer: IO[Any]

    def __init__(
        self,
        path: str,
        mode: str = "rb",
        content_type: str | None = None,
        *,
        versioned: bool = False,
        version_id: str | FileVersionMetadata | None = None,
        expected_version: str | None = None,
    ) -> None:
        """
        Create the class, emulating the file object.

        For S3, returns a BytesIO object for writing, and downloads the file
        For local storage, returns a file object
        For HTTPS, supports read-only ("rb") mode and fetches the file via HTTP(S)
        """
        if mode not in ["rb", "wb"]:
            raise NotImplementedError(f"mode {mode} is not supported")

        if version_id is not None and mode != "rb":
            raise ValueError("version_id requires read mode")
        if expected_version is not None and mode != "wb":
            raise ValueError("expected_version requires write mode")
        self.versioned = versioned or version_id is not None or expected_version is not None
        self._version_metadata = version_id if isinstance(version_id, FileVersionMetadata) else None
        self.version_id = (
            self._version_metadata.version_id if self._version_metadata is not None else cast(str | None, version_id)
        )
        self.expected_version = expected_version
        self.version: str | None = None
        self.path = path
        self.mode = mode
        self.content_type = content_type
        self.is_s3 = path.startswith(S3_PREFIX)
        self.is_https = path.startswith("https://")
        self._local_mutation: Any | None = None

        if self.is_https:
            if self.mode != "rb":
                raise NotImplementedError("Only 'rb' mode is supported for https:// paths")
            self._buffer = BytesIO()
        elif self.is_s3 or (self.versioned and (mode == "wb" or version_id is not None)):
            self._buffer = BytesIO()
        else:
            self._open_local()

    @classmethod
    def _head(cls, file: str) -> Mapping[str, Any]:
        """Read S3 metadata once with consistent absence and permission handling."""
        bucket, key = get_bucket_key(file)
        try:
            return cast(Mapping[str, Any], s3_client.head_object(Bucket=bucket, Key=key))
        except s3_client.exceptions.ClientError as error:
            if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                raise FileNotFoundError(f"File {file} not found") from error
            raise

    @classmethod
    def current_version(cls, file: str) -> str:
        """Return the revision from the same live-metadata implementation."""
        return cls.metadata(file).version

    @classmethod
    def exists(cls, file: str) -> bool:
        """Check physical existence without suppressing permission failures."""
        try:
            cls.current_version(file)
        except FileNotFoundError:
            return False
        return True

    @classmethod
    def publish(
        cls,
        file: str,
        data: str | bytes,
        *,
        expected_version: str | None = None,
        content_type: str | None = None,
    ) -> str:
        """Publish bytes or legacy UTF-8 text, optionally conditioned on a revision."""
        if file.startswith(S3_PREFIX):
            return cls._publish_s3(file, data, expected_version, content_type)
        return cls._publish_local(file, data, expected_version)

    @classmethod
    def _publish_s3(cls, file: str, data: str | bytes, expected: str | None, content_type: str | None) -> str:
        bucket, key = get_bucket_key(file)
        options: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data.encode("UTF-8") if isinstance(data, str) else data,
        }
        if expected is not None:
            options["IfMatch"] = expected
        if content_type is not None:
            options["ContentType"] = content_type
        try:
            response = s3_client.put_object(**options)
        except s3_client.exceptions.ClientError as error:
            if expected is not None:
                code = error.response["Error"]["Code"]
                if code == "NoSuchKey":
                    raise FileNotFoundError(f"File {file} not found") from error
                if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                    raise VersionMismatchError(f"File version does not match {expected}") from error
            raise
        return str(response["ETag"]).strip('"')

    @classmethod
    def _publish_local(cls, file: str, data: str | bytes, expected: str | None) -> str:
        if expected is not None and cls.current_version(file) != expected:
            raise VersionMismatchError(f"File version does not match {expected}")
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
        if expected is None:
            with _local_versioned_mutation(file):
                if isinstance(data, bytes):
                    _atomic_write_bytes(os.path.abspath(file), data)
                else:
                    with open(file, mode="w", encoding="UTF-8") as output:
                        output.write(data)
            return cls.current_version(file)
        directory = os.path.dirname(file) or "."
        descriptor, temporary_path = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(
                descriptor,
                mode="wb" if isinstance(data, bytes) else "w",
                encoding=None if isinstance(data, bytes) else "UTF-8",
            ) as output:
                output.write(data)
            with _local_versioned_mutation(file):
                os.replace(temporary_path, file)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        return cls.current_version(file)

    @classmethod
    def version_metadata(cls, file: str) -> FileVersionMetadata:
        """Return current version metadata without enumerating history."""
        if file.startswith(S3_PREFIX):
            bucket, key = get_bucket_key(file)
            response = cls._head(file)
            return _s3_version_metadata(key, response)
        versions = _load_local_versions_from_path(_require_local_history_path(file))
        if not versions:
            raise VersionNotFoundError(f"Current version of {file} not found")
        return versions[0]

    @classmethod
    def metadata(cls, file: str) -> FileMetadata:
        """Read live portable metadata without enumerating a directory or history."""
        if file.startswith(S3_PREFIX):
            version = cls.version_metadata(file)
            return FileMetadata(
                name=version.name,
                last_modified=version.last_modified,
                size_bytes=version.size_bytes,
                version=version.revision or version.version_id,
            )
        if file.startswith(_HTTPS_PREFIX):
            raise VersioningNotSupportedError("HTTPS paths do not expose portable metadata")
        info = os.stat(file)
        return FileMetadata(
            name=os.path.basename(file),
            last_modified=datetime.fromtimestamp(info.st_mtime, UTC),
            size_bytes=info.st_size,
            version=str(info.st_mtime_ns),
        )

    @classmethod
    def delete(cls, file: str) -> None:
        """Delete a file from S3 or local storage."""
        if file.startswith(S3_PREFIX):
            bucket, key = get_bucket_key(file)
            s3_client.delete_object(Bucket=bucket, Key=key)
        else:
            with _local_versioned_mutation(file):
                try:
                    os.remove(file)
                except FileNotFoundError:
                    pass

    @classmethod
    def history(cls, file: str) -> list[FileVersionMetadata]:
        """Return newest-first historical versions and delete markers for a file."""
        return cls._list_versions(file, exact=True)

    @classmethod
    def list_files(
        cls, root_path: str, file_type: str, prefix: str = "", *, include_deleted: bool = False
    ) -> list[str]:
        """
        Return a list of files from S3 or local storage with the relevant suffix and optional prefix.

        The prefix significantly improves performance for S3 by reducing the number of objects listed.
        """
        if include_deleted:
            return [
                file.name for file in cls.list_files_with_metadata(root_path, file_type, prefix, include_deleted=True)
            ]
        if root_path.startswith(S3_PREFIX):
            return [
                f.removesuffix(f".{file_type}") for f in get_files(root_path, prefix) if f.endswith(f".{file_type}")
            ]
        return [
            os.path.split(f)[1][: -len(file_type) - 1] for f in glob(os.path.join(root_path, f"{prefix}*.{file_type}"))
        ]

    @classmethod
    def list_files_with_metadata(
        cls, root_path: str, file_type: str, prefix: str = "", *, include_deleted: bool = False
    ) -> list[FileMetadata]:
        """Return files and their portable metadata from S3 or local storage."""
        if include_deleted:
            suffix = f".{file_type}"
            versions = cls._list_versions(root_path, exact=False)
            latest_versions, latest_readable_versions = _summarize_versions(versions)
            versioned = [
                FileMetadata(
                    name=version.name.removesuffix(suffix),
                    last_modified=version.last_modified,
                    size_bytes=version.size_bytes,
                    version=version.version_id if version.is_deleted else version.revision or version.version_id,
                    is_deleted=version.is_deleted,
                    latest_readable_version=latest_readable_versions.get(version.name),
                )
                for version in latest_versions.values()
                if version.name.endswith(suffix) and version.name.removesuffix(suffix).startswith(prefix)
            ]
            if root_path.startswith(S3_PREFIX):
                return versioned
            current = cls.list_files_with_metadata(root_path, file_type, prefix)
            versioned_by_name = {file.name: file for file in versioned}
            current = [
                (
                    replace(file, latest_readable_version=versioned_by_name[file.name].latest_readable_version)
                    if file.name in versioned_by_name
                    else file
                )
                for file in current
            ]
            current_names = {file.name for file in current}
            deleted = [file for file in versioned if file.is_deleted and file.name not in current_names]
            return current + deleted
        suffix = f".{file_type}"
        if root_path.startswith(S3_PREFIX):
            bucket, key = get_bucket_key(root_path)
            key = key + "/" if key and (not key.endswith("/")) else key
            paginator = s3_client.get_paginator("list_objects_v2")
            files: list[FileMetadata] = []
            for page in paginator.paginate(Bucket=bucket, Prefix=key + prefix, PaginationConfig={"PageSize": 10000}):
                for item in page.get("Contents", []):
                    relative = str(item["Key"]).removeprefix(key)
                    if relative.endswith(suffix):
                        files.append(
                            FileMetadata(
                                name=relative.removesuffix(suffix),
                                last_modified=cast(datetime, item["LastModified"]).astimezone(UTC),
                                size_bytes=int(item["Size"]),
                                version=str(item["ETag"]).strip('"'),
                            )
                        )
            return files
        files = []
        for file in glob(os.path.join(root_path, f"{prefix}*{suffix}")):
            try:
                file_stat = os.stat(file)
            except FileNotFoundError:
                continue
            files.append(
                FileMetadata(
                    name=os.path.basename(file).removesuffix(suffix),
                    last_modified=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
                    size_bytes=file_stat.st_size,
                    version=str(file_stat.st_mtime_ns),
                )
            )
        return files

    @classmethod
    def _list_versions(cls, path: str, *, exact: bool) -> list[FileVersionMetadata]:
        """List versions through the single historical-discovery backend branch."""
        if path.startswith(S3_PREFIX):
            return cls._list_s3_versions(path, exact=exact)
        if path.startswith(_HTTPS_PREFIX):
            raise VersioningNotSupportedError("HTTPS paths do not expose version history")
        return cls._list_local_versions(path, exact=exact)

    @classmethod
    def _list_s3_versions(cls, path: str, *, exact: bool) -> list[FileVersionMetadata]:
        """List S3 object versions for one file or every file below a root."""
        bucket, key = get_bucket_key(path)
        prefix = key if exact else key + "/" if key and (not key.endswith("/")) else key
        paginator = s3_client.get_paginator("list_object_versions")
        versions: list[FileVersionMetadata] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": 1000}):
            version_groups = (
                (False, cast(list[dict[str, Any]], page.get("Versions", []))),
                (True, cast(list[dict[str, Any]], page.get("DeleteMarkers", []))),
            )
            for is_deleted, items in version_groups:
                for item in items:
                    item_key = str(item["Key"])
                    if exact and item_key != key:
                        continue
                    name = os.path.basename(item_key) if exact else item_key.removeprefix(prefix)
                    versions.append(
                        FileVersionMetadata(
                            name=name,
                            version_id=str(item["VersionId"]),
                            last_modified=cast(datetime, item["LastModified"]).astimezone(UTC),
                            size_bytes=0 if is_deleted else int(item["Size"]),
                            is_latest=bool(item["IsLatest"]),
                            is_deleted=is_deleted,
                            revision=None if is_deleted else str(item["ETag"]).strip('"'),
                        )
                    )
        versions.sort(key=lambda item: (item.last_modified, item.is_latest), reverse=True)
        return versions

    @classmethod
    def _list_local_versions(cls, path: str, *, exact: bool) -> list[FileVersionMetadata]:
        """List local sidecar versions for one file or every file below a root."""
        if exact:
            return _load_local_versions_from_path(_require_local_history_path(path))
        history_root = os.path.join(os.path.abspath(path), _LOCAL_HISTORY_DIRECTORY)
        if not _local_versioning_enabled():
            raise VersioningNotEnabledError(f"Set {_LOCAL_VERSIONING_ENV}=true to enable local version history")
        versions: list[FileVersionMetadata] = []
        for history_path in glob(os.path.join(history_root, "*")):
            if os.path.isdir(history_path):
                versions.extend(_load_local_versions_from_path(history_path))
        return versions

    @classmethod
    def _restore_known_version(cls, file: str, version: FileVersionMetadata) -> FileVersionMetadata:
        """Restore exact historical bytes as a new current version."""
        with cls(file, version_id=version) as source:
            data = source.read()
        cls.publish(file, data)
        return cls.version_metadata(file)

    @classmethod
    def _restore_s3_version(cls, file: str, version_id: str) -> FileVersionMetadata:
        """Restore an S3 version with a server-side copy."""
        bucket, key = get_bucket_key(file)
        try:
            s3_client.copy_object(
                Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": key, "VersionId": version_id}
            )
        except s3_client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchVersion"}:
                raise VersionNotFoundError(f"Version {version_id} of {file} not found") from exc
            raise
        return cls.version_metadata(file)

    @classmethod
    def restore_version(cls, file: str, version_id: str) -> FileVersionMetadata:
        """Restore historical text content as a new current version."""
        if file.startswith(S3_PREFIX):
            return cls._restore_s3_version(file, version_id)
        return cls._restore_known_version(file, _load_local_version(file, version_id))

    @classmethod
    def undelete(cls, file: str) -> FileVersionMetadata:
        """Restore the newest available content when the current state is deleted."""
        if file.startswith(S3_PREFIX):
            return cls._undelete_s3(file)
        versions = cls.history(file)
        if not versions:
            raise FileNotFoundError(f"File {file} has no recoverable versions")
        current = next((version for version in versions if version.is_latest), None)
        if current is None:
            raise FileNotFoundError(f"File {file} has no current version")
        if not current.is_deleted:
            raise FileNotDeletedError(f"File {file} is not deleted")
        try:
            version = max((item for item in versions if not item.is_deleted), key=lambda item: item.last_modified)
        except ValueError as exc:
            raise FileNotFoundError(f"File {file} has no recoverable versions") from exc
        return cls._restore_known_version(file, version)

    @classmethod
    def _undelete_s3(cls, file: str) -> FileVersionMetadata:
        """Remove current S3 delete markers so the newest live version becomes current."""
        bucket, key = get_bucket_key(file)
        removed_marker = False
        while True:
            try:
                response = s3_client.head_object(Bucket=bucket, Key=key)
            except s3_client.exceptions.ClientError as exc:
                headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
                if str(headers.get("x-amz-delete-marker", "")).lower() != "true":
                    if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                        raise FileNotFoundError(f"File {file} has no recoverable versions") from exc
                    raise
                version_id = headers.get("x-amz-version-id")
                if not version_id:
                    raise VersionNotFoundError(f"Current delete marker of {file} has no version ID") from exc
                s3_client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
                removed_marker = True
                continue
            if not removed_marker:
                raise FileNotDeletedError(f"File {file} is not deleted")
            return _s3_version_metadata(key, response)

    def _open_local(self) -> None:
        """Open the existing local stream and manage its history lifecycle."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if self.mode == "wb":
            self._local_mutation = _local_versioned_mutation(self.path)
            self._local_mutation.__enter__()
        try:
            self._buffer = open(self.path, self.mode)  # pylint: disable=consider-using-with
            if self.mode == "rb":
                self.version = str(os.fstat(self._buffer.fileno()).st_mtime_ns)
        except BaseException as error:
            if self._local_mutation is not None:
                self._local_mutation.__exit__(type(error), error, error.__traceback__)
                self._local_mutation = None
            raise

    def _set_buffer_http(self) -> None:
        with _get_response(self.path) as response:
            self._buffer = BytesIO(response.read())
        self._buffer.seek(0)

    def _set_buffer_s3(self) -> None:
        bucket, key = get_bucket_key(self.path)
        if self.mode == "rb":
            if self.versioned:
                try:
                    options: dict[str, Any] = {"Bucket": bucket, "Key": key}
                    if self.version_id is not None:
                        options["VersionId"] = self.version_id
                    response = s3_client.get_object(**options)
                except s3_client.exceptions.ClientError as error:
                    code = error.response["Error"]["Code"]
                    if code == "MethodNotAllowed" and self.version_id is not None:
                        raise FileVersionDeletedError(
                            f"Version {self.version_id} of {self.path} is a delete marker"
                        ) from error
                    if code in {"NoSuchKey", "NoSuchVersion", "404", "NotFound"}:
                        error_type = VersionNotFoundError if self.version_id is not None else FileNotFoundError
                        raise error_type(f"File {self.path} version {self.version_id} not found") from error
                    raise
                with response["Body"] as source:
                    self._buffer.write(source.read())
                self.version = str(response["ETag"]).strip('"')
                self._buffer.seek(0)
                return
            # Download the file from S3 to the stream
            try:
                s3_client.download_fileobj(Bucket=bucket, Key=key, Fileobj=self._buffer)
            except s3_client.exceptions.ClientError as exc:
                raise FileNotFoundError(f"File {self.path} not found") from exc
            self._buffer.seek(0)

    def _set_buffer_local_version(self) -> None:
        """Read immutable sidecar bytes using known metadata when available."""
        assert self.version_id is not None
        metadata = self._version_metadata or _load_local_version(self.path, self.version_id)
        if metadata.is_deleted:
            raise FileVersionDeletedError(f"Version {self.version_id} of {self.path} is a delete marker")
        history_path = _require_local_history_path(self.path)
        data_path = os.path.join(history_path, "versions", f"{self.version_id}.data")
        try:
            with open(data_path, "rb") as source:
                self._buffer.write(source.read())
        except FileNotFoundError as error:  # pragma: no cover - corrupt sidecar
            raise VersionNotFoundError(f"Version {self.version_id} of {self.path} not found") from error
        self.version = metadata.revision or metadata.version_id
        self._buffer.seek(0)

    def __enter__(self) -> BytesIO | IO:
        """Read current or historical content through the appropriate backend."""
        if self._version_metadata is not None and self._version_metadata.is_deleted:
            raise FileVersionDeletedError(f"Version {self.version_id} of {self.path} is a delete marker")
        if self.is_https:
            if self.version_id is not None:
                raise VersioningNotSupportedError("HTTPS paths do not expose version history")
            self._set_buffer_http()
        elif self.is_s3:
            self._set_buffer_s3()
        elif self.version_id is not None:
            self._set_buffer_local_version()
        return self._buffer

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Write to S3 or local storage and close the stream."""
        try:
            if self.versioned and self.mode == "wb":
                if exc_type is None:
                    self._buffer.seek(0)
                    data = self._buffer.read()
                    self.version = self.publish(
                        self.path,
                        data,
                        expected_version=self.expected_version,
                        content_type=self.content_type,
                    )
            elif self.is_s3 and self.mode == "wb":
                self._buffer.seek(0)
                bucket, key = get_bucket_key(self.path)
                try:
                    s3_client.upload_fileobj(
                        Fileobj=self._buffer,
                        Bucket=bucket,
                        Key=key,
                        ExtraArgs=({"ContentType": self.content_type} if self.content_type else None),
                    )
                except s3_client.exceptions.ClientError as exc:
                    raise PermissionError(f"Cannot write to {self.path}.") from exc
        finally:
            self._buffer.close()
            if self._local_mutation is not None:
                self._local_mutation.__exit__(exc_type, exc_value, traceback)
                self._local_mutation = None


class GzipFileHandler:
    """Wrap BinaryFileHandler with gzip while preserving its version and publication lifecycle."""

    def __init__(
        self,
        path: str,
        mode: str = "rb",
        encoding: str | None = None,
        *,
        compression_level: int = 9,
        versioned: bool = False,
        version_id: str | FileVersionMetadata | None = None,
        expected_version: str | None = None,
    ) -> None:
        """Configure gzip and optional current, historical or conditional binary I/O."""
        if mode not in ("rb", "wb", "rt", "wt"):
            raise NotImplementedError(f"mode {mode} is not supported")
        if not 0 <= compression_level <= 9:
            raise ValueError("compression_level must be between 0 and 9")
        self._binary = BinaryFileHandler(
            path,
            mode.replace("t", "b"),
            versioned=versioned,
            version_id=version_id,
            expected_version=expected_version,
        )
        self._mode = mode
        self._encoding = encoding
        self._compression_level = compression_level
        self._contexts = ExitStack()

    @property
    def version(self) -> str | None:
        """Return the revision read or successfully published by the binary handler."""
        return self._binary.version

    def __enter__(self) -> Any:
        """Open gzip after the underlying binary read has completed."""
        with ExitStack() as contexts:
            source = contexts.enter_context(self._binary)
            stream = contexts.enter_context(
                gzip.open(source, self._mode, compresslevel=self._compression_level, encoding=self._encoding)
            )
            self._contexts = contexts.pop_all()
            return stream

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Finish gzip before the binary handler decides whether to publish."""
        self._contexts.__exit__(exc_type, exc_value, traceback)


def _validate_compression(compression: str | None, level: int) -> None:
    """Validate the compression settings shared by typed file handlers."""
    if compression not in {None, "gzip"}:
        raise ValueError("compression must be None or 'gzip'")
    if not 0 <= level <= 9:
        raise ValueError("compression_level must be between 0 and 9")


class TextFileHandler:
    """Add text encoding and newline handling to version-aware binary or gzip I/O."""

    def __init__(
        self,
        path: str,
        mode: str = "r",
        *,
        encoding: str = "utf-8",
        newline: str | None = "auto",
        compression: Literal["gzip"] | None = None,
        compression_level: int = 1,
        version_id: str | FileVersionMetadata | None = None,
        expected_version: str | None = None,
    ) -> None:
        """
        Configure a text stream without opening or truncating the destination.

        Auto preserves remote line endings and normalizes local ones, matching legacy
        text reads. Explicit newline values retain TextIOWrapper semantics.
        """
        if mode not in {"r", "w"}:
            raise NotImplementedError(f"mode {mode} is not supported")
        _validate_compression(compression, compression_level)
        self._mode = mode
        self._encoding = encoding
        if newline == "auto":
            newline = "" if path.startswith((S3_PREFIX, _HTTPS_PREFIX)) else None
        self._newline = newline
        self._contexts = ExitStack()
        self._text: TextIOWrapper
        self._handler: BinaryFileHandler | GzipFileHandler
        if compression == "gzip":
            self._handler = GzipFileHandler(
                path,
                mode + "b",
                compression_level=compression_level,
                versioned=True,
                version_id=version_id,
                expected_version=expected_version,
            )
        else:
            self._handler = BinaryFileHandler(
                path,
                mode + "b",
                versioned=True,
                version_id=version_id,
                expected_version=expected_version,
            )

    @property
    def version(self) -> str | None:
        """Return the revision read or successfully written by the underlying handler."""
        return self._handler.version

    def __enter__(self) -> Self:
        """Open text while retaining the binary handler's publication ownership."""
        with ExitStack() as contexts:
            stream = contexts.enter_context(self._handler)
            self._text = TextIOWrapper(stream, encoding=self._encoding, newline=self._newline)
            contexts.callback(self._text.detach)
            self._contexts = contexts.pop_all()
        return self

    def read(self, size: int = -1) -> str:
        """Read decoded text with the configured newline behavior."""
        if self._mode != "r":
            raise UnsupportedOperation("Text handler is not readable")
        return self._text.read(size)

    def write(self, content: str) -> int:
        """Write text, flushing it before the binary handler publishes."""
        if self._mode != "w":
            raise UnsupportedOperation("Text handler is not writable")
        return self._text.write(content)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Flush and detach text before compression and publication finish."""
        self._contexts.__exit__(exc_type, exc_value, traceback)


class JsonFileHandler[ContentT]:
    """Read and write JSON values through the existing binary or gzip handler."""

    def __init__(
        self,
        path: str,
        mode: str = "r",
        *,
        compression: Literal["gzip"] | None = None,
        compression_level: int = 1,
        version_id: str | FileVersionMetadata | None = None,
        expected_version: str | None = None,
        decoder: Callable[[str], ContentT] = json.loads,
        encoder: Callable[[ContentT], str] = json.dumps,
    ) -> None:
        """Configure JSON conversion, compression and version-aware publication."""
        self._mode = mode
        self._decoder = decoder
        self._encoder = encoder
        self._handler = TextFileHandler(
            path,
            mode,
            newline="",
            compression=compression,
            compression_level=compression_level,
            version_id=version_id,
            expected_version=expected_version,
        )

    @property
    def version(self) -> str | None:
        """Return the revision read or successfully written by the underlying handler."""
        return self._handler.version

    def __enter__(self) -> Self:
        """Open the underlying stream and expose JSON read and write operations."""
        self._handler.__enter__()
        return self

    def read(self) -> ContentT:
        """Decode one JSON document using the configured decoder."""
        if self._mode != "r":
            raise UnsupportedOperation("JSON handler is not readable")
        return self._decoder(self._handler.read())

    def write(self, content: ContentT) -> None:
        """Encode one JSON document using the configured encoder."""
        if self._mode != "w":
            raise UnsupportedOperation("JSON handler is not writable")
        self._handler.write(self._encoder(content))

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Finish compression and publish only after successful JSON serialization."""
        self._handler.__exit__(exc_type, exc_value, traceback)


def purge_folder(
    root_path: str,
    dry_run: bool = True,
    max_workers: int = 500,
    cutoff_date: datetime | None = None,
) -> list[str]:
    """
    Delete all files within a folder/prefix on S3 or a local directory.

    For S3, root_path should be an S3 URL (s3://bucket/path/). Uses get_files to list objects
    under the prefix. For local paths, the function walks the directory tree recursively.
    If dry_run is True (default), no deletion is performed and the function returns the
    list of files that would be deleted.
    :param root_path: The S3 bucket/prefix or local directory to purge
    :param dry_run: If True, no files are deleted, and the function returns the list of files that would be deleted
    :param max_workers: The maximum number of worker threads to use for deletion
    :param cutoff_date: If provided, only purge files older than this datetime

    Returns a sorted list of full paths of files deleted (or that would be deleted).
    """
    if root_path.startswith(S3_PREFIX):
        full_paths = sorted(f"{root_path.rstrip('/')}/{f}" for f in get_files(root_path, cutoff_date=cutoff_date))
    else:
        full_paths = sorted(
            os.path.join(os.path.normpath(root_path), f) for f in get_files(root_path, cutoff_date=cutoff_date)
        )

    if not dry_run:
        delete_files(full_paths, max_workers=max_workers)

        if not root_path.startswith(S3_PREFIX):
            # Clean up any subdirectories
            for dirpath, _, _ in os.walk(root_path, topdown=False):
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)

    return full_paths
