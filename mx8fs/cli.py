"""Command-line interface for indexed JSON discovery and migration."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from .migration import discover_indexed_storage, load_index_registry, migrate_indexes


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mx8fs")
    subcommands = parser.add_subparsers(dest="command", required=True)

    discover = subcommands.add_parser("discover-indexes", help="advisory scan for indexed storage declarations")
    discover.add_argument("root")
    discover.add_argument("--registry", help="optional installed mx8fs registry entry point to compare")
    discover.add_argument("--json", action="store_true", dest="json_output")

    migrate = subcommands.add_parser("migrate-indexes", help="migrate registered indexed storage namespaces")
    migrate.add_argument("registry", help="installed mx8fs registry entry point")
    migrate.add_argument("--jobs", type=_positive_integer, default=4)
    migrate.add_argument("--total-read-workers", type=_positive_integer, default=100)
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _run_discovery(arguments: argparse.Namespace) -> int:
    discoveries = discover_indexed_storage(arguments.root)
    registered_types: set[str] = set()
    if arguments.registry:
        registered_types = set(load_index_registry(arguments.registry))
    rows = [
        {
            **asdict(discovery),
            "registered": discovery.symbol in registered_types if arguments.registry else None,
        }
        for discovery in discoveries
    ]
    if arguments.json_output:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            status = ""
            if row["registered"] is not None:
                status = " registered" if row["registered"] else " unregistered"
            symbol = row["symbol"] or "<unassigned>"
            print(f"{row['path']}:{row['line']} {symbol}{status}")
        print(f"Discovered {len(rows)} indexed storage declaration(s)")
    return 0


def _run_migration(arguments: argparse.Namespace) -> int:
    storages = load_index_registry(arguments.registry)
    report = migrate_indexes(
        storages.values(),
        jobs=arguments.jobs,
        total_read_workers=arguments.total_read_workers,
        dry_run=arguments.dry_run,
    )
    if arguments.json_output:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        action = "Would migrate" if report.dry_run else "Planned"
        print(f"{action} {len(report.planned)} indexed storage namespace(s)")
        for result in report.results:
            print(
                f"Migrated {result.target.storage_type} {result.target.base_path}: "
                f"{result.upserted} indexed, {result.removed} removed in {result.seconds:.2f}s"
            )
        for failure in report.failures:
            print(
                f"Failed {failure.target.storage_type} {failure.target.base_path} "
                f"during {failure.phase}: {failure.error}",
                file=sys.stderr,
            )
    return 0 if report.succeeded else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the mx8fs command-line interface."""
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "discover-indexes":
            return _run_discovery(arguments)
        return _run_migration(arguments)
    except (ImportError, AttributeError, TypeError, ValueError, OSError, SyntaxError) as error:
        print(f"mx8fs: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
