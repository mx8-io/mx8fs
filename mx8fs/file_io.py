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
from collections.abc import Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from glob import glob
from io import BytesIO
from typing import IO, Any, Literal, cast
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
class FileMetadata:
    """Portable metadata returned for a listed file."""

    name: str
    last_modified: datetime
    size_bytes: int
    version: str
    is_deleted: bool = False


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
    """Check if a file exists on S3 or local storage."""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except s3_client.exceptions.ClientError:
            return False

    return os.path.exists(file)


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


def _list_s3_versions(path: str, *, exact: bool) -> list[FileVersionMetadata]:
    """List S3 object versions for one file or every file below a root."""
    bucket, key = get_bucket_key(path)
    prefix = key if exact else (key + "/" if key and not key.endswith("/") else key)
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


def _list_local_versions(path: str, *, exact: bool) -> list[FileVersionMetadata]:
    """List local sidecar versions for one file or every file below a root."""
    if exact:
        return _load_local_versions_from_path(_require_local_history_path(path))

    history_root = os.path.join(os.path.abspath(path), _LOCAL_HISTORY_DIRECTORY)
    if not _local_versioning_enabled():
        raise VersioningNotEnabledError(f"Set {_LOCAL_VERSIONING_ENV}=true to enable local version history")
    versions: list[FileVersionMetadata] = []
    for history_path in glob(os.path.join(history_root, "*")):
        if os.path.isdir(history_path):  # pragma: no branch - sidecar entries are directories
            versions.extend(_load_local_versions_from_path(history_path))
    return versions


def _list_versions(path: str, *, exact: bool) -> list[FileVersionMetadata]:
    """List versions through the single historical-discovery backend branch."""
    if path.startswith(S3_PREFIX):
        return _list_s3_versions(path, exact=exact)
    if path.startswith(_HTTPS_PREFIX):
        raise VersioningNotSupportedError("HTTPS paths do not expose version history")
    return _list_local_versions(path, exact=exact)


def list_file_versions(file: str) -> list[FileVersionMetadata]:
    """Return newest-first historical versions and delete markers for a file."""
    return _list_versions(file, exact=True)


def _version_metadata(file: str, version_id: str) -> FileVersionMetadata:
    """Return metadata for a requested local version or raise."""
    return _load_local_version(file, version_id)


def _read_version(
    file: str,
    version_id: str,
    *,
    metadata: FileVersionMetadata | None = None,
) -> tuple[str, str]:
    """Read historical content through the single historical-read backend branch."""
    if metadata is not None and metadata.is_deleted:
        raise FileVersionDeletedError(f"Version {version_id} of {file} is a delete marker")
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
        except s3_client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "MethodNotAllowed":
                raise FileVersionDeletedError(f"Version {version_id} of {file} is a delete marker") from exc
            if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchVersion"}:
                raise VersionNotFoundError(f"Version {version_id} of {file} not found") from exc
            raise
        return str(response["Body"].read().decode("utf-8")), str(response["ETag"]).strip('"')
    if file.startswith(_HTTPS_PREFIX):
        raise VersioningNotSupportedError("HTTPS paths do not expose version history")
    metadata = metadata or _version_metadata(file, version_id)
    if metadata.is_deleted:
        raise FileVersionDeletedError(f"Version {version_id} of {file} is a delete marker")
    history_path = _require_local_history_path(file)
    data_path = os.path.join(history_path, "versions", f"{version_id}.data")
    try:
        with open(data_path, encoding="UTF-8") as version_file:
            return version_file.read(), metadata.revision or metadata.version_id
    except FileNotFoundError as exc:  # pragma: no cover - corrupt sidecar
        raise VersionNotFoundError(f"Version {version_id} of {file} not found") from exc


def read_file_version(file: str, version: FileVersionMetadata) -> str:
    """Read a previously listed file version without listing its metadata again."""
    return _read_version(file, version.version_id, metadata=version)[0]


def read_file(file: str, *, version_id: str | None = None) -> str:
    """Read a file from S3, HTTPS, or local storage with UTF-8 encoding."""
    if version_id is not None:
        return _read_version(file, version_id)[0]
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            return str(s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
        except s3_client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(f"File {file} not found") from exc
    elif file.startswith("https://"):
        with _get_response(file) as response:
            return str(response.read().decode("utf-8"))
    else:
        with open(file, encoding="UTF-8") as file_io:
            return file_io.read()


def read_file_with_version(file: str, *, version_id: str | None = None) -> tuple[str, str]:
    """
    Read a file from S3 or local storage with UTF-8 encoding and a version identifier.

    For S3, the version identifier is the ETag of the file.
    For local storage, the version identifier is the last modified time of the file.

    :param file: The file to read
    :return: The file contents and the version identifier
    """
    if version_id is not None:
        return _read_version(file, version_id)
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            return str(response["Body"].read().decode("utf-8")), response["ETag"].strip('"')
        except s3_client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(f"File {file} not found") from exc
    else:
        with open(file, encoding="UTF-8") as file_io:
            return file_io.read(), str(os.fstat(file_io.fileno()).st_mtime_ns)


def _get_file_version(file: str) -> str:
    """Return the current S3 ETag or local modification version."""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            return str(s3_client.head_object(Bucket=bucket, Key=key)["ETag"]).strip('"')
        except s3_client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                raise FileNotFoundError(f"File {file} not found") from exc
            raise
    try:
        return str(os.stat(file).st_mtime_ns)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"File {file} not found") from exc


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


