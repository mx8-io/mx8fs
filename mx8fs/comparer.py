"""
Support functions for testing

Copyright (c) 2023-2025 MX8 Inc, all rights reserved.

This software is confidential and proprietary information of MX8.
You shall not disclose such Confidential Information and shall use it only
in accordance with the terms of the agreement you entered into with MX8.
"""

import json
import os
import shutil
from difflib import ndiff
from logging import getLogger
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional


from mx8fs import read_file, write_file

logger = getLogger("mx8.comparer")


def get_diff(a: str, b: str) -> str:
    return "\n".join(d for d in ndiff(a.splitlines(), b.splitlines()) if not d.startswith(" "))


class Differences:
    def __init__(self) -> None:
        self._differences: List[Dict[str, str]] = []

    def __repr__(self) -> str:
        return json.dumps(self._differences, indent=4)

    def __eq__(self, value: object) -> bool:
        return self._differences == value

    def __bool__(self) -> bool:
        return bool(self._differences)

    def __len__(self) -> int:
        return len(self._differences)

    def append(self, differences: Dict[str, str]) -> None:
        self._differences.append(differences)

    def clear(self) -> None:
        self._differences.clear()

    @property
    def keys(self) -> List[str]:
        return [list(d.keys())[0] for d in self._differences]


class ResultsComparer:
    def __init__(
        self,
        ignore_keys: Optional[List[str]],
        create_test_data: bool = False,
    ) -> None:
        self._ignore_keys = ignore_keys if ignore_keys else []
        self._create_test_data = create_test_data
        self._differences = Differences()

    def _log_differences(self, key: str, correct: str, test: str) -> None:
        """Log the differences between two strings"""
        if correct != test:
            self._differences.append({key: get_diff(correct, test)})

    def _compare_dicts(self, correct: Any, test: Any, recursive: bool = False, root_key: str = "root") -> None:
        """
        Compare two dictionaries recursively, ignoring elements with the given key
        """

        if not recursive:
            logger.debug(
                {
                    "message": "Comparing dictionaries",
                    "dict1": correct,
                    "dict2": test,
                    "ignore_keys": self._ignore_keys,
                }
            )

        if isinstance(correct, list) and isinstance(test, list):
            if len(correct) != len(test):
                self._log_differences(root_key, json.dumps(correct), json.dumps(test))
                return

            for i, the_dict in enumerate(correct):
                self._compare_dicts(the_dict, test[i], recursive=True, root_key=f"{root_key}[{i}]")

            return

        # Check if both inputs are dictionaries
        if not isinstance(correct, dict) or not isinstance(test, dict):
            self._log_differences(root_key, json.dumps(correct), json.dumps(test))
            return

        # Get the set of keys for each dictionary
        correct_keys = set(correct.keys())
        test_keys = set(test.keys())

        # Check if the keys are the same
        if correct_keys != test_keys:
            self._log_differences(root_key, json.dumps(correct), json.dumps(test))
        else:
            # Recursively compare the values for each key
            for key in correct_keys:
                if key not in self._ignore_keys:
                    self._compare_dicts(correct[key], test[key], recursive=True, root_key=f"{root_key}/{key}")

    def compare_dicts(self, correct: Any, test: Any) -> Differences:
        """Compare two dictionaries"""
        self._compare_dicts(correct, test)
        return self._differences

    def get_text_differences(self, test: str, correct: str) -> Differences:
        """Compare a test file with a correct file"""
        if self._create_test_data:
            # make the directory if it doesn't exist
            os.makedirs(os.path.dirname(correct), exist_ok=True)

            # copy the test file to the correct file
            shutil.copyfile(test, correct)

        differences = Differences()

        if diff := get_diff(read_file(correct), read_file(test)):
            differences.append({"file": diff})

        return differences

    def get_dict_differences(self, test: str, correct: str) -> Differences:
        """Compare a test file with a correct file"""

        # Load the test file
        test_dict = json.loads(read_file(test))

        self._differences.clear()
        if self._create_test_data:
            try:
                correct_dict = json.loads(read_file(correct))
                self._compare_dicts(correct_dict, test_dict)
                assert self._differences == [], "The files should be identical"
            except (FileNotFoundError, AssertionError):
                # Save the test file as the correct file
                os.makedirs(os.path.dirname(correct), exist_ok=True)
                write_file(correct, json.dumps(test_dict, indent=4, ensure_ascii=False).strip())
                self._differences.clear()
        else:
            correct_dict = json.loads(read_file(correct))
            self._compare_dicts(correct_dict, test_dict)

        return self._differences

    def get_api_response_differences(
        self,
        response: Any,
        correct_file: str,
    ) -> Differences:
        """Check the response from the reporting API and return the differences"""

        file_name = os.path.basename(correct_file)

        # Write the response to a temporary file
        try:
            result = json.dumps(response.json(), indent=4, ensure_ascii=False)
            compare_func = self.get_dict_differences
        except json.JSONDecodeError:
            compare_func = self.get_text_differences
            result = response.text

        with NamedTemporaryFile(mode="wt", delete=False, prefix=file_name) as temp_file:
            temp_file.write(result.strip())
            temp_file.flush()
            temp_file_name = temp_file.name

        # Compare the response to the correct file
        mismatches = compare_func(temp_file_name, correct_file)

        # Clean up the temporary file
        os.remove(temp_file_name)

        return mismatches
