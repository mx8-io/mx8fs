"""Discovery and bounded-parallel migration helpers for indexed JSON storage."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from time import perf_counter
from typing import Any

from .index import IndexRebuildResult
from .indexed_storage import IndexedJsonFileStorage

_IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_INDEX_REGISTRY_GROUP = "mx8fs.index_registries"


@dataclass(frozen=True)
class IndexMigrationTarget:
    """Serializable identity for one indexed JSON namespace."""

    storage_type: str
    base_path: str
    table_name: str
    namespace: str


@dataclass(frozen=True)
class IndexMigrationResult:
    """Successful migration result and elapsed time."""

    target: IndexMigrationTarget
    upserted: int
    removed: int
    seconds: float


@dataclass(frozen=True)
class IndexMigrationFailure:
    """Migration failure retained without stopping independent targets."""

    target: IndexMigrationTarget
    phase: str
    error: str


@dataclass(frozen=True)
class IndexMigrationReport:
    """Complete result of a migration plan."""

    planned: tuple[IndexMigrationTarget, ...]
    results: tuple[IndexMigrationResult, ...]
    failures: tuple[IndexMigrationFailure, ...]
    dry_run: bool

    @property
    def succeeded(self) -> bool:
        """Return whether every planned target migrated successfully."""
        return not self.failures


@dataclass(frozen=True)
class DiscoveredIndex:
    """One advisory static discovery result."""

    path: str
    line: int
    symbol: str | None


def load_index_registry(name: str) -> list[IndexedJsonFileStorage[Any]]:
    """Load and validate an installed ``mx8fs.index_registries`` entry point."""
    matches = [entry for entry in entry_points(group=_INDEX_REGISTRY_GROUP) if entry.name == name]
    if not matches:
        raise ValueError(f"No installed {_INDEX_REGISTRY_GROUP} entry point named {name!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple installed {_INDEX_REGISTRY_GROUP} entry points are named {name!r}")
    registry = matches[0].load()
    if not callable(registry):
        raise TypeError(f"Registry {name!r} is not callable")
    targets = list(registry())
    if any(not isinstance(target, IndexedJsonFileStorage) for target in targets):
        raise TypeError("Registry must return only IndexedJsonFileStorage instances")
    return targets


def _target(storage: IndexedJsonFileStorage[Any]) -> IndexMigrationTarget:
    return IndexMigrationTarget(
        storage_type=type(storage).__name__,
        base_path=storage.base_path,
        table_name=storage.index.table_name,
        namespace=storage._namespace,
    )


def _unique_storages(
    storages: Iterable[IndexedJsonFileStorage[Any]],
) -> list[IndexedJsonFileStorage[Any]]:
    unique: dict[tuple[int, str, str], IndexedJsonFileStorage[Any]] = {}
    for storage in storages:
        if not isinstance(storage, IndexedJsonFileStorage):
            raise TypeError("Migration targets must be IndexedJsonFileStorage instances")
        identity = (id(storage._index_manager.engine), storage.index.table_name, storage._namespace)
        unique.setdefault(identity, storage)
    return list(unique.values())


def _migrate_schema_groups(
    storages: list[IndexedJsonFileStorage[Any]], jobs: int
) -> tuple[set[tuple[int, str]], list[IndexMigrationFailure]]:
    schema_groups: dict[tuple[int, str], list[IndexedJsonFileStorage[Any]]] = {}
    for storage in storages:
        key = (id(storage._index_manager.engine), storage.index.table_name)
        schema_groups.setdefault(key, []).append(storage)
    failed_schema_keys: set[tuple[int, str]] = set()
    failures: list[IndexMigrationFailure] = []
    with ThreadPoolExecutor(max_workers=min(jobs, len(schema_groups))) as executor:
        futures = {
            executor.submit(group[0].migrate_schema): (schema_key, group) for schema_key, group in schema_groups.items()
        }
        for future in as_completed(futures):
            schema_key, group = futures[future]
            try:
                future.result()
            except Exception as error:  # batch migration must report independent failures
                failed_schema_keys.add(schema_key)
                failures.extend(
                    IndexMigrationFailure(target=_target(storage), phase="schema", error=str(error))
                    for storage in group
                )
    return failed_schema_keys, failures


def _rebuild_namespaces(
    storages: list[IndexedJsonFileStorage[Any]], jobs: int, total_read_workers: int
) -> tuple[list[IndexMigrationResult], list[IndexMigrationFailure]]:
    if not storages:
        return [], []
    active_jobs = min(jobs, len(storages))
    read_workers = max(1, total_read_workers // active_jobs)

    def rebuild(storage: IndexedJsonFileStorage[Any]) -> tuple[IndexRebuildResult, float]:
        started = perf_counter()
        result = storage.rebuild_index(max_workers=read_workers)
        return result, perf_counter() - started

    results: list[IndexMigrationResult] = []
    failures: list[IndexMigrationFailure] = []
    with ThreadPoolExecutor(max_workers=active_jobs) as executor:
        futures = {executor.submit(rebuild, storage): storage for storage in storages}
        for future in as_completed(futures):
            storage = futures[future]
            try:
                result, seconds = future.result()
                results.append(
                    IndexMigrationResult(
                        target=_target(storage),
                        upserted=result.upserted,
                        removed=result.removed,
                        seconds=seconds,
                    )
                )
            except Exception as error:  # batch migration must report independent failures
                failures.append(IndexMigrationFailure(target=_target(storage), phase="namespace", error=str(error)))
    return results, failures


def migrate_indexes(
    storages: Iterable[IndexedJsonFileStorage[Any]],
    *,
    jobs: int = 4,
    total_read_workers: int = 100,
    dry_run: bool = False,
) -> IndexMigrationReport:
    """Migrate physical schemas once, then rebuild namespaces with bounded parallelism."""
    if jobs <= 0:
        raise ValueError("jobs must be positive")
    if total_read_workers <= 0:
        raise ValueError("total_read_workers must be positive")
    unique = _unique_storages(storages)
    planned = tuple(_target(storage) for storage in unique)
    if dry_run or not unique:
        return IndexMigrationReport(planned=planned, results=(), failures=(), dry_run=dry_run)

    failed_schema_keys, failures = _migrate_schema_groups(unique, jobs)

    eligible = [
        storage
        for storage in unique
        if (id(storage._index_manager.engine), storage.index.table_name) not in failed_schema_keys
    ]
    results, namespace_failures = _rebuild_namespaces(eligible, jobs, total_read_workers)
    failures.extend(namespace_failures)

    target_order = {target: index for index, target in enumerate(planned)}
    results.sort(key=lambda result: target_order[result.target])
    failures.sort(key=lambda failure: target_order[failure.target])
    return IndexMigrationReport(
        planned=planned,
        results=tuple(results),
        failures=tuple(failures),
        dry_run=False,
    )


def _factory_aliases(tree: ast.AST) -> set[str]:
    aliases = {"json_file_storage_factory"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"mx8fs", "mx8fs.storage"}:
            aliases.update(
                imported.asname or imported.name
                for imported in node.names
                if imported.name == "json_file_storage_factory"
            )
    return aliases


def _is_indexed_factory_call(node: ast.Call, aliases: set[str]) -> bool:
    function = node.func
    is_factory = (isinstance(function, ast.Name) and function.id in aliases) or (
        isinstance(function, ast.Attribute) and function.attr == "json_file_storage_factory"
    )
    if not is_factory:
        return False
    for keyword in node.keywords:
        if keyword.arg == "index":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
    return False


def _assigned_symbol(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> str | None:
    parent = parents.get(node)
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
        return parent.targets[0].id
    if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
        return parent.target.id
    return None


def discover_indexed_storage(root: str | Path) -> list[DiscoveredIndex]:
    """Advisory AST scan for indexed ``json_file_storage_factory`` declarations."""
    source_root = Path(root)
    files = [source_root] if source_root.is_file() else source_root.rglob("*.py")
    discovered: list[DiscoveredIndex] = []
    for path in sorted(files):
        if any(part in _IGNORED_DIRECTORIES for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="UTF-8"), filename=str(path))
        aliases = _factory_aliases(tree)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_indexed_factory_call(node, aliases):
                display_path = str(path.relative_to(source_root)) if source_root.is_dir() else path.name
                discovered.append(
                    DiscoveredIndex(
                        path=display_path,
                        line=node.lineno,
                        symbol=_assigned_symbol(node, parents),
                    )
                )
    return sorted(discovered, key=lambda item: (item.path, item.line))
