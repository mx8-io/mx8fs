"""
Locks a file across multiple process and clients.

Based on DoggoLock (https://bitbucket.org/deductive/newtools/src/master/newtools/doggo/lock.py)

Copyright (c) 2012-2025, Deductive Limited
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
    this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
    this list of conditions and the following disclaimer in the documentation
    and/or other materials provided with the distribution.
    * Neither the name of the Deductive Limited nor the names of
    its contributors may be used to endorse or promote products derived from
    this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

This version

Copyright (c) 2024-2025 MX8 Inc, all rights reserved.

"""

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
from time import sleep, time
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError
from filelock import FileLock as LocalFileLock
from filelock import Timeout as LocalLockTimeout

from .file_io import _atomic_write_bytes, get_bucket_key, read_file_with_version, s3_client

logger = logging.getLogger("mx8.lock")

TIME_FORMAT = "%Y%m%d%H%M%S"


@dataclass(frozen=True)
class _LockRecord:
    """One claim record together with the storage version returned by that read."""

    claim_id: str
    created_at: float
    version: str


class Waiter:
    """
    Provide generic wait and timeout behavior.

    Raises TimeoutError() if the task doesn't occur in the period.

    ```python

        waiter = Waiter()
        waiter.start_timeout()
        while not check_my_condition():
            waiter.check_timeout()
    ```
    """

    def __init__(self, wait_period: float, time_out_seconds: float):
        """
        Initialize the waiter.

        Args:
            wait_period: The period to wait for between iterations.
            time_out_seconds: The time after which a TimeoutError is raised.

        """
        self.time_out_seconds = time_out_seconds
        self.wait_period = wait_period
        self._timeout: float | None = None

    def __enter__(self) -> "Waiter":
        """Start the timer."""
        self.start_timeout()
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop the timer."""
        self._timeout = None

    def wait(self, number_times: int = 1) -> None:
        """Wait for the defined period."""
        sleep(self.wait_period * number_times)

    def start_timeout(self) -> None:
        """Start a timeout."""
        self._timeout = time() + self.time_out_seconds

    def timed_out(self) -> bool:
        """
        Check whether the timer has timed out.

        Returns:
            True if the timer has timed out; otherwise False.

        """
        if self._timeout is not None:
            return time() > self._timeout
        else:
            raise ValueError("Someone has tried to call timed_out() before calling start_timeout()")

    def check_timeout(self) -> None:
        """Wait and raise an exception if the timer has timed out."""
        self.wait()
        if self.timed_out():
            raise TimeoutError("Timed out waiting for completion")


@dataclass(frozen=True)
class _LockClaim:
    """Identify one successful acquisition and, on S3, its ETag."""

    claim_id: str
    etag: str | None = None


class BaseLock(ABC):
    """Share polling, claim lifecycle and record encoding between backends."""

    def __init__(self, file: str):
        """Set the shared record path and begin without an acquired claim."""
        self._lock_file = f"{file}.lock"
        self._claim: _LockClaim | None = None

    def acquire(self, waiter: Waiter, maximum_age: timedelta) -> None:
        """Keep trying until storage accepts the claim or the wait times out."""
        claim_id = uuid4().hex
        waiter.start_timeout()
        while (claim := self._try_acquire(claim_id, maximum_age)) is None:
            waiter.check_timeout()
        # A failed acquisition leaves any previously held claim intact.
        self._claim = claim

    def release(self, waiter: Waiter) -> None:
        """Release the acquired claim, or finish if it has already been replaced."""
        waiter.start_timeout()
        claim = self._claim
        if claim is None:
            return
        while not self._try_release(claim):
            waiter.check_timeout()
        self._claim = None

    @abstractmethod
    def _try_acquire(self, claim_id: str, maximum_age: timedelta) -> _LockClaim | None:
        """Return a successful atomic claim, or None when another claim wins."""

    @abstractmethod
    def _try_release(self, claim: _LockClaim) -> bool:
        """Return True when this claim is removed, absent or superseded."""

    def _read_lock(self) -> _LockRecord | None:
        """Read the claim record and its storage version together."""
        try:
            contents, version = read_file_with_version(self._lock_file)
        except FileNotFoundError:
            return None
        record = json.loads(contents)
        # Retain the stored "owner" field for compatibility; it identifies a claim, not a user.
        claim_id, created_at = record["owner"], record["created_at"]
        if not isinstance(claim_id, str) or not isinstance(created_at, (int, float)) or not isfinite(created_at):
            raise ValueError(f"Invalid lock record: {self._lock_file}")
        return _LockRecord(claim_id, created_at, version)

    @staticmethod
    def _is_unexpired(record: _LockRecord, maximum_age: timedelta) -> bool:
        """Apply the contender's maximum-age policy."""
        return time() < record.created_at + maximum_age.total_seconds()

    @staticmethod
    def _encode_claim_record(claim_id: str) -> bytes:
        """Keep the claim UUID in the content so successive S3 claims have distinct ETags."""
        return json.dumps({"owner": claim_id, "created_at": time()}).encode("utf-8")


