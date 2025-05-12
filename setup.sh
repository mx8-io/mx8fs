#!/bin/bash
poetry lock
poetry install
poetry run pre-commit install
poetry run pytest tests
poetry run pre-commit run --all-files
