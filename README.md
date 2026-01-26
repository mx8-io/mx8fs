# MX8 File system

This library provides environment agnostic file system access across local and AWS, including:
- File / IO
- List / Glob
- Locking
- Caching
- Comparing Dictionaries

## API Reference

Below are the functions and classes exported by the library (`mx8fs.__all__`). Paths can be local (e.g. `/tmp/file.txt`) or S3 (`s3://bucket/key`). HTTPS reads are supported in specific helpers.

### File I/O and Paths

- `read_file(path: str) -> str`: Read text from S3, HTTPS, or local using UTF‑8.
  - S3: `GetObject` and decode body.
  - HTTPS: simple GET; raises `FileNotFoundError` for network or 404.
  - Local: standard open/read.

  Example:
  ```python
  from mx8fs import read_file

  text = read_file('/tmp/example.txt')
  s3_text = read_file('s3://my-bucket/path/data.txt')
  https_text = read_file('https://example.com/info.txt')
  ```

- `read_file_with_version(path: str) -> tuple[str, str]`: Read text and a version identifier.
  - S3: returns `(content, etag)` (quotes stripped).
  - Local: returns `(content, mtime-as-string)`.

  Example:
  ```python
  from mx8fs import read_file_with_version

  content, version = read_file_with_version('s3://my-bucket/app/config.json')
  ```

- `write_file(path: str, data: str) -> None`: Write UTF‑8 text to S3 or local. Creates parent directories for local writes.

  Example:
  ```python
  from mx8fs import write_file

  write_file('/tmp/output.txt', 'hello world')
  write_file('s3://my-bucket/logs/run.txt', 'done')
  ```

- `update_file_if_version_matches(path: str, data: str, version: str) -> None`: Conditional write.
  - S3: uses `IfMatch=<etag>`. Raises `VersionMismatchError` on mismatch, `FileNotFoundError` if key missing.
  - Local: acquires a `FileLock`, compares mtime string; raises `VersionMismatchError` or `FileNotFoundError` similarly.

  Example (optimistic concurrency):
  ```python
  from mx8fs import read_file_with_version, update_file_if_version_matches, VersionMismatchError

  path = 's3://my-bucket/app/config.json'
  data, ver = read_file_with_version(path)
  new_data = data + "\n" + "# updated"
  try:
      update_file_if_version_matches(path, new_data, ver)
  except VersionMismatchError:
      # somebody else updated it — reload & retry as needed
      pass
  ```

- `file_exists(path: str) -> bool`: True if file exists (HEAD on S3; `os.path.exists` locally).

  Example:
  ```python
  from mx8fs import file_exists
  if file_exists('s3://my-bucket/data.csv'):
      ...
  ```

- `delete_file(path: str) -> None`: Delete from S3 or local. Local deletion ignores missing files.

  Example:
  ```python
  from mx8fs import delete_file
  delete_file('/tmp/temp.txt')
  delete_file('s3://my-bucket/tmp/stale.json')
  ```

- `delete_files(paths: list[str], max_workers: int = 500) -> None`: Batch delete mixed S3/local paths.
  - S3: groups by bucket; uses `DeleteObjects` in chunks of 1000.
  - Local: deletes concurrently with a thread pool.

  Example:
  ```python
  from mx8fs import delete_files
  delete_files([
      '/tmp/a.txt',
      's3://my-bucket/path/old.log',
  ])
  ```

- `copy_file(src: str, dst: str, chunk_size: int = 131072) -> None`: Copy S3→S3 via `CopyObject`, or stream copy for all other combinations (S3/local/HTTPS read with `BinaryFileHandler`).

  Example:
  ```python
  from mx8fs import copy_file
  copy_file('/tmp/input.bin', '/tmp/output.bin')
  copy_file('s3://my-bucket/a.txt', 's3://my-bucket/archive/a.txt')
  ```

- `move_file(src: str, dst: str) -> None`: Copy then delete source.

  Example:
  ```python
  from mx8fs import move_file
  move_file('s3://my-bucket/new.txt', 's3://my-bucket/archive/new.txt')
  ```

- `get_files(root_path: str, prefix: str = "", cutoff_date: datetime | None = None, cutoff_earlier: bool = True) -> list[str]`: List files under a root.
  - S3: returns keys relative to the root prefix; optional `prefix` filter and `cutoff_date` (defaults to older than) on `LastModified`.
  - Local: returns relative file paths under the directory; supports the same filters (mtime in UTC).

  Example:
  ```python
  from datetime import datetime, timezone
  from mx8fs import get_files

  files = get_files('/data/reports/', prefix='2025-')
  old = get_files('s3://my-bucket/reports/', cutoff_date=datetime.now(timezone.utc))
  ```