def _current_version_metadata(file: str) -> FileVersionMetadata:
    """Return current version metadata without enumerating history."""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            response = s3_client.head_object(Bucket=bucket, Key=key)
        except s3_client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                raise FileNotFoundError(f"File {file} not found") from exc
            raise
        return _s3_version_metadata(key, response)
    versions = _load_local_versions_from_path(_require_local_history_path(file))
    if not versions:  # pragma: no cover - successful versioned writes create metadata
        raise VersionNotFoundError(f"Current version of {file} not found")
    return versions[0]


def write_file(file: str, data: str) -> str:
    """Write a file and return its S3 ETag or local modification version."""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        response = s3_client.put_object(Bucket=bucket, Key=key, Body=data.encode("UTF-8"))
        return str(response["ETag"]).strip('"')
    else:
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
        with _local_versioned_mutation(file):
            with open(file, mode="w", encoding="UTF-8") as file_io:
                file_io.write(data)
        return str(os.stat(file).st_mtime_ns)


def update_file_if_version_matches(file: str, data: str, version: str) -> str:
    """
    Write a file to S3 or local storage with UTF-8 encoding if the version matches.

    For S3, the version identifier is the ETag of the file.
    For local storage, the version identifier is the last modified time of the file.

    :param file: The file to write.
    :param data: The data to write.
    :return: The version written by the successful conditional update.
    """
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            response = s3_client.put_object(Bucket=bucket, Key=key, Body=data.encode("UTF-8"), IfMatch=version)
            return str(response["ETag"]).strip('"')
        except s3_client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError("File does not exist") from exc
        except s3_client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ["PreconditionFailed", "ConditionalRequestConflict"]:
                raise VersionMismatchError(f"File version does not match {version}") from exc
            else:  # pragma: no cover
                raise exc
    else:
        try:
            current_version = str(os.stat(file).st_mtime_ns)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"File {file} not found") from exc
        if current_version != version:
            raise VersionMismatchError(f"File version does not match {version}")

        directory = os.path.dirname(file) or "."
        descriptor, temporary_path = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(descriptor, mode="w", encoding="UTF-8") as file_io:
                file_io.write(data)
            with _local_versioned_mutation(file):
                os.replace(temporary_path, file)
        finally:
            if os.path.exists(temporary_path):  # pragma: no cover - replace normally consumes it
                os.remove(temporary_path)
        return str(os.stat(file).st_mtime_ns)


def delete_file(file: str) -> None:
    """Delete a file from S3 or local storage."""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        s3_client.delete_object(Bucket=bucket, Key=key)
    else:
        with _local_versioned_mutation(file):
            try:
                os.remove(file)
            except FileNotFoundError:
                # Ignore if the file does not exist for S3 consistency
                pass


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
        executor.map(delete_file, files)


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
    delete_file(src)


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
    """
    Return a list of files from S3 or local storage with the relevant suffix and optional prefix.

    The prefix significantly improves performance for S3 by reducing the number of objects listed.
    """
    if include_deleted:
        return [file.name for file in list_files_with_metadata(root_path, file_type, prefix, include_deleted=True)]
    if root_path.startswith(S3_PREFIX):
        return [f.removesuffix(f".{file_type}") for f in get_files(root_path, prefix) if f.endswith(f".{file_type}")]
    return [os.path.split(f)[1][: -len(file_type) - 1] for f in glob(os.path.join(root_path, f"{prefix}*.{file_type}"))]


def list_files_with_metadata(
    root_path: str,
    file_type: str,
    prefix: str = "",
    *,
    include_deleted: bool = False,
) -> list[FileMetadata]:
    """Return files and their portable metadata from S3 or local storage."""
    if include_deleted:
        suffix = f".{file_type}"
        latest_versions = {
            version.name: version for version in _list_versions(root_path, exact=False) if version.is_latest
        }
        versioned = [
            FileMetadata(
                name=version.name.removesuffix(suffix),
                last_modified=version.last_modified,
                size_bytes=version.size_bytes,
                version=version.version_id if version.is_deleted else version.revision or version.version_id,
                is_deleted=version.is_deleted,
            )
            for version in latest_versions.values()
            if version.name.endswith(suffix) and version.name.removesuffix(suffix).startswith(prefix)
        ]
        if root_path.startswith(S3_PREFIX):
            return versioned

        current = list_files_with_metadata(root_path, file_type, prefix)
        current_names = {file.name for file in current}
        deleted = [file for file in versioned if file.is_deleted and file.name not in current_names]
        return current + deleted

    suffix = f".{file_type}"
    if root_path.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(root_path)
        key = key + "/" if key and not key.endswith("/") else key
        paginator = s3_client.get_paginator("list_objects_v2")
        files: list[FileMetadata] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=key + prefix, PaginationConfig={"PageSize": 10_000}):
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
        except FileNotFoundError:  # pragma: no cover
            # File may have been deleted after glob returned it.
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


