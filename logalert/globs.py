"""Glob patterns in ``files``: expansion at run time, and what a glob may match.

A ``files`` entry that contains ``*``, ``?`` or ``[`` is a glob. It is expanded at every run
(never at load time: a daily directory changes between runs), and the state is keyed by each
expanded path, never by the pattern. The rules (decided in issue #18; the ``glob`` facts
measured in the sandbox and on Windows):

  * The shell's rules, per path component (``fnmatch``): ``*`` and ``?`` never cross a
    separator, a name that starts with ``.`` is matched only by a component that starts with
    one, ``[[]`` is a literal bracket, and the platform decides case. ``**`` and a trailing
    separator are refused by the loader: without the recursive flag ``glob`` reads ``**`` as
    ONE level (measured: the top-level files were not matched), with it a glob would walk the
    whole tree; and ``*/`` means directories to the shell, which logalert would pass over.
  * A glob matches REGULAR FILES only, and never through a symbolic link: a link it matches,
    and a link where a wildcard directory component would descend, is passed over -- a link
    is a name the operator never wrote, and a root run following one planted in a watched
    directory would mail a file the pattern never named (reproduced in review). A listed
    path may be a link: the operator wrote it. A directory, FIFO, device or socket the
    pattern names is passed over too (``/var/log/*`` names directories nobody wants read),
    where a listed entry naming one stays an error. Hard links to one file that one glob
    matches are read once, under the first name in sort order. A file whose ``stat`` fails
    otherwise (EACCES) is kept, so that its open fails the way a listed file's does, as a
    failed item.
  * A directory that cannot be listed is a FAILED ITEM of the section, like a file that cannot
    be opened: ``glob.glob`` returns nothing for it, silently (measured as ``nobody`` over a
    0000 directory), and a watch that went quiet because of a permission would look like a
    watch with nothing to say. This module walks the pattern itself, with ``os.scandir``, for
    that one reason. A directory that does not exist, or a component that is not a
    directory, matches nothing; ``listed`` says whether any directory was scanned at all,
    which the run's new-file rule needs.
  * Rotated copies are left out unless the section says ``include_archives = yes``: by SHAPE
    (``rotation.archive_suffix``: a numeric or dated rotation suffix whose base does not end
    in a digit, a copy suffix such as ``.bak`` or ``-old``, or a bare compression extension,
    each with an optional compression extension after it -- ``router.log.1``,
    ``router.log-20260915.gz``, ``messages.bak``, ``messages.gz``; this holds while the live
    file is absent after a ``nocreate`` rotation or a hand-made ``gzip``) and by KIN (a name
    ``rotation.classify`` calls a numeric or dated copy of ANOTHER file matched in the same
    directory, which catches the bases the shape rule declines: ``router1.2`` beside
    ``router1`` -- and, with the name's own extension put back on the base, logrotate's
    ``extension`` form, ``router.1.log`` and ``router-20260916.log.gz`` beside ``router.log``,
    issue #51; a ``rotatelogs`` daily ``access-20260916.log`` is therefore left out only when
    ``access.log`` is matched beside it, and ``worker.2.log`` alone stays a file of its
    own). Otherwise ``/var/log/router*`` would read ``router.log.1.gz`` as a file of
    its own and mail every alert twice after each rotation, and a hand-made copy with a fresh
    mtime would be mailed whole under the run's new-file rule. The ``other`` style is NOT
    kin: ``fw-dmz`` beside ``fw`` and ``router1.example.net`` beside ``router1`` are hosts,
    not copies (reproduced in review), and the catch-up verifies such a copy by inode and
    content where a name alone cannot. ``include_archives = yes`` reads every match as a
    file of its own -- for directories where the dated names ARE the live files (Apache's
    ``rotatelogs``), never for a rotating log, whose renamed copies it would mail twice.
  * The expansion is sorted by name, so a daily directory reads in date order.

What the run does with the expansion -- one read per path however often a section names it,
a failed item per unlistable directory, the new-file rule -- is ``logalert.run``'s.
"""

import fnmatch
import os
import re
import stat
from dataclasses import dataclass

from logalert.cursor import special_kind
from logalert.rotation import archive_suffix, classify

GLOB_CHARS = frozenset("*?[")
_SEPARATORS = "".join(sep for sep in (os.sep, os.altsep) if sep)
_SPLIT = re.compile("([" + re.escape(_SEPARATORS) + "])")


def is_glob(entry: str) -> bool:
    """Whether a ``files`` entry is a pattern rather than a path."""
    return any(char in GLOB_CHARS for char in entry)


@dataclass(frozen=True)
class Expansion:
    """What one glob matched this run."""

    pattern: str
    files: tuple[str, ...]  # the regular files to read, sorted, rotated copies left out
    archives: tuple[str, ...]  # left out as rotated copies (include_archives = no), sorted
    skipped: tuple[tuple[str, str], ...]  # (path, what it is): matched but not a regular file
    errors: tuple[str, ...]  # directories that could not be listed, each with the reason
    listed: bool  # at least one directory was scanned: the pattern led somewhere


