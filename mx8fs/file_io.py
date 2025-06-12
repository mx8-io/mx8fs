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
from contextlib import contextmanager
from datetime import datetime
from glob import glob
from io import BytesIO
from shutil import copyfile
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


def copy_file(src: str, dst: str) -> None:
    """Copy a file from S3 or local storage"""
    if src.startswith(S3_PREFIX):
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
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        copyfile(src, dst)


def move_file(src: str, dst: str) -> None:
    """Move a file from S3 or local storage"""
    copy_file(src, dst)
    delete_file(src)


def list_files(root_path: str, file_type: str, prefix: str = "") -> List[str]:
    """Returns a list of files from S3 or local storage with the relevant suffix and optional prefix.

    The prefix signficantly improves performance for S3 by reducing the number of objects listed.
    """
    if root_path.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(root_path)
        key = key + "/" if not key.endswith("/") else key

        paginator = s3_client.get_paginator("list_objects_v2")
        files = []

        for page in paginator.paginate(Bucket=bucket, Prefix=key + prefix, PaginationConfig={"PageSize": 10_000}):
            if "Contents" in page:
                files.extend(
                    [
                        obj["Key"].removeprefix(key).removesuffix(f".{file_type}")
                        for obj in page["Contents"]
                        if obj["Key"].endswith(file_type)
                    ]
                )

        return files
    return [os.path.split(f)[1][: -len(file_type) - 1] for f in glob(os.path.join(root_path, f"{prefix}*.{file_type}"))]


def most_recent_timestamp(root_path: str, file_type: str) -> float:
    """Returns the most recent timestamp from S3 or local storage with the suffix"""
    if root_path.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(root_path)
        boto_response = s3_client.list_objects_v2(Bucket=bucket, Prefix=key, Delimiter="/")
        if "Contents" not in boto_response:
            return 0

        return max(
            [obj["LastModified"] for obj in boto_response["Contents"] if obj["Key"].endswith(file_type)],
            default=datetime(1970, 1, 1),
        ).timestamp()

    return max(
        [os.path.getmtime(f) for f in glob(os.path.join(root_path, f"*.{file_type}"))],
        default=0,
    )


def get_public_url(file: str, expires_in: int = 3600) -> str:
    """Get a signed URL for a file on S3"""

    if file.startswith(S3_PREFIX):
        bucket, key = get_bucket_key(file)
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod="get_object",
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
