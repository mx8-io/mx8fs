"""
AWS file IO functions

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
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from glob import glob
from io import BytesIO
from typing import IO, Any, Dict, Generator, List, Literal, Tuple, cast

import boto3
from botocore.config import Config
from urllib3 import HTTPResponse

boto_config = Config(
    max_pool_connections=int(os.getenv("BOTO_MAX_CONNECTIONS", 50)),
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


class VersionMismatchError(FileNotFoundError):
    """Custom error for version mismatch when writing files"""


def get_bucket_key(path: str) -> Tuple[str, str]:
    """Get the bucket and key from a S3 path"""
    path = path.replace(S3_PREFIX, "")
    bucket, key = path.split("/", 1)
    return bucket, key


def file_exists(file: str) -> bool:
    """Check if a file exists on S3 or local storage"""
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
    """Read a file from HTTPS with UTF-8 encoding"""
    try:
        with urllib.request.urlopen(url) as resp:
            if resp.status != 200:  # pragma: no cover
                raise FileNotFoundError(f"HTTPS file {url} returned status {resp.status}")
            yield resp
    except urllib.error.URLError as exc:
        raise FileNotFoundError(f"HTTPS file {url} could not be read: {exc}") from exc


def read_file(file: str) -> str:
    """Read a file from S3, HTTPS, or local storage with UTF-8 encoding"""
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
        with open(file, mode="r", encoding="UTF-8") as file_io:
            return file_io.read()


def read_file_with_version(file: str) -> Tuple[str, str]:
    """Read a file from S3 or local storage with UTF-8 encoding and a version identifier

    For S3, the version identifier is the ETag of the file.
    For local storage, the version identifier is the last modified time of the file.

    :param file: The file to read
    :return: The file contents and the version identifier
    """
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            return str(response["Body"].read().decode("utf-8")), response["ETag"].strip('"')
        except s3_client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(f"File {file} not found") from exc
    else:
        with open(file, mode="r", encoding="UTF-8") as file_io:
            # Use the file's last modified time as a unique hash
            return file_io.read(), str(os.path.getmtime(file))


def write_file(file: str, data: str) -> None:
    """Write a file to S3 or local storage with UTF-8 encoding"""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        s3_client.put_object(Bucket=bucket, Key=key, Body=data.encode("UTF-8"))
    else:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, mode="w", encoding="UTF-8") as file_io:
            file_io.write(data)


def update_file_if_version_matches(file: str, data: str, version: str) -> None:
    """Write a file to S3 or local storage with UTF-8 encoding if the version matches.

    For S3, the version identifier is the ETag of the file.
    For local storage, the version identifier is the last modified time of the file.

    :param file: The file to write
    :param data: The data to write
    """
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        try:
            s3_client.put_object(Bucket=bucket, Key=key, Body=data.encode("UTF-8"), IfMatch=version)
        except s3_client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError("File does not exist") from exc
        except s3_client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ["PreconditionFailed", "ConditionalRequestConflict"]:
                raise VersionMismatchError(f"File with the etag {version} does not exist") from exc
            else:  # pragma: no cover
                raise exc
    else:
        if not os.path.exists(file):
            raise FileNotFoundError(f"File {file} not found")

        # Lock the local file and compare the timestamp
        from mx8fs import FileLock

        with FileLock(file) as _:
            file_mtime = os.path.getmtime(file)
            if str(file_mtime) != version:
                raise VersionMismatchError(f"File with the etag {version} does not exist")
            else:
                with open(file, mode="w", encoding="UTF-8") as file_io:
                    file_io.write(data)


def delete_file(file: str) -> None:
    """Delete a file from S3 or local storage"""
    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        s3_client.delete_object(Bucket=bucket, Key=key)
    else:
        try:
            os.remove(file)
        except FileNotFoundError:
            # Ignore if the file does not exist for S3 consistency
            pass


def delete_files(files: list[str], max_workers: int = 500) -> None:
    """Delete multiple files from S3 or local storage, using up to max_workers threads."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(delete_file, files)


def copy_file(src: str, dst: str, chunk_size: int = 131072) -> None:
    """Copy a file from S3 or local storage"""
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
    else:
        with BinaryFileHandler(src, "rb") as original_file:
            with BinaryFileHandler(dst, "wb") as new_file:
                while True:
                    chunk = original_file.read(chunk_size)
                    if not chunk:
                        break
                    new_file.write(chunk)


def move_file(src: str, dst: str) -> None:
    """Move a file from S3 or local storage"""
    copy_file(src, dst)
    delete_file(src)


def get_files(root_path: str, prefix: str = "") -> List[str]:
    """Returns a list of files from S3 or local storage with the relevant prefix.

    The prefix significantly improves performance for S3 by reducing the number of objects listed.
    """
    if root_path.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(root_path)
        key = key + "/" if not key.endswith("/") else key

        paginator = s3_client.get_paginator("list_objects_v2")
        files = []

        for page in paginator.paginate(Bucket=bucket, Prefix=key + prefix, PaginationConfig={"PageSize": 10_000}):
            if "Contents" in page:
                files.extend([obj["Key"].removeprefix(key) for obj in page["Contents"]])

        return files

    root_path = os.path.abspath(os.path.normpath(root_path)) + os.path.sep
    results: List[str] = []
    for dirpath, _, filenames in os.walk(root_path):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root_path)
            if not prefix or rel.startswith(prefix):
                results.append(rel)
    return results


def _s3_get_folders(root_path: str, prefix: str = "") -> List[str]:
    bucket, key = get_bucket_key(root_path)
    # Ensure the key ends with a trailing slash for prefixing
    key = key + "/" if key and not key.endswith("/") else key

    paginator = s3_client.get_paginator("list_objects_v2")
    folders: List[str] = []

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


