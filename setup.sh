#!/bin/bash
set -e

echo -e "\033[1;36m===============================================\033[0m"
echo -e "\033[1;32m  Setting up the development environment...  \033[0m"
echo -e "\033[1;36m===============================================\033[0m"

poetry lock
poetry install
poetry run pre-commit install
pre-commit autoupdate
poetry run pytest tests
poetry run pre-commit run --all-files

echo -e "\033[1;32m  Development environment setup complete!  \033[0m"
echo -e "\033[1;36m===============================================\033[0m"
echo -e "\033[1;32m  To run tests, use:                         \033[0m"
echo -e "\033[1;32m  pytest tests                               \033[0m"
echo -e "\033[1;36m===============================================\033[0m"
