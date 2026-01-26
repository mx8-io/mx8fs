# Agent Instructions

## Project overview
- Environment-agnostic file system library for local, S3, and HTTPS paths.

## Repo map
- `mx8fs/`: library code.
- `tests/`: pytest suites and fixtures.

## Setup
- Use `./setup.sh` as the canonical setup path.

## Required tooling (always run for changed code)
- Format: `black`.
- Lint: `ruff`.
- Type check: `mypy`.
- Tests: `pytest` (targeted where possible, full suite before handoff).
- Use the local virtualenv at `.venv`.
- Activate any local virtualenv using `source .venv/bin/activate` if it exists.
- Pytest may require network access; if pytest fails due to network, request permission and rerun.

## Codacy checks (required)
- Analyze touched files using Codacy MCP when needed:
  - `mcp__codacy__codacy_list_repository_issues`, `mcp__codacy__codacy_cli_analyze`.
- Fix issues found or explain why a fix isn’t possible.

## GitHub workflow

- Prefer GitHub MCP for issues/PRs; fall back to REST only if MCP auth fails.
- Branch naming: `issue-<number>-<short-slug>` (e.g., `issue-860-user-duplicate`).
- PRs: include `Fixes #<number>` in the body for auto-close.
- Default target branch is `dev` unless the user specifies otherwise. Also check the project specific fields as this includes a target branch field.
- For GitHub Projects fields on issues: add to current sprint if not already, set status to **In Progress**, and populate any other project-specific fields if unset.
