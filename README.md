# MX8 File system

This library provides environment agnostic file system access across local and AWS, including:
- File / IO
- List / Glob
- Locking
- Caching
- Comparing Dictionaries

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