class _LocalLock(BaseLock):
    """Protect each claim-record read/check/change with a short OS-backed guard."""

    def __init__(self, file: str):
        super().__init__(file)
        # Keep the guard separate: replacing the claim record must not replace the guarded inode.
        self._guard = LocalFileLock(f"{self._lock_file}.guard")

    def _try_acquire(self, claim_id: str, maximum_age: timedelta) -> _LockClaim | None:
        try:
            with self._guard.acquire(timeout=0):
                current = self._read_lock()
                if current is not None and self._is_unexpired(current, maximum_age):
                    return None
                _atomic_write_bytes(self._lock_file, self._encode_claim_record(claim_id))
                return _LockClaim(claim_id=claim_id)
        except LocalLockTimeout:
            return None

    def _try_release(self, claim: _LockClaim) -> bool:
        try:
            with self._guard.acquire(timeout=0):
                current = self._read_lock()
                if current is not None and current.claim_id == claim.claim_id:
                    os.remove(self._lock_file)
                return True
        except LocalLockTimeout:
            return False


class _S3Lock(BaseLock):
    """Let S3 conditional PUT and DELETE operations arbitrate claims on one fixed key."""

    def __init__(self, file: str):
        super().__init__(file)
        self._bucket, self._key = get_bucket_key(self._lock_file)

    def _try_acquire(self, claim_id: str, maximum_age: timedelta) -> _LockClaim | None:
        current = self._read_lock()
        if current is not None and self._is_unexpired(current, maximum_age):
            if current.claim_id == claim_id:
                # A retried PUT may report contention after its first attempt succeeded.
                return _LockClaim(claim_id=claim_id, etag=current.version)
            return None
        try:
            if current is None:
                # Create only if the key is still absent when S3 commits the PUT.
                response = s3_client.put_object(
                    Bucket=self._bucket,
                    Key=self._key,
                    Body=self._encode_claim_record(claim_id),
                    IfNoneMatch="*",
                )
            else:
                # Replace this expired version only; any newer claim makes the condition fail.
                response = s3_client.put_object(
                    Bucket=self._bucket,
                    Key=self._key,
                    Body=self._encode_claim_record(claim_id),
                    IfMatch=current.version,
                )
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] in {409, 412}:
                return None
            if error.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return _LockClaim(claim_id=claim_id, etag=response["ETag"])

    def _try_release(self, claim: _LockClaim) -> bool:
        assert claim.etag is not None
        try:
            # Retain our acquired ETag; a fresh read could authorize deleting a successor.
            s3_client.delete_object(Bucket=self._bucket, Key=self._key, IfMatch=claim.etag)
        except ClientError as error:
            status = error.response["ResponseMetadata"]["HTTPStatusCode"]
            if status == 409:
                return False
            if status != 412 and error.response["Error"]["Code"] != "NoSuchKey":
                raise
        return True


class FileLock:
    """
    Acquire an expiring local or S3 lock through a backend selected at construction.

    The constructor and context-manager API are shared by both backends.
    Each contender respects its own maximum_age. Expiry permits takeover without
    stopping the old holder; release cannot remove a successor's claim.
    All participating clients must use this claim-record protocol.
    """

    def __init__(
        self,
        file: str,
        wait_period: float = 0.1,
        time_out_seconds: int = 840,
        maximum_age: int = 900,
    ):
        """Select the storage backend and preserve the existing polling and expiry options."""
        self.file = file
        self.waiter = Waiter(wait_period, time_out_seconds)
        self.maximum_age = timedelta(seconds=maximum_age)
        self._backend: BaseLock = _S3Lock(file) if file.startswith("s3://") else _LocalLock(file)

    def __enter__(self) -> "FileLock":
        """Wait for the selected backend to acquire the lock."""
        logger.debug("Getting lock on %s", self.file)
        self._backend.acquire(self.waiter, self.maximum_age)
        logger.debug("Acquired lock on %s", self.file)
        return self

    def __exit__(self, *_: list[Any], **__: dict[str, Any]) -> None:
        """Ask the same backend to release its acquired claim."""
        self._backend.release(self.waiter)
        logger.debug("Released lock on %s", self.file)
