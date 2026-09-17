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


def test_the_mta_lists_name_exim_and_the_login_limit() -> None:
    """Issue #58: Exim is Debian's default MTA and provides the binary `transport = auto`
    looks for; a Debian operator reading 'Postfix, dma or msmtp-mta' installed a second one.
    And the relay `transport = smtp` reaches must take mail without a login."""
    install = _text("INSTALL.md")
    step = install[install.index("### 6. Mail"):install.index("### 7. Schedule it")]
    assert "Exim" in step and "exim4-daemon-light" in step
    assert "SMTP AUTH" in step and "msmtp-mta" in step
    readme = _text("README.md")
    assert "Exim" in readme and "no login" in readme
    table = install[install.index("## Troubleshooting"):]
    row = next(r for r in table.splitlines() if "Authentication required" in r)
    assert "SMTPSenderRefused: 530" in row and "msmtp-mta" in row and "Exim" in row
    assert "SMTP AUTH is not supported" in example_config()  # the shipped comment, too


def test_the_classifiers_claim_linux_and_nothing_else() -> None:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        classifiers = tomllib.load(handle)["project"]["classifiers"]
    systems = [c for c in classifiers if c.startswith("Operating System ::")]
    assert systems == ["Operating System :: POSIX :: Linux"]


def test_the_python_floor_is_one_number_in_metadata_ci_and_the_documents() -> None:
    """The classifier rule (#53): claim only what CI tests. `requires-python`'s floor, the
    `3.x` classifiers and ci.yml's matrix are one set, and README.md, INSTALL.md and CLAUDE.md
    name that floor -- a leg dropped from the matrix or a floor bumped alone reddens here."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)
    floor = re.fullmatch(r">=3\.(\d+)", project["project"]["requires-python"])
    assert floor is not None, project["project"]["requires-python"]
    versions = sorted(int(c.rsplit(".", 1)[1]) for c in project["project"]["classifiers"]
                      if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c))
    assert versions and versions[0] == int(floor.group(1)), (versions, floor.group(0))
    assert versions == list(range(versions[0], versions[-1] + 1)), versions  # no gap
    matrix = re.search(r'python-version: \[([^\]]+)\]', _text(".github/workflows/ci.yml"))
    assert matrix is not None, "the matrix line moved"
    legs = sorted(int(v.strip().strip('"').split(".")[1]) for v in matrix.group(1).split(","))
    assert legs == versions, (legs, versions)
    said = f"3.{versions[0]} or newer"
    for name in ("README.md", "INSTALL.md"):
        text = _text(name)
        assert said in text, f"{name} does not say {said!r}"
        stale = re.search(r"3\.\d+ or newer", text.replace(said, ""))
        assert stale is None, (name, stale and stale.group(0))
        # a claim or an example command naming a version BELOW the floor is always stale
        # (a bare `python3.7` in prose about an old distribution is not a claim and is left alone)
        for token in re.finditer(r"(?:python3\.(\d+) -m venv|py -3\.(\d+) -m venv"
                                 r"|3\.(\d+)-or-newer|[Ff]loor (?:is |\()3\.(\d+))", text):
            minor = int(next(g for g in token.groups() if g))
            assert minor >= versions[0], (name, token.group(0), f"floor is 3.{versions[0]}")
    assert f"currently 3.{versions[0]}" in _text("CLAUDE.md"), "CLAUDE.md's Versioning bullet"


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
