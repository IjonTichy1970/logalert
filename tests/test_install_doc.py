"""README.md and INSTALL.md against the package (issue #16): the README's configuration
example loads, the systemd unit block is one text in INSTALL.md and docs/USAGE.md, every
relative link resolves to a file in the tree, and the Linux classifier is claimed. Pure text:
real on both platforms."""

import re
import tomllib
from pathlib import Path

from logalert.config import example_config, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
NL = chr(10)


def _text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _fences(text: str, info: str) -> list[str]:
    """The bodies of the fenced blocks whose info string is ``info`` (``""`` for a bare fence),
    scanned line by line: a regex keyed on the opening fence would take a closing one for it."""
    bodies: list[str] = []
    inside: str | None = None
    body: list[str] = []
    for line in text.split(NL):
        if line.startswith("```"):
            if inside is None:
                inside, body = line[3:].strip(), []
            else:
                if inside == info:
                    bodies.append(NL.join(body) + NL)
                inside = None
        elif inside is not None:
            body.append(line)
    return bodies


def test_the_readme_config_example_loads_and_uses_only_keys_the_example_documents(
    tmp_path: Path,
) -> None:
    (example,) = _fences(_text("README.md"), "ini")
    assert example.count(NL) == 10  # ten lines, as the issue asked
    path = tmp_path / "logalert.conf"
    path.write_text(example.replace("/var/log/router.log", (tmp_path / "router.log").as_posix()),
                    encoding="utf-8", newline=NL)
    config = load_config(str(path))
    assert [w.name for w in config.watches] == ["router-disk"] and config.warnings == ()
    assert config.settings.from_address == "logalert@example.net"
    shipped = example_config()
    for key in re.findall(r"^(\w+) =", example, re.M):
        assert re.search(rf"^#?{key} =", shipped, re.M), f"{key} is not in --example-config"


def test_the_systemd_units_are_one_text_in_install_and_usage() -> None:
    install = [f for f in _fences(_text("INSTALL.md"), "") if "logalert.service" in f]
    usage = [f for f in _fences(_text("docs/USAGE.md"), "") if "logalert.service" in f]
    assert len(install) == 1 and len(usage) == 1
    assert install[0] == usage[0]
    assert "ExecStart=/usr/local/bin/logalert" in install[0]  # absolute, the symlink
    assert "Type=oneshot" in install[0] and "User=logalert" in install[0]


def test_the_cron_line_is_one_text_in_readme_install_and_usage() -> None:
    lines = {name: [f for f in _fences(_text(name), "") if "MAILTO=" in f]
             for name in ("README.md", "INSTALL.md", "docs/USAGE.md")}
    assert all(len(found) == 1 for found in lines.values()), lines
    assert len({found[0] for found in lines.values()}) == 1
    assert "*/5 * * * * /usr/local/bin/logalert" in lines["README.md"][0]


def test_every_relative_link_in_the_two_documents_resolves() -> None:
    for name in ("README.md", "INSTALL.md"):
        base = (REPO_ROOT / name).parent
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", _text(name)):
            if target.startswith(("http://", "https://")):
                continue
            assert (base / target).is_file(), f"{name}: {target} does not exist"


def test_the_readme_names_the_documents_and_the_site() -> None:
    readme = _text("README.md")
    for pointer in ("docs/USAGE.md", "INSTALL.md", "CHANGELOG.md",
                    "https://ijontichy1970.github.io/logalert/"):
        assert pointer in readme, pointer
    assert "pip install logalert" in readme and "GitHub" in readme  # not on PyPI, said plainly


def test_the_classifiers_claim_linux_and_nothing_else() -> None:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        classifiers = tomllib.load(handle)["project"]["classifiers"]
    systems = [c for c in classifiers if c.startswith("Operating System ::")]
    assert systems == ["Operating System :: POSIX :: Linux"]


def test_the_minimum_watch_install_names_loads(tmp_path: Path) -> None:
    """INSTALL.md's own statement of the smallest watch: a section built from exactly the
    keys that sentence names must load (the review found `subject` missing from it)."""
    install = _text("INSTALL.md")
    sentence = re.search(r"at least one watch section with (.+?), and `from = `", install,
                         re.S)
    assert sentence is not None, "the minimum-watch sentence moved"
    keys = re.findall(r"`(\w+)`", sentence.group(1))
    assert "a pattern" in sentence.group(1) and keys, sentence.group(1)
    log = tmp_path / "router.log"
    log.write_text("up" + NL, encoding="utf-8", newline=NL)
    values = {"subject": "Router disk failure", "to": "noc@example.net", "files": log.as_posix()}
    body = "".join(f"{key} = {values[key]}" + NL for key in keys)
    path = tmp_path / "logalert.conf"
    path.write_text("[router-disk]" + NL + body + "patterns = disk failure" + NL,
                    encoding="utf-8", newline=NL)
    (watch,) = load_config(str(path)).watches
    assert watch.name == "router-disk"


def test_the_dev_venv_extras_are_spelled_alike_everywhere() -> None:
    """One command, four documents: README.md, INSTALL.md, CLAUDE.md and ci.yml install the
    same extras (the review found `.[dev]` surviving in three of them after `docs` arrived)."""
    seen: dict[str, set[str]] = {}
    for name in ("README.md", "INSTALL.md", "CLAUDE.md", ".github/workflows/ci.yml",
                 "tools/gate.sh"):
        found = set(re.findall(r'pip install -e "(\.\[[a-z,]+\])"', _text(name)))
        assert found, f"{name} does not spell the editable install"
        seen[name] = found
    assert all(found == {".[dev,docs]"} for found in seen.values()), seen
