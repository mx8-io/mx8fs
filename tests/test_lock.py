"""
Lock file tests

Based on FileLock (https://bitbucket.org/deductive/newtools/src/master/newtools/tests/test_doggo.py)

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

Copyright (c) 2024 MX8 Inc, all rights reserved.

"""

import multiprocessing
import os
from pathlib import Path
from random import random
from time import sleep

import pytest

from mx8fs import FileLock, Waiter, read_file, write_file

# pylint: disable=protected-access


class TestWaiter:
    """Test the Waiter class."""

    def test_basic(self) -> None:
        """Test the basic functionality of the Waiter."""
        waiter = Waiter(wait_period=0.1, time_out_seconds=1)

        with pytest.raises(ValueError):
            waiter.timed_out()

        waiter.start_timeout()
        assert waiter.timed_out() is False
        waiter.check_timeout()
        waiter.wait(11)
        with pytest.raises(TimeoutError):
            waiter.check_timeout()

    def test_context_manager(self) -> None:

        with Waiter(wait_period=0.1, time_out_seconds=1) as waiter:
            assert waiter.timed_out() is False
            waiter.check_timeout()
            waiter.wait(11)
            with pytest.raises(TimeoutError):
                waiter.check_timeout()


def check_lock(file: str) -> bool:
    """Check the lock."""
    test_str = str(random())  # NOSONAR
    with FileLock(file, wait_period=0.1, time_out_seconds=20, maximum_age=30):
        write_file(file, test_str)

        assert read_file(file) == test_str

        sleep(0.1)

    return True


class TestFileLock:
    """Base class for lock tests."""

    def test_lock_timeout(self, tmpdir: Path) -> None:
        """Test the lock timeout."""
        file = os.path.join(tmpdir, f"test_lock_timeout.{random()}tmp")  # NOSONAR
        with FileLock(file, wait_period=1, time_out_seconds=5, maximum_age=20):
            sleep(0.1)
            with pytest.raises(TimeoutError):
                with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=20):
                    assert False, "File lock should have timed out"

    def test_basic(self, tmpdir: Path) -> None:
        """ "Test the basic functionality of the lock."""
        file = os.path.join(tmpdir, f"test_basic.{random()}.tmp")  # NOSONAR
        check_lock(file)
        check_lock(file)

    def test_lock_ages(self, tmpdir: Path) -> None:
        """Test the lock over a longer time."""
        file = os.path.join(tmpdir, f"test_lock_ages.{random()}.tmp")  # NOSONAR

        with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=10):

            with pytest.raises(TimeoutError):
                with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=10):
                    assert False, "File lock should have timed out"

    def test_lock_multiprocess(self, tmpdir: Path) -> None:
        """Test the lock in a multiprocess environment."""
        file = os.path.join(tmpdir, f"test_lock_multiprocess.{random()}.tmp")  # NOSONAR

        processes = list()
        pool = multiprocessing.Pool()
        for _ in range(0, min(10, multiprocessing.cpu_count() - 1)):
            processes.append(pool.apply_async(check_lock, (file,)))

        pool.close()
        pool.join()

        assert all([x.get() for x in processes])

    def test_lock_bad_lock_filenames(self, tmpdir: Path) -> None:
        """Test the lock with bad lock filenames."""
        file = os.path.join(tmpdir, f"test_lock_bad_lock_filenames.{random()}.tmp")  # NOSONAR

        with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=10) as lock:
            assert lock._lock_is_current("not a lock file") is False
            assert lock._lock_is_current(f"{file}.lock") is False
            assert lock._lock_is_current(f"{file}.1231241.asbfe.lock") is False
