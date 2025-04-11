"""
Cache tests

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

from pathlib import Path
from random import random
from time import sleep

from mx8fs import cache_to_disk, cache_to_disk_binary


def test_cache(tmp_path: Path) -> None:
    """Test the cache decorator"""

    @cache_to_disk(str(tmp_path))
    def random_string() -> str:
        return str(random())

    test_n = random_string()
    test_n2 = random_string()

    assert test_n == test_n2


def test_cache_expiry(tmp_path: Path) -> None:
    """Test the cache decorator with an expiration"""

    @cache_to_disk(str(tmp_path), expiration_seconds=1)
    def random_string() -> str:
        return str(random())

    test_n = random_string()
    test_n2 = random_string()
    sleep(2)
    test_n3 = random_string()

    assert test_n == test_n2
    assert test_n2 != test_n3


def test_cache_binary(tmp_path: Path) -> None:
    """Test the cache decorator"""

    @cache_to_disk_binary(str(tmp_path))
    def random_string() -> str:
        return str(random())

    test_n = random_string()
    test_n2 = random_string()

    assert test_n == test_n2


def test_cache_log(tmp_path: Path) -> None:
    """Test the cache decorator"""

    @cache_to_disk(str(tmp_path), log_group="test")
    def random_string() -> str:
        return str(random())

    test_n = random_string()
    test_n2 = random_string()

    assert test_n == test_n2


def test_cache_log_not_json_serializable(tmp_path: Path) -> None:
    """Test the cache decorator"""

    @cache_to_disk_binary(str(tmp_path), log_group="test")
    def random_float() -> float:
        return random()

    test_n = random_float()
    test_n2 = random_float()

    assert test_n == test_n2


def test_cache_ignore_args(tmp_path: Path) -> None:
    """Test the cache decorator"""

    @cache_to_disk(str(tmp_path), ignore_kwargs=["second"])
    def random_string(second: str) -> str:
        return str(random()) + second

    test_n = random_string(second="test")
    test_n2 = random_string(second="test2")

    assert test_n == test_n2
