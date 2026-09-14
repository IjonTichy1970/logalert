"""Smoke tests for the CLI entry point and version single-sourcing."""

import subprocess
import sys
from importlib.metadata import version

import pytest

from logalert import __version__
from logalert.__main__ import main


def test_version_derives_from_installed_metadata():
    assert __version__ == version("logalert")


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"logalert {__version__}"


def test_no_args_prints_help(capsys):
    assert main([]) == 0
    assert "usage: logalert" in capsys.readouterr().out


def test_module_is_runnable():
    result = subprocess.run(
        [sys.executable, "-m", "logalert", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"logalert {__version__}"
