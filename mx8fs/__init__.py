"""
MX8 - Common utilities for MX8 projects

Copyright (c) 2023 MX8 Inc, all rights reserved.

This software is confidential and proprietary information of MX8.
You shall not disclose such Confidential Information and shall use it only
in accordance with the terms of the agreement you entered into with MX8.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .cache import cache_to_disk, cache_to_disk_binary, get_cache_filename
from .comparer import ResultsComparer
from .file_io import (
    BinaryFileHandler,
    GzipFileHandler,
    VersionMismatchError,
    copy_file,
    delete_file,
    delete_files,
    file_exists,
    get_files,
    get_folders,
    get_public_url,
    list_files,
    most_recent_timestamp,
    move_file,
    purge_folder,
    read_file,
    read_file_with_version,
    update_file_if_version_matches,
    write_file,
)
from .lock import FileLock, Waiter
from .storage import JsonFileStorage, json_file_storage_factory

if TYPE_CHECKING:  # pragma: no cover
    from .index import (  # noqa: I001 - same-name aliases mark optional exports for mypy
        IndexRebuildResult as IndexRebuildResult,
        IndexSchemaError as IndexSchemaError,
        IndexUpdateError as IndexUpdateError,
        InvalidIndexError as InvalidIndexError,
        JsonIndex as JsonIndex,
        MissingIndexError as MissingIndexError,
        StorageQuery as StorageQuery,
        json_index_factory as json_index_factory,
    )
    from .indexed_storage import IndexedJsonFileStorage as IndexedJsonFileStorage

_OPTIONAL_EXPORTS = {
    "IndexedJsonFileStorage",
    "IndexRebuildResult",
    "IndexSchemaError",
    "IndexUpdateError",
    "InvalidIndexError",
    "JsonIndex",
    "MissingIndexError",
    "StorageQuery",
    "json_index_factory",
}


def _load_optional_export(name: str) -> Any:
    if name == "IndexedJsonFileStorage":
        from .indexed_storage import IndexedJsonFileStorage

        return IndexedJsonFileStorage
    from . import index

    return {
        "IndexRebuildResult": index.IndexRebuildResult,
        "IndexSchemaError": index.IndexSchemaError,
        "IndexUpdateError": index.IndexUpdateError,
        "InvalidIndexError": index.InvalidIndexError,
        "JsonIndex": index.JsonIndex,
        "MissingIndexError": index.MissingIndexError,
        "StorageQuery": index.StorageQuery,
        "json_index_factory": index.json_index_factory,
    }[name]


def __getattr__(name: str) -> Any:
    """Load indexed-storage exports only when their optional dependencies are installed."""
    if name not in _OPTIONAL_EXPORTS:
        raise AttributeError(name)
    try:
        value = _load_optional_export(name)
    except ModuleNotFoundError as error:
        raise ModuleNotFoundError(
            f"{name} requires the mx8fs indexed-storage extra; install mx8fs[indexed-json-storage]"
        ) from error
    globals()[name] = value
    return value


__all__ = [
    "BinaryFileHandler",
    "GzipFileHandler",
    "cache_to_disk_binary",
    "cache_to_disk",
    "copy_file",
    "delete_file",
    "delete_files",
    "file_exists",
    "FileLock",
    "get_cache_filename",
    "get_public_url",
    "get_files",
    "get_folders",
    "json_file_storage_factory",
    "JsonFileStorage",
    "list_files",
    "most_recent_timestamp",
    "move_file",
    "read_file_with_version",
    "read_file",
    "ResultsComparer",
    "update_file_if_version_matches",
    "VersionMismatchError",
    "Waiter",
    "purge_folder",
    "write_file",
]