- `list_files(root_path: str, file_type: str, prefix: str = "") -> list[str]`: List files with a given extension (without dot).
  - Returns basenames without extension, optionally filtered by `prefix`.

  Example:
  ```python
  from mx8fs import list_files
  names = list_files('/data', 'json', prefix='user_')  # ['user_1', 'user_2']
  ```

- `get_folders(root_path: str, prefix: str = "") -> list[str]`: List immediate subfolders (non‑recursive).
  - S3: uses `Delimiter='/'` and returns top‑level folder names under the prefix.
  - Local: returns directory names directly under `root_path`.

  Example:
  ```python
  from mx8fs import get_folders
  top = get_folders('s3://my-bucket/data/')  # e.g. ['a', 'b']
  ```

- `most_recent_timestamp(root_path: str, file_type: str) -> float`: Latest modification time for files of a type.
  - S3: scans with a paginator and returns the newest `LastModified` as epoch seconds.
  - Local: max of `os.path.getmtime` for `*.ext`, or 0 when none.

  Example:
  ```python
  from mx8fs import most_recent_timestamp
  ts = most_recent_timestamp('/data', 'csv')
  ```

- `get_public_url(path: str, expires_in: int = 3600, method: str = "get_object") -> str`: For S3, returns a presigned URL for GET/PUT; for local, returns the input path.

  Example:
  ```python
  from mx8fs import get_public_url
  get_url = get_public_url('s3://my-bucket/file.txt')
  put_url = get_public_url('s3://my-bucket/file.txt', method='put_object')
  ```

- `purge_folder(root_path: str, dry_run: bool = True, max_workers: int = 500, cutoff_date: datetime | None = None) -> list[str]`: List and optionally delete all files under a folder/prefix.
  - Returns the full paths that were (or would be) deleted; respects `cutoff_date` when provided.

  Example:
  ```python
  from mx8fs import purge_folder
  paths = purge_folder('s3://my-bucket/tmp/', dry_run=True)
  deleted = purge_folder('/tmp/workdir', dry_run=False)
  ```

### Stream Handlers

- `class BinaryFileHandler(path: str, mode: str = "rb", content_type: str | None = None)`: Context manager for binary reads/writes.
  - S3: `rb` downloads to an in‑memory buffer; `wb` uploads on exit (uses `content_type` if provided).
  - HTTPS: supports `rb` read‑only; writing or text modes raise `NotImplementedError`.
  - Local: proxies to built‑in file object; ensures directories exist for writes.
  - Usage:
  ```python
  from mx8fs import BinaryFileHandler
  with BinaryFileHandler('s3://bucket/key.bin', 'wb', content_type='application/octet-stream') as f:
      f.write(b'data')
  ```

- `GzipFileHandler(path: str, mode: str = "rb", encoding: str | None = None)`: Context manager for gzip‑compressed files.
  - Modes: `rb`, `wb`, `rt`, `wt` (text modes require `encoding`). Wraps `BinaryFileHandler` under the hood.

  Example:
  ```python
  from mx8fs import GzipFileHandler
  # write text
  with GzipFileHandler('/tmp/data.gz', 'wt', encoding='utf-8') as f:
      f.write('hello')
  # read binary
  with GzipFileHandler('/tmp/data.gz', 'rb') as f:
      blob = f.read()
  ```

### Locking

- `class Waiter(wait_period: float, time_out_seconds: float)`: Helper for periodic waits with a timeout.
  - Methods: `start_timeout()`, `check_timeout()`, `wait()`, `timed_out()`; usable as a context manager.

  Example:
  ```python
  from mx8fs import Waiter
  with Waiter(0.1, 5) as w:
      while not some_condition():
          w.check_timeout()
  ```

- `class FileLock(file: str, wait_period: float = 0.1, time_out_seconds: int = 840, maximum_age: int = 900)`: Cross‑process/client lock via lock files.
  - Creates lock files named `{file}.{timestamp}.{random}.lock` and coordinates access; cleans up on exit.
  - Use a higher `wait_period` for eventually‑consistent backends.
  - Usage:
  ```python
  from mx8fs import FileLock, write_file
  with FileLock('s3://bucket/data.txt'):
      write_file('s3://bucket/data.txt', 'content')
  ```

### Caching

- `get_cache_filename(path: str, name: str, extension: str, expiration_seconds: int = 0, **kwargs) -> str`: Build a deterministic, optionally time‑scoped cache filename based on args/kwargs.

  Example:
  ```python
  from mx8fs import get_cache_filename
  fname = get_cache_filename('/tmp/cache', 'fetch_users', 'txt', expiration_seconds=3600, region='us', extra_args=(1,2))
  ```