def _local_get_folders(root_path: str, prefix: str = "") -> List[str]:
    # Local filesystem: list immediate directories in root_path (non-recursive)
    root_path = os.path.abspath(root_path)
    if not os.path.isdir(root_path):
        return []

    results: List[str] = []
    try:
        for entry in os.listdir(root_path):
            if prefix and not entry.startswith(prefix):
                continue
            full = os.path.join(root_path, entry)
            if os.path.isdir(full):
                results.append(entry)
    except FileNotFoundError:
        return []

    return results


def get_folders(root_path: str, prefix: str = "") -> List[str]:
    """Returns a list of immediate subfolders from S3 or local storage with an optional prefix filter.

    Non-recursive: only immediate children are returned (no nested folder paths).
    """
    if root_path.startswith(S3_PREFIX):
        return _s3_get_folders(root_path, prefix)
    return _local_get_folders(root_path, prefix)


def list_files(root_path: str, file_type: str, prefix: str = "") -> List[str]:
    """Returns a list of files from S3 or local storage with the relevant suffix and optional prefix.

    The prefix significantly improves performance for S3 by reducing the number of objects listed.
    """
    if root_path.startswith(S3_PREFIX):
        return [f.removesuffix(f".{file_type}") for f in get_files(root_path, prefix) if f.endswith(f".{file_type}")]
    return [os.path.split(f)[1][: -len(file_type) - 1] for f in glob(os.path.join(root_path, f"{prefix}*.{file_type}"))]


def most_recent_timestamp(root_path: str, file_type: str) -> float:
    """Returns the most recent timestamp from S3 or local storage with the suffix"""

    if root_path.startswith(S3_PREFIX):
        default = datetime(1970, 1, 1, tzinfo=timezone.utc)

        def _get_timestamps() -> Generator[datetime, Any, None]:
            """Get the max timestamp on each page in the paginator"""
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
    """Get a signed URL for a file on S3"""

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
    """File handler for S3, local storage, or HTTPS (read-only)"""

    _buffer: IO[Any]

    def __init__(self, path: str, mode: str = "rb", content_type: str | None = None):
        """
        Creates the class, emulating the file object.

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

        if self.is_https:
            if self.mode != "rb":
                raise NotImplementedError("Only 'rb' mode is supported for https:// paths")
            self._buffer = BytesIO()
        elif self.is_s3:
            self._buffer = BytesIO()
        else:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._buffer = open(  # pylint: disable=consider-using-with
                self.path, self.mode, encoding="UTF-8" if self.mode == "w" else None
            )

    def __enter__(self) -> BytesIO | IO:
        """Read from S3, HTTPS, or open the stream"""
        if self.is_https:
            with _get_response(self.path) as response:
                self._buffer = BytesIO(response.read())
            self._buffer.seek(0)

        if self.is_s3:
            bucket, key = get_bucket_key(self.path)
            if self.mode == "rb":
                # Download the file from S3 to the stream
                try:
                    s3_client.download_fileobj(Bucket=bucket, Key=key, Fileobj=self._buffer)
                except s3_client.exceptions.ClientError as exc:
                    raise FileNotFoundError(f"File {self.path} not found") from exc
                self._buffer.seek(0)

        return self._buffer

    def __exit__(self, *_: List[Any], **__: Dict[str, Any]) -> None:
        """Write to S3 or local storage and close the stream"""
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
        self._buffer.close()


@contextmanager
def GzipFileHandler(path: str, mode: str = "rb", encoding: str | None = None) -> Generator[Any, Any, None]:  # NOSONAR
    """
    Context manager for reading/writing gzip-compressed files from S3 or local storage,
    using BinaryFileHandler for the underlying file I/O.
    Supports binary ('rb', 'wb') and text ('rt', 'wt') modes.
    Usage:
        with GzipFileHandler(path, mode, encoding='utf-8') as f:
            f.read() / f.write(...)
    """
    if mode not in ("rb", "wb", "rt", "wt"):
        raise NotImplementedError(f"mode {mode} is not supported")
    file_mode = mode.replace("t", "b")
    with BinaryFileHandler(path, file_mode) as base_file:
        with gzip.open(base_file, mode, encoding=encoding) as gz_file:
            yield gz_file


def purge_folder(root_path: str, dry_run: bool = True, max_workers: int = 500) -> List[str]:
    """Delete all files within a folder/prefix on S3 or a local directory.

    For S3, root_path should be an S3 URL (s3://bucket/path/). Uses get_files to list objects
    under the prefix. For local paths, the function walks the directory tree recursively.
    If dry_run is True (default), no deletion is performed and the function returns the
    list of files that would be deleted.
    :param root_path: The S3 bucket/prefix or local directory to purge
    :param dry_run: If True, no files are deleted, and the function returns the list of files that would be deleted
    :param max_workers: The maximum number of worker threads to use for deletion

    Returns a sorted list of full paths of files deleted (or that would be deleted).
    """
    if root_path.startswith(S3_PREFIX):
        full_paths = sorted(f"{root_path.rstrip('/')}/{f}" for f in get_files(root_path))
    else:
        full_paths = sorted(os.path.join(os.path.normpath(root_path), f) for f in get_files(root_path))

    if not dry_run:
        delete_files(full_paths, max_workers=max_workers)

        if not root_path.startswith(S3_PREFIX):
            # Clean up any subdirectories
            for dirpath, _, _ in os.walk(root_path, topdown=False):
                os.rmdir(dirpath)

    return full_paths
