"""logalert -- generalized log file watcher."""

import logging
from importlib.metadata import PackageNotFoundError, version

# The library convention: without it, a WARNING logged before the activity log is wired
# (issue #13) reaches stderr through logging.lastResort -- measured, --test-mail printed a
# refused recipient twice from a real console while the suite saw one line.
logging.getLogger("logalert").addHandler(logging.NullHandler())

try:
    __version__ = version("logalert")
except PackageNotFoundError:  # source tree without an install
    __version__ = "0.0.0+unknown"
