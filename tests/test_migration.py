"""Tests for indexed storage discovery and migration tooling."""

from __future__ import annotations

import json
import threading
import types
from pathlib import Path
from time import sleep
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine

import mx8fs.cli as cli_module
import mx8fs.indexed_storage as indexed_storage_module
import mx8fs.migration as migration_module
from mx8fs import IndexedJsonFileStorage, json_file_storage_factory, json_index_factory
from mx8fs.index import IndexRebuildResult
from mx8fs.migration import (
    DiscoveredIndex,
    IndexMigrationFailure,
    IndexMigrationReport,
    IndexMigrationResult,
    IndexMigrationTarget,
    discover_indexed_storage,
    load_index_registry,
    migrate_indexes,
)


class MigrationRecord(BaseModel):
    key: str
    status: str


def make_storages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int = 2,
) -> list[IndexedJsonFileStorage[MigrationRecord]]:
    """Create storage instances sharing one physical SQLite index."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite3'}")
    monkeypatch.setattr(indexed_storage_module, "_create_index_engine", lambda _: (engine, None))
    index = json_index_factory(MigrationRecord, ["status"], table_name="migration_records")
    storage_type = json_file_storage_factory("json", MigrationRecord, index=index)
    return [storage_type(str(tmp_path / f"storage-{number}")) for number in range(count)]


def test_registry_loading_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storages(tmp_path, monkeypatch, count=1)[0]
    entries = [
        types.SimpleNamespace(name="targets", load=lambda: lambda: {"MigrationStorage": storage}),
        types.SimpleNamespace(name="not-callable", load=lambda: []),
        types.SimpleNamespace(name="not-mapping", load=lambda: lambda: [storage]),
        types.SimpleNamespace(name="bad-symbol", load=lambda: lambda: {1: storage}),
        types.SimpleNamespace(name="bad-target", load=lambda: lambda: {"MigrationStorage": object()}),
        types.SimpleNamespace(name="duplicate", load=lambda: lambda: {}),
        types.SimpleNamespace(name="duplicate", load=lambda: lambda: {}),
    ]
    monkeypatch.setattr(migration_module, "entry_points", lambda **_: entries)

    assert load_index_registry("targets") == {"MigrationStorage": storage}
    with pytest.raises(ValueError, match="No installed"):
        load_index_registry("missing")
    with pytest.raises(ValueError, match="Multiple installed"):
        load_index_registry("duplicate")
    with pytest.raises(TypeError, match="not callable"):
        load_index_registry("not-callable")
    for name in ["not-mapping", "bad-symbol", "bad-target"]:
        with pytest.raises(TypeError, match="mapping of source symbols"):
            load_index_registry(name)


def test_migrate_indexes_groups_schemas_deduplicates_and_budgets_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = make_storages(tmp_path, monkeypatch)
    schema_calls: list[str] = []
    rebuild_workers: dict[str, int | None] = {}

    monkeypatch.setattr(first, "migrate_schema", lambda: schema_calls.append(first.base_path))
    monkeypatch.setattr(second, "migrate_schema", lambda: pytest.fail("schema migrated twice"))

    def rebuild(storage: IndexedJsonFileStorage[Any], workers: int | None) -> IndexRebuildResult:
        rebuild_workers[storage.base_path] = workers
        return IndexRebuildResult(upserted=1, removed=0)

    monkeypatch.setattr(first, "rebuild_index", lambda max_workers=None: rebuild(first, max_workers))
    monkeypatch.setattr(second, "rebuild_index", lambda max_workers=None: rebuild(second, max_workers))

    report = migrate_indexes([first, first, second], jobs=2, total_read_workers=10)

    assert report.succeeded
    assert len(report.planned) == 2
    assert len(report.results) == 2
    assert not report.failures
    assert schema_calls == [first.base_path]
    assert rebuild_workers == {first.base_path: 5, second.base_path: 5}


def test_migrate_indexes_limits_active_jobs_to_total_read_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storages = make_storages(tmp_path, monkeypatch, count=3)
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    for storage in storages:
        monkeypatch.setattr(storage, "migrate_schema", lambda: None)

        def rebuild(max_workers: int | None = None) -> IndexRebuildResult:
            nonlocal active, maximum_active
            assert max_workers == 1
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            sleep(0.01)
            with lock:
                active -= 1
            return IndexRebuildResult(upserted=1, removed=0)

        monkeypatch.setattr(storage, "rebuild_index", rebuild)

    assert migrate_indexes(storages, jobs=8, total_read_workers=2).succeeded
    assert maximum_active == 2


def test_migrate_indexes_reports_schema_and_namespace_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_storages(tmp_path / "first", monkeypatch, count=1)[0]
    monkeypatch.undo()
    second = make_storages(tmp_path / "second", monkeypatch, count=1)[0]
    monkeypatch.setattr(first, "migrate_schema", lambda: (_ for _ in ()).throw(RuntimeError("schema failed")))
    monkeypatch.setattr(second, "migrate_schema", lambda: None)
    monkeypatch.setattr(second, "rebuild_index", lambda **_: (_ for _ in ()).throw(RuntimeError("rebuild failed")))

    report = migrate_indexes([first, second], jobs=2)

    assert not report.succeeded
    assert [(failure.phase, failure.error) for failure in report.failures] == [
        ("schema", "schema failed"),
        ("namespace", "rebuild failed"),
    ]


def test_migrate_indexes_dry_run_empty_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = make_storages(tmp_path, monkeypatch, count=1)[0]
    monkeypatch.setattr(storage, "migrate_schema", lambda: pytest.fail("dry run migrated"))

    report = migrate_indexes([storage], dry_run=True)
    assert report.dry_run
    assert report.succeeded
    assert len(report.planned) == 1
    assert migrate_indexes([]).succeeded
    assert migration_module._rebuild_namespaces([], 1, 1) == ([], [])
    with pytest.raises(ValueError, match="jobs"):
        migrate_indexes([], jobs=0)
    with pytest.raises(ValueError, match="total_read_workers"):
        migrate_indexes([], total_read_workers=0)
    with pytest.raises(TypeError, match="IndexedJsonFileStorage"):
        migrate_indexes([object()])  # type: ignore[list-item]


def test_discover_indexed_storage_is_advisory_and_alias_aware(tmp_path: Path) -> None:
    source = tmp_path / "storages.py"
    source.write_text(
        """
