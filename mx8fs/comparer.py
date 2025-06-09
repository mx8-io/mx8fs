"""
Support functions for testing

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

import hashlib
import json
import os
import re
from difflib import ndiff
from logging import getLogger
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

from mx8fs.file_io import read_file, write_file

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

    def contains(self, key: str) -> bool:
        return any(key in v for d in self._differences for v in d.values())

    def append(self, differences: Dict[str, str]) -> None:
        self._differences.append(differences)

    def clear(self) -> None:
        self._differences.clear()

    @property
    def keys(self) -> List[str]:
        return [list(d.keys())[0] for d in self._differences]


class ResultsComparer:
    _DEFAULT_OBFUSCATE_REGEX = ".*(password|secret|token|key|auth|credentials|salt|signature|hash|webhook).*"

    def __init__(
        self,
        ignore_keys: Optional[List[str]],
        create_test_data: bool = False,
        obfuscate_regex: Optional[str] = None,
    ) -> None:
        self._regex_obfuscation = re.compile(
            obfuscate_regex if obfuscate_regex is not None else self._DEFAULT_OBFUSCATE_REGEX, re.IGNORECASE
        )
        self._ignore_keys = ignore_keys if ignore_keys else []
        self._create_test_data = create_test_data
        self._differences = Differences()

    @staticmethod
    def _obfuscate_value(value: str) -> str:
        h = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"OBFUSCATED-{h}"

    def _obfuscate_dict(self, d: Any) -> Any:
        if isinstance(d, dict):
            return {
                k: (self._obfuscate_value(str(v)) if self._regex_obfuscation.search(k) else self._obfuscate_dict(v))
                for k, v in d.items()
            }
        elif isinstance(d, list):
            return [self._obfuscate_dict(item) for item in d]
        else:
            return d

    def _obfuscate_text(self, text: str) -> str:
        # For each sensitive key, obfuscate its value to the end of the line
        def obfuscate_line(line: str) -> str:
            if self._regex_obfuscation.search(line):
                # If the line contains a sensitive key, obfuscate it
                return self._obfuscate_value(line)

            return line

        return "\n".join(obfuscate_line(line) for line in text.splitlines())

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
        self._differences.clear()
        self._compare_dicts(correct, test)
        return self._differences

    def get_text_differences(self, test: str, correct: str) -> Differences:
        """Compare a test file with a correct file"""
        differences = Differences()

        # Obfuscate the test file content before diffing
        test_content = read_file(test)
        obfuscated_test_content = self._obfuscate_text(test_content)

        if self._create_test_data:
            # make the directory if it doesn't exist
            os.makedirs(os.path.dirname(correct), exist_ok=True)
            write_file(correct, obfuscated_test_content)

        correct_content = read_file(correct)

        if diff := get_diff(correct_content, obfuscated_test_content):
            differences.append({"file": diff})

        return differences

    def get_dict_differences(self, test: str, correct: str) -> Differences:
        """Compare a test file with a correct file"""

        # Load and obfuscate the test file
        test_dict = json.loads(read_file(test))
        obfuscated_test_dict = self._obfuscate_dict(test_dict)

        self._differences.clear()
        if self._create_test_data:
            try:
                correct_dict = json.loads(read_file(correct))
                self._compare_dicts(correct_dict, obfuscated_test_dict)
                assert self._differences == [], "The files should be identical"
            except (FileNotFoundError, AssertionError):
                # Save the obfuscated test file as the correct file
                os.makedirs(os.path.dirname(correct), exist_ok=True)
                write_file(correct, json.dumps(obfuscated_test_dict, indent=4, ensure_ascii=False).strip())
                self._differences.clear()
        else:
            correct_dict = json.loads(read_file(correct))
            self._compare_dicts(correct_dict, obfuscated_test_dict)

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
