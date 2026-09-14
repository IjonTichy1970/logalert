# logalert

Generalized log file watcher.

> **Status:** pre-release. The packaging, CI, and release workflow are in place; the watcher
> itself is being designed. `logalert --version` is the only thing the command does today.

## Installation

logalert is distributed through **[GitHub Releases](https://github.com/IjonTichy1970/logalert/releases)**,
not PyPI — `pip install logalert` will not work. See [INSTALL.md](INSTALL.md) for the
supported procedure.

## Development

Create the venv with a **versioned** interpreter (the oldest supported version is the one to
develop against), then install the package in editable mode with the dev extras:

```bash
python3.12 -m venv .venv            # Windows: py -3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"   # Windows: .venv/Scripts/python.exe
```

The gate — both must be green before anything is committed:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

Changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
