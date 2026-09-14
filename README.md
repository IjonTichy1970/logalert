# logalert

Generalized log file watcher.

> **Status:** pre-release. The packaging, CI, gate and release workflow are in
> place; the watcher itself is being designed. `logalert --version` is the only
> thing the command does today.

## Installation

logalert is distributed through **[GitHub
Releases](https://github.com/IjonTichy1970/logalert/releases)**, not PyPI --
`pip install logalert` will not work. See [INSTALL.md](INSTALL.md) for the
supported procedure.

## Development

Developed on Windows, run on Linux. Create the venv with a **versioned**
interpreter (the oldest supported version is the one to develop against), then
install the package in editable mode with the dev extras:

```bash
python3.12 -m venv .venv            # Windows: py -3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"   # Windows: .venv/Scripts/python.exe
```

The gate is one file, `tools/gate.sh`, and it is the only definition of green.
Run it with the venv's interpreter first on PATH and read its exit code directly
(never through a pipe):

```bash
export PATH="$PWD/.venv/bin:$PATH"   # Windows Git Bash: $PWD/.venv/Scripts
bash tools/gate.sh > gate.out 2>&1; rc=$?
```

On the Windows dev host the Linux-only stage runs inside a WSL sandbox; see
`CLAUDE.md` for the sandbox rules. Changes are tracked in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT -- see [LICENSE](LICENSE).
