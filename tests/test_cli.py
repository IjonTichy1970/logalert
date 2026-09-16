"""Smoke tests for the CLI entry point and version single-sourcing."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from logalert import __version__
from logalert.__main__ import main

NL = chr(10)


def test_version_derives_from_installed_metadata() -> None:
    assert __version__ == version("logalert")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"logalert {__version__}"


def test_no_args_is_the_run_and_a_missing_config_is_exit_2(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # since #12 a bare `logalert` runs; the help is --help
    missing = tmp_path / "none.conf"
    assert main(["-f", str(missing)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"logalert: {missing}: config file not found or not readable" + NL


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
