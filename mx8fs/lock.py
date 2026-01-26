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

import logging
import string
from datetime import datetime, timedelta
from functools import cached_property
from random import choice
from time import sleep, time
from typing import Any

from mx8fs import delete_file, list_files, write_file

logger = logging.getLogger("mx8.lock")

TIME_FORMAT = "%Y%m%d%H%M%S"


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


class FileLock:
    """
    Implements locking using an additional file on the file system.

    For file systems that are only eventually consistent, use a longer
    wait_period to wait for consistency when multiple clients are reading at the same time.

    Locks a file across multiple process and clients using an additional file on the file system.

    The lock file has the following format:
    "{file}.{timestamp}.{random}.lock"

    where:

    * file - is the file being locked
    * timestamp - is the timestamp the lock was requested in the format %Y%m%d%H%M%S
    * random - is a random set of letters

    On creating the lock we

    1. Check to see if anyone already has a lock
    2. If they don't, attempt to create a lock and wait for wait_period.
    3. If two processes have attempted to get the lock then the one with the earlier lock gets it.
    4. When the lock is released the lock class deletes the lock and other processes can proceed

    Locks only last for maximum_age seconds, and any request will time out after time_out_seconds
    """

    def __init__(
        self,
        file: str,
        wait_period: float = 0.1,
        time_out_seconds: int = 840,  # 1 minute less than the lambda timeout
        maximum_age: int = 900,  # 15 minutes, the maximum time a lambda can run
    ):
        """
        Initialize the lock.

        Args:
            file: The file to lock.
            wait_period: The period to wait before confirming file lock.
            time_out_seconds: The time out to stop waiting after.
            maximum_age: The maximum age of lock files to respect.

        """
        self.file = file
        self.waiter = Waiter(wait_period, time_out_seconds)
        self.maximum_age = timedelta(seconds=maximum_age)

    @cached_property
    def _lock_file(self) -> str:
        """Get the lock file name."""
        timestamp = datetime.now().strftime(TIME_FORMAT)
        random_key = "".join(choice(string.ascii_lowercase) for _ in range(12))  # nosec - insecure random number

        return f"{self.file}.{timestamp}.{random_key}.lock"

    def __enter__(self) -> "FileLock":
        """Acquire the lock on the file. This will wait until the lock is available."""
        logger.debug("Getting lock on %s", self.file)

        # If the file is locked then wait for it to be unlocked
        self.waiter.start_timeout()
        while len(self._get_lock_files()) > 0:
            self.waiter.check_timeout()

        # create a lock file
        write_file(self._lock_file, "locked")

        # Check an wait in case another process is trying to get the lock
        self.waiter.wait()
        while (
            len(lock_files := self._get_lock_files()) > 1 and lock_files[0] != self._lock_file
        ):  # pragma: no cover - coverage is not collected for multi-process tests
            try:
                self.waiter.check_timeout()
            except TimeoutError as ex:
                delete_file(self._lock_file)
                raise ex

        logger.debug("Acquired lock on %s", self.file)
        return self

    def __exit__(self, *_: list[Any], **__: dict[str, Any]) -> None:
        """Release the lock on the file."""
        delete_file(self._lock_file)
        logger.debug("Released lock on %s", self.file)

    def _get_lock_files(self) -> list[str]:
        """Get all the lock files for the current file."""
        path = "/".join(self.file.split("/")[:-1])
        prefix = self.file.split("/")[-1]

        # Get all the lock files in the same directory
        files = [f"{path}/{file}.lock" for file in list_files(path, "lock", prefix)]

        # Return the sorted current lock files for the current file
        return sorted(file for file in files if self._lock_is_current(file))

    def _lock_is_current(self, lock_file: str) -> bool:
        """Check if the lock file is current."""
        # If the lock file is not for the current file then it is not current
        if self.file not in lock_file:
            return False

        # If we cannot parse the timestamp then it is not current
        try:
            timestamp = datetime.strptime(lock_file.split(".")[-3], TIME_FORMAT)
        except (IndexError, ValueError):
            return False

        # If we are less than the maximum age then it is current
        return datetime.now() < timestamp + self.maximum_age
