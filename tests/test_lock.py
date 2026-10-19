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

import json
import multiprocessing
import os
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from queue import Queue
from random import random
from threading import Event
from time import sleep, time
from types import FrameType
from typing import Any
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from filelock import FileLock as LocalFileLock

import mx8fs.lock as lock_module
from mx8fs import FileLock, Waiter, delete_file, read_file, write_file
from mx8fs.file_io import get_bucket_key, s3_client

# pylint: disable=protected-access


@pytest.fixture
def s3_requests() -> Generator[Stubber, None, None]:
    with Stubber(s3_client) as requests:
        yield requests
        requests.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("status", "code"), [(409, "ConditionalRequestConflict"), (412, "PreconditionFailed"), (404, "NoSuchKey")]
)
def test_s3_acquisition_retries_storage_contention(s3_requests: Stubber, status: int, code: str) -> None:
    params = {"Bucket": "bucket", "Key": "resource.lock"}
    put_params = {**params, "Body": ANY, "IfNoneMatch": "*"}
    s3_requests.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=params)
    s3_requests.add_client_error("put_object", code, http_status_code=status, expected_params=put_params)
    s3_requests.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=params)
    s3_requests.add_response("put_object", {"ETag": '"acquired-version"'}, put_params)
    s3_requests.add_response("delete_object", {}, {**params, "IfMatch": '"acquired-version"'})

    with FileLock("s3://bucket/resource", wait_period=0, time_out_seconds=1):
        pass


def test_s3_acquisition_recovers_its_already_written_claim(
    s3_requests: Stubber, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim_id = UUID(int=1)
    monkeypatch.setattr(lock_module, "uuid4", lambda: claim_id)
    contents = json.dumps({"owner": claim_id.hex, "created_at": time()}).encode()
    params = {"Bucket": "bucket", "Key": "resource.lock"}
    s3_requests.add_response(
        "get_object",
        {"Body": StreamingBody(BytesIO(contents), len(contents)), "ETag": '"recovered-version"'},
        params,
    )
    s3_requests.add_response("delete_object", {}, {**params, "IfMatch": "recovered-version"})

    with FileLock("s3://bucket/resource", wait_period=0, time_out_seconds=1):
        pass


def test_s3_acquisition_propagates_unexpected_storage_errors(s3_requests: Stubber) -> None:
    params = {"Bucket": "bucket", "Key": "resource.lock"}
    s3_requests.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=params)
    s3_requests.add_client_error(
        "put_object", "AccessDenied", http_status_code=403, expected_params={**params, "Body": ANY, "IfNoneMatch": "*"}
    )

    with pytest.raises(ClientError, match="AccessDenied"):
        with FileLock("s3://bucket/resource", wait_period=0, time_out_seconds=1):
            pytest.fail("A rejected write was treated as an acquired claim")


def test_s3_release_retries_with_the_acquired_version(s3_requests: Stubber) -> None:
    params = {"Bucket": "bucket", "Key": "resource.lock"}
    delete_params = {**params, "IfMatch": '"acquired-version"'}
    s3_requests.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=params)
    s3_requests.add_response("put_object", {"ETag": '"acquired-version"'}, {**params, "Body": ANY, "IfNoneMatch": "*"})
    s3_requests.add_client_error(
        "delete_object", "ConditionalRequestConflict", http_status_code=409, expected_params=delete_params
    )
    s3_requests.add_response("delete_object", {}, delete_params)

    with FileLock("s3://bucket/resource", wait_period=0, time_out_seconds=1):
        pass


def test_s3_release_propagates_unexpected_storage_errors(s3_requests: Stubber) -> None:
    params = {"Bucket": "bucket", "Key": "resource.lock"}
    s3_requests.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=params)
    s3_requests.add_response("put_object", {"ETag": '"acquired-version"'}, {**params, "Body": ANY, "IfNoneMatch": "*"})
    s3_requests.add_client_error(
        "delete_object",
        "AccessDenied",
        http_status_code=403,
        expected_params={**params, "IfMatch": '"acquired-version"'},
    )

    with pytest.raises(ClientError, match="AccessDenied"):
        with FileLock("s3://bucket/resource", wait_period=0, time_out_seconds=1):
            pass