def _list_current_file_versions(root_path: str, file_type: str) -> dict[str, str]:
    """Return storage keys mapped to their S3 ETag or local modification version."""
    return {file.name: file.version for file in list_files_with_metadata(root_path, file_type)}


def _restore_file_version(file: str, version: FileVersionMetadata) -> FileVersionMetadata:
    """Restore known historical text content as a new current version."""
    data = read_file_version(file, version)
    write_file(file, data)
    return _current_version_metadata(file)


def _restore_s3_file_version(file: str, version_id: str) -> FileVersionMetadata:
    """Restore an S3 version with a server-side copy."""
    bucket, key = get_bucket_key(file)
    try:
        s3_client.copy_object(
            Bucket=bucket,
            Key=key,
            CopySource={"Bucket": bucket, "Key": key, "VersionId": version_id},
        )
    except s3_client.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchVersion"}:
            raise VersionNotFoundError(f"Version {version_id} of {file} not found") from exc
        raise
    return _current_version_metadata(file)


def restore_file_version(file: str, version_id: str) -> FileVersionMetadata:
    """Restore historical text content as a new current version."""
    if file.startswith(S3_PREFIX):
        return _restore_s3_file_version(file, version_id)
    return _restore_file_version(file, _version_metadata(file, version_id))


def undelete_file(file: str) -> FileVersionMetadata:
    """Restore the newest available content when the current state is deleted."""
    if file.startswith(S3_PREFIX):
        return _undelete_s3_file(file)
    versions = list_file_versions(file)
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
    return _restore_file_version(file, version)


def _undelete_s3_file(file: str) -> FileVersionMetadata:
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
            if not version_id:  # pragma: no cover - S3 identifies current delete markers
                raise VersionNotFoundError(f"Current delete marker of {file} has no version ID") from exc
            s3_client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
            removed_marker = True
            continue
        if not removed_marker:
            raise FileNotDeletedError(f"File {file} is not deleted")
        return _s3_version_metadata(key, response)


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

    def __init__(self, path: str, mode: str = "rb", content_type: str | None = None):
        """
        Create the class, emulating the file object.

        For S3, returns a BytesIO object for writing, and downloads the file
        For local storage, returns a file object
        For HTTPS, supports read-only ("rb") mode and fetches the file via HTTP(S)
        """
        if mode not in ["rb", "wb"]:
            raise NotImplementedError(f"mode {mode} is not supported")

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
        elif self.is_s3:
            self._buffer = BytesIO()
        else:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if self.mode == "wb":
                self._local_mutation = _local_versioned_mutation(self.path)
                self._local_mutation.__enter__()
            try:
                self._buffer = open(  # pylint: disable=consider-using-with
                    self.path, self.mode, encoding="UTF-8" if self.mode == "w" else None
                )
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
            # Download the file from S3 to the stream
            try:
                s3_client.download_fileobj(Bucket=bucket, Key=key, Fileobj=self._buffer)
            except s3_client.exceptions.ClientError as exc:
                raise FileNotFoundError(f"File {self.path} not found") from exc
            self._buffer.seek(0)

    def __enter__(self) -> BytesIO | IO:
        """Read from S3, HTTPS, or open the stream."""
        if self.is_https:
            self._set_buffer_http()

        if self.is_s3:
            self._set_buffer_s3()

        return self._buffer

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Write to S3 or local storage and close the stream."""
        try:
            if self.is_s3 and self.mode == "wb":
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


@contextmanager
def GzipFileHandler(  # noqa: N802
    path: str, mode: str = "rb", encoding: str | None = None
) -> Generator[Any, Any, None]:
    """
    Open gzip-compressed files from S3 or local storage.

    Context manager for reading/writing gzip-compressed files from S3 or local storage,
    using BinaryFileHandler for the underlying file I/O.
    Supports binary ('rb', 'wb') and text ('rt', 'wt') modes.
    Usage:
        with GzipFileHandler(path, mode, encoding='utf-8') as f:
            f.read() / f.write(...).
    """
    if mode not in ("rb", "wb", "rt", "wt"):
        raise NotImplementedError(f"mode {mode} is not supported")
    file_mode = mode.replace("t", "b")
    with BinaryFileHandler(path, file_mode) as base_file:
        with gzip.open(base_file, mode, encoding=encoding) as gz_file:
            yield gz_file


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
