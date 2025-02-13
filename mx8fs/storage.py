"""
Generic file storage class.

Copyright (c) 2023-2025 MX8 Inc, all rights reserved.

This software is confidential and proprietary information of MX8.
You shall not disclose such Confidential Information and shall use it only
in accordance with the terms of the agreement you entered into with MX8.
"""

import os
import random
import string
from typing import Any, Callable, List, Optional, Union

from mx8fs import delete_file, file_exists, list_files, read_file, write_file


class JsonFileStorage:
    """A storage class for JSON serializable pydantic models."""

    _extension: str
    _key_field: str

    def __init__(self, base_path: str, randomizer: Optional[Callable[[], None]] = None) -> None:
        self.base_path = base_path

        if "AWS_LAMBDA_FUNCTION_NAME" in os.environ and randomizer is None:
            raise ValueError("Randomizer must be provided in AWS Lambda environment")

        self.randomizer = randomizer or random.seed

    @staticmethod
    def _json_to_model(json: str) -> Any:  # pragma: no cover
        raise NotImplementedError()

    @staticmethod
    def _dict_to_model(json: dict) -> Any:  # pragma: no cover
        raise NotImplementedError()

    @staticmethod
    def _model_to_json(content: Any) -> str:  # pragma: no cover
        raise NotImplementedError()

    def _get_unique_key(self, key_length: int = 8) -> str:
        """Create a eight letter unique key. This gives us nearly 3 trillion possibities"""

        self.randomizer()

        # Generate a random key
        key: str = "".join(random.choices(string.ascii_uppercase + string.digits, k=key_length))  # NOSONAR

        # If the key already exists, try again
        if file_exists(self._get_path(key)):
            return self._get_unique_key(key_length)

        return key

    def list(self) -> List[str]:
        """List files in storage."""

        return list_files(self.base_path, self._extension)

    def read(self, key: str) -> Any:
        """Read a file from storage."""

        return self._json_to_model(read_file(self._get_path(key)))

    def write(self, content: Any, key: Union[str, None] = None) -> Any:
        """Write a file to storage."""
        return self.write_dict(content.model_dump(), key)

    def write_dict(self, content: dict, key: Union[str, None] = None) -> Any:
        """Write a file to storage."""

        # If no key is provided, generate a unique key
        key = key or content.get(self._key_field, None)
        if not key:
            key = self._get_unique_key()

        # Add the key to the content
        content[self._key_field] = key
        content_out = self._dict_to_model(content)

        # Now write the file
        return self.update(content_out)

    def update(self, content: Any) -> Any:
        """Update a file in storage."""

        write_file(
            self._get_path(getattr(content, self._key_field)),
            self._model_to_json(content),
        )
        return content

    def delete(self, key: str) -> None:
        """Delete a file from storage."""

        delete_file(self._get_path(key))

    def _get_path(self, key: str) -> str:
        """Get the path for a file."""
        return os.path.join(self.base_path, f"{key}.{self._extension}")


def json_file_storage_factory(extension: str, model: Any, key_field: str = "key") -> type[JsonFileStorage]:
    """Create a file storage class."""

    cls: type[JsonFileStorage] = type(f"{model.__class__}Storage", (JsonFileStorage,), {})

    def _json_to_model(json: str) -> Any:
        """Convert a JSON object to a model."""
        return model.model_validate_json(json)

    def _dict_to_model(json: dict) -> Any:
        """Convert a dictionary to a model."""
        return model(**json)

    def _model_to_json(content: Any) -> str:
        """Convert a model to a JSON object."""
        if not isinstance(content, model):  # pragma: no cover
            raise ValueError(f"Expected {model}, got {type(content)}")

        return str(content.model_dump_json())

    setattr(cls, "_json_to_model", staticmethod(_json_to_model))
    setattr(cls, "_dict_to_model", staticmethod(_dict_to_model))
    setattr(cls, "_model_to_json", staticmethod(_model_to_json))
    setattr(cls, "_extension", extension)
    setattr(cls, "_key_field", key_field)

    return cls