@pytest.mark.parametrize("expired", [False, True], ids=["creation", "takeover"])
def test_s3_delayed_contender_cannot_displace_active_claim(expired: bool) -> None:
    """A contender paused before its PUT must respect a claim acquired meanwhile."""
    file = f"s3://mx8-test-bucket/mx8fs/lock-regression/{uuid4().hex}/resource"
    _, key = get_bucket_key(file)
    maximum_age = 1 if expired else 900
    contender = FileLock(file, time_out_seconds=10, maximum_age=maximum_age)
    paused = Event()
    resume = Event()
    release = Event()
    resolution: Queue[str] = Queue()

    def contend() -> None:
        did_pause = False
        reported_wait = False

        def trace(frame: FrameType, event: str, _: Any) -> Any:
            nonlocal did_pause, reported_wait
            if (
                not did_pause
                and event == "call"
                and frame.f_code.co_name == "_make_api_call"
                and frame.f_locals.get("operation_name") == "PutObject"
                and frame.f_locals["api_params"]["Key"].startswith(f"{key}.")
            ):
                did_pause = True
                paused.set()
                assert resume.wait(20), "The controller did not resume the contender"
            if (
                did_pause
                and resume.is_set()
                and not reported_wait
                and event == "call"
                and frame.f_code.co_name == "check_timeout"
                and frame.f_locals.get("self") is contender.waiter
            ):
                reported_wait = True
                resolution.put("waiting")
            return trace

        sys.settrace(trace)
        try:
            with contender:
                sys.settrace(None)
                resolution.put("acquired")
                assert release.wait(20), "The controller did not release the contender"
        finally:
            sys.settrace(None)

    with ExitStack() as stack:
        if expired:
            original = FileLock(file)
            original.__enter__()
            stack.callback(original.__exit__)
            sleep(1.05)
        executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        future = executor.submit(contend)
        try:
            assert paused.wait(10), "The contender did not reach its storage write"
            # Make the original implementation's delayed filename strictly earlier.
            sleep(1.01)
            with FileLock(file, time_out_seconds=10, maximum_age=maximum_age):
                resume.set()
                assert resolution.get(timeout=10) == "waiting", "A late contender displaced the active claim"
            release.set()
            future.result(timeout=10)
        finally:
            resume.set()
            release.set()