def expand(pattern: str, *, include_archives: bool = False) -> Expansion:
    """Expand an absolute glob; see the module docstring for what is and is not matched."""
    drive, rest = os.path.splitdrive(pattern)
    pieces = _SPLIT.split(rest)  # text, separator, text, separator, ...
    if pieces[0]:  # not absolute -- the loader refuses these; expanded as given anyway
        root, steps = drive, [("", pieces[0])]
    else:
        root, steps = drive + pieces[1], []
    steps += [(pieces[i - 1], pieces[i]) for i in range(2, len(pieces), 2) if pieces[i]]
    paths = [root]
    errors: list[str] = []
    scanned = [0]
    for index, (separator, component) in enumerate(steps):
        last = index == len(steps) - 1
        if not is_glob(component):
            paths = [_join(path, separator, component) for path in paths]
            continue
        found: list[str] = []
        for path in paths:
            found += _list(path, separator, component, last, errors, scanned)
        paths = found
    files: list[str] = []
    skipped: list[tuple[str, str]] = []
    identities: dict[tuple[int, int], str] = {}
    for path in sorted(paths):
        try:
            st = os.lstat(path)  # never through a link: see the module docstring
        except (FileNotFoundError, NotADirectoryError):
            if steps and is_glob(steps[-1][1]):
                skipped.append((path, "gone since the listing"))
            continue  # a literal last component with nothing behind it: absent, as listed
        except OSError:
            files.append(path)  # let the open say why, as it would for a listed file
            continue
        if stat.S_ISLNK(st.st_mode):
            skipped.append((path, "a symbolic link; list it by name"))
            continue
        kind = special_kind(st.st_mode)
        if kind is not None:
            skipped.append((path, "a " + kind))
            continue
        identity = (st.st_dev, st.st_ino)
        if st.st_ino and identity in identities:
            skipped.append((path, "another name of " + identities[identity]))
            continue
        identities[identity] = path
        files.append(path)
    archives: list[str] = []
    if not include_archives:
        files, archives = _without_archives(files)
    return Expansion(pattern, tuple(files), tuple(archives), tuple(skipped), tuple(errors),
                     scanned[0] > 0)


def _join(path: str, separator: str, name: str) -> str:
    """Keep the pattern's own separators: the expanded path is the state's key and the
    operator's ``--reset-state`` spelling."""
    if path.endswith(tuple(_SEPARATORS)):
        return path + name  # the root already carries its separator
    return path + separator + name


def _list(path: str, separator: str, component: str, last: bool, errors: list[str],
          scanned: list[int]) -> list[str]:
    """The entries of ``path`` matching one wildcard component; directories only (and not
    links to them) unless it is the last component."""
    try:
        with os.scandir(path) as listing:
            entries = list(listing)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        errors.append(f"cannot list {path} ({exc.strerror or exc})")
        return []
    scanned[0] += 1
    found: list[str] = []
    for entry in entries:
        if entry.name.startswith(".") and not component.startswith("."):
            continue  # hidden, as the shell and glob leave it
        if not fnmatch.fnmatch(entry.name, component):
            continue
        if not last:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
        found.append(_join(path, separator, entry.name))
    return found


def _without_archives(files: list[str]) -> tuple[list[str], list[str]]:
    """Split the matched regular files into the ones to read and the rotated copies: by shape
    (``archive_suffix``) and by kin (``classify`` against another name matched in the same
    directory -- only a prefix of the name ending before a ``.`` or ``-`` can be one, so a
    directory of ten thousand files costs a few lookups per name, not a pass over them)."""
    names: dict[str, set[str]] = {}
    for path in files:
        names.setdefault(os.path.dirname(path), set()).add(os.path.basename(path))
    kept: list[str] = []
    archives: list[str] = []
    for path in files:
        directory, name = os.path.dirname(path), os.path.basename(path)
        if archive_suffix(name) is not None or any(
                base in names[directory] and _rotation_of(name, base)
                for base in _kin_bases(name)):
            archives.append(path)
        else:
            kept.append(path)
    return kept, archives


def _kin_bases(name: str) -> list[str]:
    """The names ``name`` could be a rotation of: the text before each ``.`` or ``-``
    (``router1`` for ``router1.2``) and, for logrotate's ``extension`` form (issue #51), that
    text with the name's own extension put back (``router.log`` for ``router.1.log``). A
    compressed name never reaches the kin rule: the bare compression extension is a
    shape ``archive_suffix`` catches first."""
    own = os.path.splitext(name)[1]
    bases: list[str] = []
    for index in range(1, len(name)):
        if name[index] in ".-":
            bases.append(name[:index])
            if own and index < len(name) - len(own):
                bases.append(name[:index] + own)
    return list(dict.fromkeys(bases))  # a prefix can coincide with an earlier one plus own


def _rotation_of(name: str, base: str) -> bool:
    """Whether ``name`` is a numeric or dated rotation of ``base`` -- never merely ``other``."""
    found = classify(name, base)
    return found is not None and found[0] != "other"
