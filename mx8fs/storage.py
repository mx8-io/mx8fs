"""
Generic file storage class.

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

from __future__ import annotations

import builtins
import os
import random
import string
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any, cast, overload

from .file_io import (
    FileMetadata,
    FileNotDeletedError,
    FileVersionMetadata,
    VersionMismatchError,
    delete_file,
    file_exists,
    list_file_versions,
    list_files,
    list_files_with_metadata,
    read_file,
    read_file_with_version,
    update_file_if_version_matches,
    write_file,
)
from .lock import FileLock

if TYPE_CHECKING:  # pragma: no cover
    from .index import JsonIndex
    from .indexed_storage import IndexedJsonFileStorage

_MAX_VERSION_ATTEMPTS = 3
_PARALLEL_PREFETCH_LIMIT = 1000


def _parallel_map[InputT, OutputT](
    function: Callable[[InputT], OutputT],
    items: builtins.list[InputT],
    max_workers: int | None = None,
    on_result: Callable[[], None] | None = None,
) -> builtins.list[OutputT]:
    """Apply a function concurrently with bounded submissions while preserving order."""
    workers = max_workers if max_workers is not None else min(32, (os.cpu_count() or 1) + 4)
    if workers <= 0:
        raise ValueError("max_workers must be positive")
    if not items:
        return []
    max_pending = max(workers, min(_PARALLEL_PREFETCH_LIMIT, workers * 10))
    results: dict[int, OutputT] = {}
    indexed_items = iter(enumerate(items))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: dict[Future[OutputT], int] = {}

        def submit_next() -> bool:
            try:
                index, item = next(indexed_items)
            except StopIteration:
                return False
            pending[executor.submit(function, item)] = index
            return True

        for _ in range(min(max_pending, len(items))):
            submit_next()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                results[pending.pop(future)] = future.result()
                if on_result is not None:
                    on_result()
                submit_next()
    return [results[index] for index in range(len(items))]


class JsonFileStorage[ModelT]:
    """A storage class for JSON serializable pydantic models."""

    _extension: str
    _key_field: str
    _randomizer: Callable[[], None] = random.seed

    def __init__(self, base_path: str, randomizer: Callable[[], None] | None = None) -> None:
        """Initialize storage with a base path and optional randomizer."""
        self.base_path = base_path
        self._randomizer = randomizer or self._randomizer

        if "AWS_LAMBDA_FUNCTION_NAME" in os.environ and self._randomizer == random.seed:
            raise ValueError("Cannot use random.seed as a randomizer in AWS Lambda environment")

        self.randomizer = randomizer or random.seed

    @staticmethod
    def _json_to_model(json: str) -> ModelT:  # pragma: no cover
        raise NotImplementedError()

    @staticmethod
    def _dict_to_model(json: dict[str, Any]) -> ModelT:  # pragma: no cover
        raise NotImplementedError()

    @staticmethod
    def _model_to_json(content: ModelT) -> str:  # pragma: no cover
        raise NotImplementedError()

    def _get_unique_key(self, key_length: int = 8) -> str:
        """Create a eight letter unique key. This gives us nearly 3 trillion possibilities."""
        self.randomizer()

        # Generate a random key
        key: str = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=key_length)  # nosec - insecure random number
        )

        # If the key already exists, try again
        if file_exists(self._get_path(key)):
            return self._get_unique_key(key_length)

        return key

    def list(self, *, include_deleted: bool = False) -> list[str]:
        """List files in storage."""
        return list_files(self.base_path, self._extension, include_deleted=include_deleted)

    def list_with_metadata(self, *, include_deleted: bool = False) -> builtins.list[FileMetadata]:
        """List files and their portable metadata in storage."""
        return list_files_with_metadata(self.base_path, self._extension, include_deleted=include_deleted)

    def history(self, key: str) -> builtins.list[FileVersionMetadata]:
        """List historical versions and delete markers for a stored file."""
        return list_file_versions(self._get_path(key))

    def read(self, key: str) -> ModelT:
        """Read a file from storage."""
        return self._json_to_model(read_file(self._get_path(key)))

    def read_with_version(self, key: str) -> tuple[ModelT, str]:
        """Read canonical JSON with its S3 ETag or local modification version."""
        contents, version = read_file_with_version(self._get_path(key))
        return self._json_to_model(contents), version

    def read_version(self, key: str, version_id: str) -> ModelT:
        """Read a specific historical version."""
        return self._json_to_model(read_file(self._get_path(key), version_id=version_id))

    def read_many(self, keys: builtins.list[str], max_workers: int | None = None) -> builtins.list[ModelT]:
        """Read multiple models concurrently while preserving key order."""
        return _parallel_map(self.read, keys, max_workers)

    def write(self, content: ModelT, key: str | None = None) -> ModelT:
        """Write a file to storage."""
        return self.write_dict(cast(Any, content).model_dump(), key)

    def write_dict(self, content: dict[str, Any], key: str | None = None) -> ModelT:
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

    def update(self, content: ModelT) -> ModelT:
        """Update a file in storage."""
        self._write_with_version(content)
        return content

    def _write_with_version(self, content: ModelT) -> str:
        """Write canonical JSON and return the resulting version."""
        return write_file(self._get_path(getattr(content, self._key_field)), self._model_to_json(content))

    def update_if_version(self, content: ModelT, version: str) -> ModelT:
        """Update canonical JSON only when its current version matches."""
        self._update_if_version(content, version)
        return content

    def _update_if_version(self, content: ModelT, version: str) -> str:
        """Conditionally write canonical JSON and return the resulting version."""
        key = cast(str, getattr(content, self._key_field))
        return update_file_if_version_matches(self._get_path(key), self._model_to_json(content), version)

    def mutate(
        self,
        key: str,
        mutation: Callable[[ModelT], ModelT],
        max_attempts: int = _MAX_VERSION_ATTEMPTS,
    ) -> ModelT:
        """Apply a side-effect-free mutation using optimistic version checks."""
        updated, _ = self._mutate_with_version(key, mutation, max_attempts)
        return updated

    def _mutate_with_version(
        self,
        key: str,
        mutation: Callable[[ModelT], ModelT],
        max_attempts: int,
    ) -> tuple[ModelT, str]:
        """Apply a mutation and return the model with its written version."""
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        mismatch: VersionMismatchError | None = None
        for _ in range(max_attempts):
            current, version = self.read_with_version(key)
            updated = mutation(current)
            updated_key = cast(str, getattr(updated, self._key_field))
            if updated_key != key:
                raise ValueError("Mutations may not change the storage key")
            try:
                written_version = self._update_if_version(updated, version)
                return updated, written_version
            except VersionMismatchError as error:
                mismatch = error
        raise VersionMismatchError(
            f"JSON changed during all {max_attempts} mutation attempts for key {key}"
        ) from mismatch

    def delete(self, key: str) -> None:
        """Delete a file from storage."""
        delete_file(self._get_path(key))

    def restore_version(self, key: str, version_id: str) -> ModelT:
        """Restore a historical model as the new current version."""
        return self.update(self.read_version(key, version_id))

    def undelete(self, key: str) -> ModelT:
        """Restore the newest content when a stored file is deleted."""
        versions = self.history(key)
        if not versions:
            raise FileNotFoundError(f"File {key} has no recoverable versions")
        if not versions[0].is_deleted:
            raise FileNotDeletedError(f"File {key} is not deleted")
        try:
            version = next(item for item in versions[1:] if not item.is_deleted)
        except StopIteration as exc:
            raise FileNotFoundError(f"File {key} has no recoverable versions") from exc
        return self.restore_version(key, version.version_id)

    def get_lock(
        self,
        key: str,
        wait_period: float = 0.1,
        time_out_seconds: int = 840,
        maximum_age: int = 900,
    ) -> FileLock:
        """Get a file lock for the stored file."""
        return FileLock(
            self._get_path(key),
            wait_period=wait_period,
            time_out_seconds=time_out_seconds,
            maximum_age=maximum_age,
        )

    def _get_path(self, key: str) -> str:
        """Get the path for a file."""
        return os.path.join(self.base_path, f"{key}.{self._extension}")


@overload
def json_file_storage_factory[ModelT](
    extension: str,
    model: type[ModelT],
    key_field: str = "key",
    *,
    index: None = None,
) -> type[JsonFileStorage[ModelT]]: ...


@overload
def json_file_storage_factory[ModelT](
    extension: str,
    model: type[ModelT],
    key_field: str = "key",
    *,
    index: JsonIndex[ModelT],
) -> type[IndexedJsonFileStorage[ModelT]]: ...


def json_file_storage_factory[ModelT](
    extension: str,
    model: type[ModelT],
    key_field: str = "key",
    *,
    index: JsonIndex[ModelT] | None = None,
) -> type[JsonFileStorage[ModelT]]:
    """Create a file storage class."""
    if index is not None and (index.model is not model or index.key_field != key_field):
        raise ValueError("The index model and key field must match the storage factory")
    if index is not None:
        from .indexed_storage import IndexedJsonFileStorage

        base = cast(type[JsonFileStorage[ModelT]], IndexedJsonFileStorage)
    else:
        base = JsonFileStorage
    cls = cast(type[JsonFileStorage[ModelT]], type(f"{model.__name__}Storage", (base,), {}))

    def _json_to_model(json: str) -> ModelT:
        """Convert a JSON object to a model."""
        return cast(ModelT, cast(Any, model).model_validate_json(json))

    def _dict_to_model(json: dict[str, Any]) -> ModelT:
        """Convert a dictionary to a model."""
        return model(**json)

    def _model_to_json(content: ModelT) -> str:
        """Convert a model to a JSON object."""
        if not isinstance(content, model):  # pragma: no cover
            raise ValueError(f"Expected {model}, got {type(content)}")

        return str(cast(Any, content).model_dump_json())

    cls._json_to_model = staticmethod(_json_to_model)  # type: ignore[method-assign]
    cls._dict_to_model = staticmethod(_dict_to_model)  # type: ignore[method-assign]
    cls._model_to_json = staticmethod(_model_to_json)  # type: ignore[method-assign]
    cls._extension = extension
    cls._key_field = key_field

    if index is not None:
        cast(Any, cls)._index_definition = index

    return cls