@pytest.fixture(params=["local", "s3"])
def lock_path(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[str, None, None]:
    file = (
        f"s3://mx8-test-bucket/mx8fs/lock-regression/{uuid4().hex}/resource"
        if request.param == "s3"
        else str(tmp_path / "resource")
    )
    try:
        yield file
    finally:
        delete_file(f"{file}.lock")


def test_expired_claim_cannot_release_successor(lock_path: str) -> None:
    with FileLock(lock_path) as expired:
        sleep(1.05)
        # Preserve maximum_age as the contender's policy, regardless of the holder's setting.
        with FileLock(lock_path, maximum_age=1):
            expired.__exit__()
            with pytest.raises(TimeoutError):
                with FileLock(lock_path, wait_period=0.05, time_out_seconds=1):
                    pytest.fail("An expired holder deleted its successor's lock")


def test_exception_releases_lock(lock_path: str) -> None:
    with pytest.raises(ValueError, match="critical section failed"):
        with FileLock(lock_path):
            raise ValueError("critical section failed")
    with FileLock(lock_path, time_out_seconds=1):
        assert read_file(f"{lock_path}.lock")


def test_lock_instance_can_be_reused(lock_path: str) -> None:
    lock = FileLock(lock_path)
    with lock:
        first_claim_id = json.loads(read_file(f"{lock_path}.lock"))["owner"]
    with lock:
        second_claim_id = json.loads(read_file(f"{lock_path}.lock"))["owner"]
    assert first_claim_id != second_claim_id


def test_failed_nested_acquisition_preserves_outer_claim(lock_path: str) -> None:
    lock = FileLock(lock_path, wait_period=0.05, time_out_seconds=1)
    with lock:
        with pytest.raises(TimeoutError):
            with lock:
                pytest.fail("The same instance unexpectedly acquired its live lock twice")
    with FileLock(lock_path, time_out_seconds=1):
        pass


@pytest.mark.parametrize("contents", ["invalid json", '{"owner":"owner","created_at":"invalid"}'])
def test_invalid_lock_record_fails_closed(lock_path: str, contents: str) -> None:
    write_file(f"{lock_path}.lock", contents)
    with pytest.raises(ValueError):
        with FileLock(lock_path, time_out_seconds=1):
            pytest.fail("An invalid record was treated as an available lock")
    assert read_file(f"{lock_path}.lock") == contents


def test_local_claim_is_atomic_when_contender_pauses(tmp_path: Path) -> None:
    """A pause after reading an absent record must not open the local claim transaction."""
    file = str(tmp_path / "resource")
    contender = FileLock(file, time_out_seconds=10)
    paused = Event()
    resume = Event()

    def contend() -> None:
        did_pause = False

        def trace(frame: FrameType, event: str, result: Any) -> Any:
            nonlocal did_pause
            if (
                not did_pause
                and event == "return"
                and frame.f_code.co_name == "_read_lock"
                and frame.f_locals.get("self") is contender._backend
                and result is None
            ):
                did_pause = True
                paused.set()
                assert resume.wait(10), "The controller did not resume the contender"
            return trace

        sys.settrace(trace)
        try:
            with contender:
                pass
        finally:
            sys.settrace(None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(contend)
        try:
            assert paused.wait(10), "The contender did not read the absent record"
            with pytest.raises(TimeoutError):
                with FileLock(file, wait_period=0.01, time_out_seconds=1):
                    pytest.fail("Another contender entered during the paused claim transaction")
        finally:
            resume.set()
        future.result(timeout=10)
    with FileLock(file, time_out_seconds=1):
        pass


def test_local_release_preserves_claim_while_guard_is_busy(tmp_path: Path) -> None:
    file = str(tmp_path / "resource")
    with FileLock(file, wait_period=0, time_out_seconds=0) as lock:
        claim = read_file(f"{file}.lock")
        with LocalFileLock(f"{file}.lock.guard"):
            with pytest.raises(TimeoutError):
                lock.__exit__()
            assert read_file(f"{file}.lock") == claim
    assert not Path(f"{file}.lock").exists()
    with FileLock(file, time_out_seconds=1):
        pass


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
    test_str = str(random())
    with FileLock(file, wait_period=0.1, time_out_seconds=20, maximum_age=30):
        write_file(file, test_str)

        assert read_file(file) == test_str

        sleep(0.1)
        assert read_file(file) == test_str

    return True


class TestFileLock:
    """Base class for lock tests."""

    def test_lock_timeout(self, tmpdir: Path) -> None:
        """Test the lock timeout."""
        file = os.path.join(tmpdir, f"test_lock_timeout.{random()}tmp")
        with FileLock(file, wait_period=1, time_out_seconds=5, maximum_age=20):
            sleep(0.1)
            with pytest.raises(TimeoutError):
                with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=20):
                    raise AssertionError("File lock should have timed out")

    def test_basic(self, tmpdir: Path) -> None:
        """ "Test the basic functionality of the lock."""
        file = os.path.join(tmpdir, f"test_basic.{random()}.tmp")
        check_lock(file)
        check_lock(file)

    def test_lock_ages(self, tmpdir: Path) -> None:
        """Test the lock over a longer time."""
        file = os.path.join(tmpdir, f"test_lock_ages.{random()}.tmp")

        with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=10):

            with pytest.raises(TimeoutError):
                with FileLock(file, wait_period=1, time_out_seconds=2, maximum_age=10):
                    raise AssertionError("File lock should have timed out")

    def test_lock_multiprocess(self, tmpdir: Path) -> None:
        """Test the lock in a multiprocess environment."""
        file = os.path.join(tmpdir, f"test_lock_multiprocess.{random()}.tmp")

        processes = []
        pool = multiprocessing.Pool()
        for _ in range(0, min(10, multiprocessing.cpu_count() - 1)):
            processes.append(pool.apply_async(check_lock, (file,)))

        pool.close()
        pool.join()

        assert all(x.get() for x in processes)
