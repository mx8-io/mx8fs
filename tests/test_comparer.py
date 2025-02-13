"""
Test the file comparer

Copyright (c) 2023-2025 MX8 Inc
All rights reserved.

This software is confidential and proprietary information of MX8.
You shall not disclose such Confidential Information and shall use it only
in accordance with the terms of the agreement you entered into with MX8.
"""

import os
import json
from pathlib import Path
from typing import Any, Callable
import pytest
from httpx import Response
from mx8fs import ResultsComparer, write_file, read_file


@pytest.fixture(name="save_text")
def fixture_save_text(tmp_path: Path) -> Callable[[str], str]:

    ix = 0

    def save_text(data: str) -> str:
        nonlocal ix
        path = os.path.join(tmp_path, f"temp_{ix}.txt")
        ix = ix + 1
        write_file(path, data)

        return path

    return save_text


@pytest.fixture(name="save_dict")
def fixture_save_dict(save_text: Callable[[str], str]) -> Callable[[Any], str]:
    def save_dict(data: Any) -> str:
        return save_text(json.dumps(data, indent=4))

    return save_dict


def test_compare_identical_files(save_dict: Callable[[Any], str]) -> None:
    test_dict = {"key": "value", "nested": {"key": "nested_value"}}
    test_file = save_dict(test_dict)
    correct_file = save_dict(test_dict)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)

    differences = comparer.get_dict_differences(test_file, correct_file)

    assert differences == [], "Identical files should have no differences"
    assert repr(differences) == "[]", "Differences should be empty"


def test_compare_different_files(save_dict: Callable[[Any], str]) -> None:
    test_file = save_dict({"key": "value", "nested": {"key": "nested_value"}})
    correct_file = save_dict({"key": "different_value", "nested": {"key": "nested_value"}})

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)

    differences = comparer.get_dict_differences(test_file, correct_file)

    assert len(differences) == 1, "There should be one difference"
    assert "root/key" in differences.keys, "Difference should be in the root key"

    # Now test the same with ignore_keys
    comparer = ResultsComparer(ignore_keys=["key"], create_test_data=False)
    assert comparer.get_dict_differences(test_file, correct_file) == [], "Differences should be ignored"


def test_create_test_data(save_dict: Callable[[Any], str]) -> None:
    test_file = save_dict({"key": "value", "nested": {"key": "nested_value"}})
    correct_file = save_dict({"key": "different_value", "nested": {"key": "nested_value"}})

    comparer = ResultsComparer(ignore_keys=None, create_test_data=True)

    differences = comparer.get_dict_differences(test_file, correct_file)

    # Since create_test_data is True, the correct file should be overwritten
    assert differences == [], "Differences should be resolved by overwriting the correct file"

    assert read_file(correct_file) == read_file(test_file), "Correct file should match the modified test file"


def test_compare_nested_differences(save_dict: Callable[[Any], str]) -> None:
    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_dict_differences(
        save_dict({"key": "value", "nested": {"key": "nested_value"}}),
        save_dict({"key": "value", "nested": {"key": "different_nested_value"}}),
    )

    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested/key" in differences.keys, "Difference should be in the nested key"

    # Now compare with key names changing
    differences = comparer.get_dict_differences(
        save_dict({"key": "value", "nested": {"key": "nested_value"}}),
        save_dict({"key": "value", "nested": {"different_key": "nested_value"}}),
    )
    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested" in differences.keys, "Difference should be in the nested key"

    # Now compare with the difference in a list size
    differences = comparer.get_dict_differences(
        save_dict({"key": "value", "nested": {"key": ["nested_value", "nested_value2"]}}),
        save_dict({"key": "value", "nested": {"key": ["nested_value"]}}),
    )
    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested/key" in differences.keys, "Difference should be in the nested key"

    # Now compare with the difference in list values
    differences = comparer.get_dict_differences(
        save_dict({"key": "value", "nested": {"key": ["nested_value", "nested_value2"]}}),
        save_dict({"key": "value", "nested": {"key": ["nested_value", "nested_value3"]}}),
    )
    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested/key[1]" in differences.keys, "Difference should be in the nested key"

    # Now test ignore keys
    comparer = ResultsComparer(ignore_keys=["nested"], create_test_data=False)
    differences = comparer.get_dict_differences(
        save_dict({"key": "value", "nested": {"key": "nested_value"}}),
        save_dict({"key": "value", "nested": {"key": "different_nested_value"}}),
    )
    assert differences == [], "Differences should be ignored"


def test_api_response_differences(save_dict: Callable[[Any], str]) -> None:
    dict_value = {"key": "value", "nested": {"key": "nested_value"}}
    correct_file = save_dict(dict_value)
    response = Response(200, json=dict_value)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert not differences, "Identical API responses should have no differences"


def test_api_response_differences_with_changes(save_dict: Callable[[Any], str]) -> None:
    dict_value = {"key": "value", "nested": {"key": "nested_value"}}
    correct_file = save_dict(dict_value)

    dict_value["key"] = "different_value"
    response = Response(200, json=dict_value)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert len(differences) == 1, "There should be one difference"
    assert "root/key" in differences.keys, "Difference should be in the root key"


def test_api_response_text_differences(save_text: Callable[[Any], str]) -> None:
    correct_file = save_text("test")
    response = Response(200, content=b"test")

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert not differences, "Identical API responses should have no differences"


def test_api_response_text_differences_with_changes(save_text: Callable[[Any], str]) -> None:
    correct_file = save_text("test")
    response = Response(200, content=b"test2")

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert differences == [{"file": "- test\n+ test2\n?     +\n"}], "Text differences should be logged"


def test_api_text_differences_created(save_text: Callable[[Any], str]) -> None:
    correct_file = save_text("test")
    response = Response(200, content=b"test2")

    comparer = ResultsComparer(ignore_keys=None, create_test_data=True)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert not differences, "Differences should be resolved by overwriting the correct file"


def test_compare_dicts() -> None:
    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.compare_dicts(
        {"key": "value", "nested": {"key": "nested_value"}},
        {"key": "value", "nested": {"key": "nested_value"}},
    )
    assert not differences, "Identical dictionaries should have no differences"