- `cache_to_disk(path: str, expiration_seconds: int = 0, log_group: str = "", ignore_kwargs: list[str] | None = None)`: Decorator for caching text results.
  - Stores `.txt` payloads using `read_file`/`write_file`; logs cache hits when `log_group` is set.

  Example:
  ```python
  from mx8fs import cache_to_disk

  @cache_to_disk('/tmp/mycache', expiration_seconds=600)
  def compute(user_id: str) -> str:
      return f"hello {user_id}"

  print(compute('42'))
  ```

- `cache_to_disk_binary(path: str, expiration_seconds: int = 0, log_group: str = "", ignore_kwargs: list[str] | None = None)`: Decorator for caching arbitrary Python objects via pickle.
  - Stores `.pickle` payloads using `BinaryFileHandler`.

  Example:
  ```python
  from mx8fs import cache_to_disk_binary

  @cache_to_disk_binary('/tmp/bin_cache')
  def heavy() -> dict:
      return {'a': 1}
  data = heavy()
  ```

### JSON Storage

- `class JsonFileStorage(base_path: str, randomizer: Callable | None = None)`: Base class for simple JSON model storage.
  - Methods: `list()`, `read(key)`, `write(model)`, `write_dict(dict, key=None)`, `update(model)`, `delete(key)`.
  - Implements unique key generation and defers serialization to subclass hooks.

  Example (using factory below for a Pydantic model): see next section.

- `json_file_storage_factory(extension: str, model: Any, key_field: str = "key") -> type[JsonFileStorage]`: Generates a concrete `JsonFileStorage` for a Pydantic model.
  - The resulting class knows how to serialize/deserialize the given `model` and stores files as `<key>.<extension>`.

  Example:
  ```python
  from pydantic import BaseModel
  from mx8fs import json_file_storage_factory

  class User(BaseModel):
      key: str | None = None
      name: str

  UserStorage = json_file_storage_factory('json', User, key_field='key')
  store = UserStorage('/tmp/users')

  u = store.write(User(name='Ada'))     # auto-keyed
  u = store.update(User(key=u.key, name='Ada Lovelace'))
  got = store.read(u.key)
  all_keys = store.list()
  store.delete(u.key)
  ```

### Comparison Utilities

- `class ResultsComparer(ignore_keys: list[str] | None, create_test_data: bool = False, obfuscate_regex: str | None = None)`: Compare text/JSON with optional obfuscation and test‑data generation.
  - Methods: `compare_dicts`, `get_text_differences`, `get_dict_differences`, `get_api_response_differences`.
  - Obfuscates sensitive content by default (passwords, tokens, keys, etc.).

  Example:
  ```python
  from mx8fs import ResultsComparer

  rc = ResultsComparer(ignore_keys=['timestamp'])
  diffs = rc.compare_dicts({'a': 1, 'timestamp': 1}, {'a': 2, 'timestamp': 2})
  if diffs:
      print(diffs)
  ```

### Exceptions

- `class VersionMismatchError(FileNotFoundError)`: Raised by `update_file_if_version_matches` on conditional write mismatches.

  Example (catching):
  ```python
  from mx8fs import VersionMismatchError
  try:
      raise VersionMismatchError('example')
  except VersionMismatchError:
      pass
  ```

## Notes

- S3 settings: the client uses environment variables `BOTO_MAX_CONNECTIONS`, `BOTO_CONNECT_TIMEOUT`, `BOTO_READ_TIMEOUT`, `BOTO_MAX_RETRIES`, and `BOTO_RETRY_MODE` to tune behavior.
- Time handling: `cutoff_date` is normalized to timezone‑aware UTC for consistent comparisons.
- HTTPS reads: `read_file` and `BinaryFileHandler` (`rb`) support `https://` sources; writing to HTTPS is not supported.

# Pre-commit hooks

We use precommit to run formatting checks, so whenever you clone a project run:

```bash
pre-commit install
```

Before you do anything else.

You can run this at any time using:

```bash
pre-commit run --all-files
```

## Setting up the development environment

You can install the full dev requirements by running [setup.sh](setup.sh) to
1. Install the current repo and the python lib
1. Run the pre-commit hooks on all files

The project should open reasonable well in vs.code and includes three [launch configurations](.vscode/launch.json) for running unit tests and the debug server.

## Code conventions and structure

We use python type hinting with pylance and flake8 for linting. Unit tests are created to 100% branch coverage.

The code is structured as follows:

- The [mx8fs](mx8fs) folder contains the full library.
- Tests are stored in the [tests](test) folder and run using pytest.

* The github actions are in [main.yam](.github/workflows/main.yml)

## License

Copyright © 2025 MX8 Labs

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


## Development setup

Run `./setup.sh` to install dependencies and configure the environment.

## Tooling

Use `black`, `ruff`, and `mypy` for formatting/linting/type checking. Run `pytest` for tests.
