"""
Cache decorator for the MX8 AI API.

Copyright (c) 2023-2025 MX8 Inc, all rights reserved.

This software is confidential and proprietary information of MX8.
You shall not disclose such Confidential Information and shall use it only
in accordance with the terms of the agreement you entered into with MX8.
"""

import hashlib
import json
import logging
import pickle
from json.decoder import JSONDecodeError
from time import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .file_io import BinaryFileHandler, read_file, write_file


def get_cache_filename(path: str, name: str, extension: str, expiration_seconds: int = 0, **kwargs: Dict) -> str:
    """Create an optionally time expiring cache filename using hashed parameters."""

    # Create hashes based on the name and parameter hashes
    args = kwargs.pop("extra_args", ())
    param_hash = hashlib.sha256(pickle.dumps((args, kwargs))).hexdigest()

    # If the cache is set to expire, add the current epoch rounded down
    # to the nearest expiration_seconds to the hash
    if expiration_seconds > 0:
        epoch = int(int(time()))
        time_hash = "_" + str(epoch - epoch % expiration_seconds)
    else:
        time_hash = ""

    # Create the filename for the cache
    return f"{path}/{name}_{param_hash}{time_hash}.cache.{extension}"


def _get_clean_kwargs(kwargs: Dict, ignore_kwargs: Optional[List[str]]) -> Dict:
    """Remove ignored kwargs from the kwargs dict"""

    clean_kwargs = kwargs.copy()
    for ignore in ignore_kwargs or []:
        clean_kwargs.pop(ignore, None)

    return clean_kwargs


def _do_logging(
    log_group: str,
    result: Any,
    args: Tuple,
    kwargs: Dict,
    filename: str,
    func: Callable,
    expiration_seconds: int,
) -> None:
    """Log cache hit"""

    try:
        result = json.loads(result)
    except (JSONDecodeError, TypeError):
        pass

    if log_group:
        try:
            logging.getLogger(log_group).info(
                "Cache hit",
                extra={
                    "cache_result": result,
                    "cache_args": args,
                    "cache_kwargs": kwargs,
                    "cache_filename": filename,
                    "cache_function": func.__name__,
                    "cache_expiration_seconds": expiration_seconds,
                },
            )
        except TypeError:  # pragma: no cover
            # If we get a type error, case the dangerous types to strings
            logging.getLogger(log_group).info(
                "Cache hit",
                extra={
                    "cache_result": str(result),
                    "cache_args": args,
                    "cache_kwargs": str(kwargs),
                    "cache_filename": filename,
                    "cache_function": func.__name__,
                    "cache_expiration_seconds": expiration_seconds,
                },
            )


def cache_to_disk_binary(
    path: str,
    expiration_seconds: int = 0,
    log_group: str = "",
    ignore_kwargs: Optional[List[str]] = None,
) -> Callable[..., Callable[..., Any]]:
    """Cache decorator for any MX8 functions.

    This decorator will cache the result of the function to disk, and return
    the cached result on subsequent calls. This is useful for caching the
    results of expensive operations, such as calling the AI API

    Parameters:
        path: The path to the cache directory
        expiration_seconds: The number of seconds before the cache expires
        log_group: The log group to log cache hits to
        ignore_kwargs: A list of kwargs to ignore when creating the cache key
    """

    def decorator(func: Callable) -> Callable[..., Any]:
        def wrapper(*args: Tuple, **kwargs: Dict) -> Any:
            clean_kwargs = _get_clean_kwargs(kwargs, ignore_kwargs)

            filename = get_cache_filename(
                path,
                func.__name__,
                "pickle",
                expiration_seconds,
                extra_args=args,  # type: ignore
                **clean_kwargs,
            )

            try:
                # Try to read the cached result from disk
                with BinaryFileHandler(filename) as file_handler:
                    result = pickle.load(file_handler)

                _do_logging(log_group, result, args, clean_kwargs, filename, func, expiration_seconds)

            except FileNotFoundError:
                # Cache miss, execute the function and save the result to disk
                result = func(*args, **kwargs)
                with BinaryFileHandler(filename, "wb") as file_handler:
                    pickle.dump(result, file_handler)

            return result

        return wrapper

    return decorator


def cache_to_disk(
    path: str,
    expiration_seconds: int = 0,
    log_group: str = "",
    ignore_kwargs: Optional[List[str]] = None,
) -> Callable[..., Callable[..., str | Any]]:
    """Cache decorator for any MX8 functions.

    This decorator will cache the result of the function to disk, and return
    the cached result on subsequent calls. This is useful for caching the
    results of expensive operations, such as calling the AI API

    Parameters:
        path: The path to the cache directory
        expiration_seconds: The number of seconds before the cache expires
        log_group: The log group to log cache hits to
        ignore_kwargs: A list of kwargs to ignore when creating the cache key
    """

    def decorator(func: Callable) -> Callable[..., str | Any]:
        def wrapper(*args: Tuple, **kwargs: Dict) -> str | Any:

            clean_kwargs = _get_clean_kwargs(kwargs, ignore_kwargs)

            filename = get_cache_filename(
                path,
                func.__name__,
                "txt",
                expiration_seconds,
                extra_args=args,  # type: ignore
                **clean_kwargs,
            )

            try:
                # Try to read the cached result from disk
                result = read_file(filename)

                _do_logging(log_group, result, args, clean_kwargs, filename, func, expiration_seconds)

            except FileNotFoundError:
                # Cache miss, execute the function and save the result to disk
                result = func(*args, **kwargs)
                write_file(filename, result)

            return result

        return wrapper

    return decorator