from mx8fs import json_file_storage_factory as storage_factory
JobStorage = storage_factory("json", Job, index=JobIndex)
PlainStorage = storage_factory("json", Job)
ConfiguredPlainStorage = storage_factory("json", Job, key_field="key")
NoneStorage = storage_factory("json", Job, index=None)
TypedStorage: object = storage_factory("json", Job, index=JobIndex)
mx8fs.json_file_storage_factory("json", Job, index=JobIndex)
something_else()
""",
        encoding="UTF-8",
    )
    ignored = tmp_path / ".venv" / "ignored.py"
    ignored.parent.mkdir()
    ignored.write_text('Ignored = json_file_storage_factory("json", Job, index=JobIndex)', encoding="UTF-8")

    discoveries = discover_indexed_storage(tmp_path)

    assert [(item.path, item.symbol) for item in discoveries] == [
        ("storages.py", "JobStorage"),
        ("storages.py", "TypedStorage"),
        ("storages.py", None),
    ]
    assert discover_indexed_storage(source) == [
        type(discoveries[0])("storages.py", discoveries[0].line, "JobStorage"),
        type(discoveries[1])("storages.py", discoveries[1].line, "TypedStorage"),
        type(discoveries[2])("storages.py", discoveries[2].line, None),
    ]


def test_cli_discovery_and_migration_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = IndexMigrationTarget("Jobs", "/jobs", "jobs_table", "namespace")
    discovery = [DiscoveredIndex(path="app.py", line=4, symbol="Jobs")]
    report = IndexMigrationReport(
        planned=(target,),
        results=(IndexMigrationResult(target, 2, 1, 0.25),),
        failures=(),
        dry_run=False,
    )
    monkeypatch.setattr(cli_module, "discover_indexed_storage", lambda _: discovery)
    registered = type("Jobs", (), {})()
    monkeypatch.setattr(cli_module, "load_index_registry", lambda _: {"Jobs": registered})
    monkeypatch.setattr(cli_module, "migrate_indexes", lambda *_, **__: report)

    assert cli_module.main(["discover-indexes", str(tmp_path)]) == 0
    assert "app.py:4 Jobs" in capsys.readouterr().out
    assert cli_module.main(["discover-indexes", str(tmp_path), "--registry", "application"]) == 0
    assert "Jobs registered" in capsys.readouterr().out
    monkeypatch.setattr(cli_module, "load_index_registry", lambda _: {})
    assert cli_module.main(["discover-indexes", str(tmp_path), "--registry", "application"]) == 0
    assert "Jobs unregistered" in capsys.readouterr().out
    assert cli_module.main(["discover-indexes", str(tmp_path), "--registry", "application", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["registered"] is False
    assert cli_module.main(["migrate-indexes", "application"]) == 0
    assert "Migrated Jobs /jobs" in capsys.readouterr().out
    assert cli_module.main(["migrate-indexes", "application", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["upserted"] == 2


def test_cli_failures_dry_run_and_input_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = IndexMigrationTarget("Jobs", "/jobs", "jobs_table", "namespace")
    failure = IndexMigrationFailure(target, "schema", "broken")
    failed_report = IndexMigrationReport((target,), (), (failure,), False)
    dry_report = IndexMigrationReport((target,), (), (), True)
    monkeypatch.setattr(cli_module, "load_index_registry", lambda _: {})
    monkeypatch.setattr(cli_module, "migrate_indexes", lambda *_, **__: failed_report)

    assert cli_module.main(["migrate-indexes", "app:targets"]) == 1
    assert "during schema: broken" in capsys.readouterr().err
    monkeypatch.setattr(cli_module, "migrate_indexes", lambda *_, **__: dry_report)
    assert cli_module.main(["migrate-indexes", "app:targets", "--dry-run"]) == 0
    assert "Would migrate 1" in capsys.readouterr().out
    monkeypatch.setattr(cli_module, "load_index_registry", lambda _: (_ for _ in ()).throw(ValueError("bad registry")))
    assert cli_module.main(["migrate-indexes", "bad"]) == 2
    assert "bad registry" in capsys.readouterr().err
    assert cli_module._positive_integer("2") == 2
    with pytest.raises(Exception, match="must be positive"):
        cli_module._positive_integer("0")
