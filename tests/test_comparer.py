"""
Test the file comparer

Copyright (c) 2023-2025 MX8 Inc
All rights reserved.

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

import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from mx8fs import ResultsComparer, read_file, write_file


class Response:
    def __init__(self, status_code: int, content: bytes = b"", json: Any = None) -> None:
        self.status_code = status_code
        self.content = content.decode("utf-8")
        self._json = json

    def json(self) -> Any:
        return self._json or json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content


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
    test_dict = {"any": "value", "nested": {"any": "nested_value"}}
    test_file = save_dict(test_dict)
    correct_file = save_dict(test_dict)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)

    differences = comparer.get_dict_differences(test_file, correct_file)

    assert differences == [], "Identical files should have no differences"
    assert repr(differences) == "[]", "Differences should be empty"


def test_compare_different_files(save_dict: Callable[[Any], str]) -> None:
    test_file = save_dict({"any": "value", "nested": {"any": "nested_value"}})
    correct_file = save_dict({"any": "different_value", "nested": {"any": "nested_value"}})

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)

    differences = comparer.get_dict_differences(test_file, correct_file)

    assert len(differences) == 1, "There should be one difference"
    assert "root/any" in differences.keys, "Difference should be in the root key"

    # Now test the same with ignore_keys
    comparer = ResultsComparer(ignore_keys=["any"], create_test_data=False)
    assert comparer.get_dict_differences(test_file, correct_file) == [], "Differences should be ignored"


def test_create_test_data(save_dict: Callable[[Any], str]) -> None:
    test_file = save_dict({"any": "value", "nested": {"any": "nested_value"}})
    correct_file = save_dict({"any": "different_value", "nested": {"any": "nested_value"}})

    comparer = ResultsComparer(ignore_keys=None, create_test_data=True)

    differences = comparer.get_dict_differences(test_file, correct_file)

    # Since create_test_data is True, the correct file should be overwritten
    assert differences == [], "Differences should be resolved by overwriting the correct file"

    assert read_file(correct_file) == read_file(test_file), "Correct file should match the modified test file"


def test_compare_nested_differences(save_dict: Callable[[Any], str]) -> None:
    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_dict_differences(
        save_dict({"any": "value", "nested": {"any": "nested_value"}}),
        save_dict({"any": "value", "nested": {"any": "different_nested_value"}}),
    )

    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested/any" in differences.keys, "Difference should be in the nested key"

    # Now compare with key names changing
    differences = comparer.get_dict_differences(
        save_dict({"any": "value", "nested": {"any": "nested_value"}}),
        save_dict({"any": "value", "nested": {"different_key": "nested_value"}}),
    )
    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested" in differences.keys, "Difference should be in the nested key"

    # Now compare with the difference in a list size
    differences = comparer.get_dict_differences(
        save_dict({"any": "value", "nested": {"any": ["nested_value", "nested_value2"]}}),
        save_dict({"any": "value", "nested": {"any": ["nested_value"]}}),
    )
    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested/any" in differences.keys, "Difference should be in the nested key"

    # Now compare with the difference in list values
    differences = comparer.get_dict_differences(
        save_dict({"any": "value", "nested": {"any": ["nested_value", "nested_value2"]}}),
        save_dict({"any": "value", "nested": {"any": ["nested_value", "nested_value3"]}}),
    )
    assert len(differences) == 1, "There should be one nested difference"
    assert "root/nested/any[1]" in differences.keys, "Difference should be in the nested key"

    # Now test ignore keys
    comparer = ResultsComparer(ignore_keys=["nested"], create_test_data=False)
    differences = comparer.get_dict_differences(
        save_dict({"any": "value", "nested": {"any": "nested_value"}}),
        save_dict({"any": "value", "nested": {"any": "different_nested_value"}}),
    )
    assert differences == [], "Differences should be ignored"


def test_api_response_differences(save_dict: Callable[[Any], str]) -> None:
    dict_value = {"any": "value", "nested": {"any": "nested_value"}}
    correct_file = save_dict(dict_value)
    response = Response(200, json=dict_value)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert not differences, "Identical API responses should have no differences"


def test_api_response_differences_with_changes(save_dict: Callable[[Any], str]) -> None:
    dict_value = {"any": "value", "nested": {"any": "nested_value"}}
    correct_file = save_dict(dict_value)

    dict_value["any"] = "different_value"
    response = Response(200, json=dict_value)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    differences = comparer.get_api_response_differences(response, correct_file)

    assert len(differences) == 1, "There should be one difference"
    assert "root/any" in differences.keys, "Difference should be in the root key"


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
        {"any": "value", "nested": {"any": "nested_value"}},
        {"any": "value", "nested": {"any": "nested_value"}},
    )
    assert not differences, "Identical dictionaries should have no differences"


def test_obfuscate_parameters_top_and_nested(save_dict: Callable[[Any], str]) -> None:
    # Top-level and nested sensitive keys
    test_dict = {
        "TWILIO_AUTH_TOKEN": "secret1",
        "nested": {
            "SQUARE_ACCESS_TOKEN": "secret2",
            "list": [{"TWILIO_TEST_AUTH_TOKEN": "secret3"}, {"other": "value"}],
        },
    }
    comparer = ResultsComparer(ignore_keys=None, create_test_data=True)
    test_file = save_dict(test_dict)
    correct_file = save_dict({})
    comparer.get_dict_differences(test_file, correct_file)
    obfuscated = json.loads(read_file(correct_file))
    from mx8fs.comparer import ResultsComparer as RC

    assert obfuscated["TWILIO_AUTH_TOKEN"].startswith("OBFUSCATED-")
    assert obfuscated["TWILIO_AUTH_TOKEN"] == RC._obfuscate_value("secret1")
    assert obfuscated["nested"]["SQUARE_ACCESS_TOKEN"] == RC._obfuscate_value("secret2")
    assert obfuscated["nested"]["list"][0]["TWILIO_TEST_AUTH_TOKEN"] == RC._obfuscate_value("secret3")
    assert obfuscated["nested"]["list"][1]["other"] == "value"


def test_obfuscate_parameters_ignored_in_diff(save_dict: Callable[[Any], str]) -> None:
    # Differences in sensitive keys should be ignored
    test_dict = {
        "TWILIO_AUTH_TOKEN": "secret1",
        "nested": {"SQUARE_ACCESS_TOKEN": "secret2"},
    }
    comparer = ResultsComparer(ignore_keys=None, create_test_data=True)
    test_file = save_dict(test_dict)
    correct_file = save_dict({})

    assert not comparer.get_dict_differences(test_file, correct_file)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    assert not comparer.get_dict_differences(test_file, correct_file)

    assert len(comparer.get_dict_differences(test_file, test_file)) == 2

    comparer = ResultsComparer(obfuscate_regex="twilio_auth", ignore_keys=None, create_test_data=False)
    differences = comparer.get_dict_differences(test_file, correct_file)
    assert len(differences) == 1, "There should be one difference"


def test_obfuscate_text_differences(save_text: Callable[[Any], str]) -> None:
    # Sensitive values in text files should be obfuscated to the end of the line
    test_content = (
        "TWILIO_AUTH_TOKEN: secret1\n"
        "SQUARE_ACCESS_TOKEN = secret2\n"
        "other: value\n"
        'TWILIO_TEST_AUTH_TOKEN: "secret3"\n'
        "not_sensitive: keepme\n"
    )
    comparer = ResultsComparer(ignore_keys=None, create_test_data=True)
    test_file = save_text(test_content)
    correct_file = save_text("")

    assert not comparer.get_text_differences(test_file, correct_file)

    comparer = ResultsComparer(ignore_keys=None, create_test_data=False)
    assert not comparer.get_text_differences(test_file, correct_file)

    differences = comparer.get_text_differences(test_file, test_file)

    assert differences.contains("- TWILIO_AUTH_TOKEN")
    assert differences.contains("- SQUARE_ACCESS_TOKEN")
    assert differences.contains("- TWILIO_TEST_AUTH_TOKEN")

    comparer = ResultsComparer(
        obfuscate_regex="twilio_auth",
        ignore_keys=None,
        create_test_data=False,
    )
    differences = comparer.get_text_differences(test_file, test_file)
    assert differences.contains("- TWILIO_AUTH_TOKEN")
    assert not differences.contains("- SQUARE_ACCESS_TOKEN")
    assert not differences.contains("- TWILIO_TEST_AUTH_TOKEN")
