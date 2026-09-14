"""logalert -- generalized log file watcher."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("logalert")
except PackageNotFoundError:  # source tree without an install
    __version__ = "0.0.0+unknown"
