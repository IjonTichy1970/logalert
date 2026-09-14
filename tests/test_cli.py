"""Smoke tests for the CLI entry point and version single-sourcing."""

import subprocess
import sys
from importlib.metadata import version

import pytest

from logalert import __version__
from logalert.__main__ import main


def test_version_derives_from_installed_metadata() -> None:
    assert __version__ == version("logalert")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"logalert {__version__}"


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: logalert" in capsys.readouterr().out


def test_module_is_runnable() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "logalert", "--version"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"logalert {__version__}"
