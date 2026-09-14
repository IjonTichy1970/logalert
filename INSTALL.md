# Installing logalert

logalert is published as a Python wheel on the project's
[GitHub Releases](https://github.com/IjonTichy1970/logalert/releases) page. It is **not on
PyPI**: `pip install logalert` fails with `No matching distribution found` — download the wheel
from a release instead.

## Requirements

- Python **3.12 or newer**.
- `pip` and the `venv` module (Debian/Ubuntu: `apt install python3.12-venv`).

## Install into a virtual environment

1. Create the venv with a **specific** interpreter version. `python3.12` below is an example —
   the rule is to name *a* version, never bare `python3`:

   ```bash
   python3.12 -m venv /opt/logalert          # Windows: py -3.12 -m venv C:\logalert
   ```

   A venv is bound to the Python minor version that created it. A venv created with bare
   `python3` breaks silently with `ModuleNotFoundError` the day an OS upgrade re-points
   `python3` at a newer minor, even though nothing inside the venv changed.

2. Download `logalert-X.Y.Z-py3-none-any.whl` from the release you want and install it:

   ```bash
   /opt/logalert/bin/pip install ./logalert-X.Y.Z-py3-none-any.whl
   ```

   The `py3-none-any` wheel is pure Python — one file covers every supported Python version
   and operating system.

3. Verify:

   ```bash
   /opt/logalert/bin/logalert --version
   ```

## Upgrading

Download the new wheel and `pip install` it into the same venv; pip replaces the old version.

## Moving the venv to a newer Python

`python3.13 -m venv --upgrade /opt/logalert` re-points the venv's interpreter but does **not**
migrate installed packages — reinstall the wheel afterwards. Never `rm -rf` or `venv --clear`
a venv directory that also holds operator files (configs, state, logs kept alongside it) without
backing those up first.

## Troubleshooting

- **`No matching distribution found for logalert`** — you ran `pip install logalert`. Install
  from the downloaded wheel file instead (step 2 above).
- **`ModuleNotFoundError` after an OS upgrade** — the venv's Python minor version is gone.
  Recreate the venv with a versioned interpreter and reinstall the wheel.
