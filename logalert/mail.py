"""Alert email composition: one message per section per run, 7-bit clean on the wire.

This module is the single home of the message's shape; ``logalert.transport`` (issue #11) hands
the bytes over and ``logalert.__main__`` (issue #12) decides when. The rules (decided in issue
#10, measured on 3.12 on both platforms):

  * ``EmailMessage`` under ``email.policy.default.clone(cte_type="7bit")``: ``set_content``
    picks ``7bit`` when every line is ASCII and at most 78 characters, else quoted-printable
    or base64 -- so no wire line ever exceeds 78 bytes, whatever a log line holds. Never
    ``8bit``: nothing folds a 998-byte line and dma rejects it. ``max_line_length`` stays at
    78: at 998 a non-ASCII subject folded into one 280-character encoded-word (RFC 2047 caps
    them at 75).
  * Headers, in this order and all before the content (a header added after ``set_content``
    lands after the MIME headers): From, To, Subject, Date, Message-ID, Auto-Submitted
    (RFC 3834: no vacation replies), X-Prepared-By, X-Logalert-Section; then ONLY when the
    mail has a priority (the highest across the section's reports; the system is off unless
    the section or a tag set one): X-Logalert-Priority, X-Priority (1/3/5) and Importance
    (high/normal/low), which stock mail clients render as a flag.
  * Subject: the section's ``subject``, then `` -- N match(es)`` unless ``subject_suffix = no``
    -- the literal ``match(es)`` the example config documents, so a filter can key on it.
  * The body opens with a summary in both modes: what matched where, on which host, when, the
    files read with their counts, the Message-ID (the activity log carries the same one, so an
    alert can be traced to a run). ``report = inline`` continues with the report; ``report =
    attachment`` (or ``--attach``) attaches it as ``text/plain`` named
    ``<section>-<YYYYmmddTHHMM>.txt`` with the section name reduced to ``[A-Za-z0-9._-]``.
  * The report is ``grep -n -C``'s vocabulary, as measured for issue #9: a ``tail``-style
    header per physical file, ``N: text`` for a match, ``N- text`` for context, ``--`` for
    omitted lines. The fragments of a cut line are one report line. At most ``max_lines``
    matching lines per email, each with its context, then ``... and N more matching line(s)``.
  * Sanitised before composition: a lone surrogate becomes ``?`` (``set_content`` raises on
    one), CR is removed (a bare CR is a line break to the encoder), every other C0 control
    except TAB, DEL and the C1 range (a raw CSI is live on a terminal that honours C1)
    become U+FFFD (a NUL rides through ``7bit`` to the relay otherwise).
    A header value further folds TAB and every line boundary ``str.splitlines()`` knows into
    a space: the loader forbids CR and LF there, but NEL, LS and PS pass it as one line and
    are exactly what ``EmailMessage`` refuses (measured: a ``ValueError`` on every run for
    that section). A subject shaped like an encoded-word (``=?utf-8?q?...?=``) is decoded by
    the header parser; that literal form is not preserved.
  * The flattened bytes are produced once per transport and cached: LF for the sendmail pipe,
    CRLF (``email.policy.SMTP``) for SMTP DATA -- one byte per line apart -- and ``len()`` of
    the one handed over is the size the activity log reports.
  * The From must be the bare ``local@domain`` the loader accepts (``config.is_address``):
    anything else is refused with a ``ValueError`` naming the fix, before a header is
    written. Measured: the header parser of Python 3.12.3 (Ubuntu 24.04's) raises
    ``IndexError`` on ``x@`` where 3.12.10 renders ``<>`` -- a tolerant path would depend on
    the patch level. So the Message-ID domain is always the From's and well-formed, equal in
    the header, the body and the log.
  * No name lookup, ever: the msg-id domain comes from the From, the host in the summary is
    ``gethostname()``; the effective From is the caller's to resolve.
  * ``compose_test`` (``--test-mail``, issue #11) carries the same headers minus the priority
    trio, the subject ``logalert test: <section subject>`` (never the match count) and a
    one-paragraph body naming the version, host, time, section, recipients and Message-ID.
"""

import email.policy
import email.utils
import re
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Literal

from logalert import __version__
from logalert.config import RESERVED_SECTION, Priority, Settings, Watch, is_address
from logalert.match import Entry, FileReport, highest

Transport = Literal["sendmail", "smtp"]

POLICY = email.policy.default.clone(cte_type="7bit")
_POLICIES = {
    "sendmail": POLICY,  # LF: the bytes piped to sendmail -i
    "smtp": email.policy.SMTP.clone(cte_type="7bit"),  # CRLF: the bytes DATA carries
}

X_PRIORITY: dict[str, str] = {"high": "1", "medium": "3", "low": "5"}
IMPORTANCE: dict[str, str] = {"high": "high", "medium": "normal", "low": "low"}

NL = chr(10)
_CR = chr(13)
_TAB = chr(9)
_REPLACEMENT = chr(0xFFFD)
# C0 controls other than TAB and LF, DEL, and the C1 range (a CSI a terminal honours)
# except NEL, which is a line boundary clean_header folds; built from code points
# (gated code is ASCII)
_CONTROLS = re.compile("[" + "".join(chr(c) for c in range(0x20) if c not in (9, 10))
                       + "".join(chr(c) for c in range(0x7F, 0xA0) if c != 0x85) + "]")
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_CAP = 40  # of the section's characters: the name must fit one Content-Disposition line
#                 (measured: at 48 the stdlib folds it into RFC 2231 continuations)
_INDENT = 12  # the summary's label column: "Message-ID: " is the widest label


def clean_text(text: str) -> str:
    """A body line as it may travel: UTF-8-encodable, no CR, no control other than TAB."""
    text = text.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate would raise
    return _CONTROLS.sub(_REPLACEMENT, text.replace(_CR, ""))


def clean_header(value: str) -> str:
    """A header value: as clean_text, with TAB and every line boundary ``str.splitlines()``
    knows (LF, and NEL / LS / PS, which the loader passes as one line) as a space -- that is
    the check ``EmailMessage`` applies to a header value -- and the ends stripped."""
    return " ".join(clean_text(value).replace(_TAB, " ").splitlines()).strip()


def attachment_name(section: str, now: datetime) -> str:
    """``<section>-<YYYYmmddTHHMM>.txt``, one plain token on every client: the section name
    reduced to ``[A-Za-z0-9._-]``, never starting with ``-`` (an option to a shell tool in
    the download directory) or ``.`` (hidden), at most ``_NAME_CAP`` characters."""
    stem = _UNSAFE_IN_NAME.sub("_", section).lstrip("-.")[:_NAME_CAP] or "section"
    return f"{stem}-{now:%Y%m%dT%H%M}.txt"


@dataclass(frozen=True)
class Mail:
    """One composed alert and what the run loop and the transport need to know about it.

    The bytes are cached per transport: a header added to ``message`` after a ``flatten``
    never reaches that transport's wire."""

    message: EmailMessage
    message_id: str
    sender: str  # the From as written to the header: the envelope sender too
    subject: str
    recipients: tuple[str, ...]
    priority: Priority | None
    matched: int  # matching lines across the section's files
    _wire: dict[str, bytes] = field(default_factory=dict, repr=False, compare=False)

    def flatten(self, transport: Transport) -> bytes:
        """The exact bytes for the transport, produced once; ``len()`` is the logged size."""
        if transport not in self._wire:
            self._wire[transport] = self.message.as_bytes(policy=_POLICIES[transport])
        return self._wire[transport]

    def preview(self) -> str:
        """The message as ``--dry-run`` shows it: the headers decoded, the body as a reader
        sees it, then each attachment's text under its name -- never the wire."""
        head = NL.join(f"{name}: {value}" for name, value in self.message.items())
        body = self.message.get_body(("plain",))
        parts = [head, str(body.get_content()) if body is not None else ""]
        for part in self.message.iter_attachments():
            parts.append(f"==== {part.get_filename()} ====" + NL + str(part.get_content()))
        return (NL + NL).join(parts)


def compose(watch: Watch, reports: Sequence[FileReport], *, sender: str, settings: Settings,
            now: datetime | None = None, host: str | None = None, attach: bool = False) -> Mail:
    """The section's alert for this run.

    ``reports`` are the section's files in config order (each from ``match.scan`` with
    ``cap=watch.max_lines``); ``sender`` is the effective From address, already resolved,
    in the bare shape ``config.is_address`` accepts (else ``ValueError``);
    ``now`` is an aware local datetime -- one instant for Date, the summary and the attachment
    name -- and ``host`` the name in the summary (neither is looked up here); ``attach``
    forces the attachment mode for this run.
    """
    now, host = _when_where(now, host)
    priority = highest(report.priority for report in reports)
    matched = sum(report.matched for report in reports)
    subject = clean_header(watch.subject)
    if settings.subject_suffix:
        subject = f"{subject} -- {matched} match(es)"
    msg, sender, message_id = _stamp(watch, sender=sender, subject=subject, now=now,
                                     priority=priority)
    name = attachment_name(watch.name, now)
    summary = _summary(watch, reports, priority=priority, matched=matched,
                       host=clean_header(host), now=now, message_id=message_id)
    report = render(reports, watch.max_lines)
    if attach or watch.report == "attachment":
        msg.set_content(summary + NL + f"The report is attached as {name}." + NL)
        msg.add_attachment(report, filename=name)
    else:
        msg.set_content(summary + NL + report)
    return Mail(msg, message_id, sender, subject, watch.to, priority, matched)


def compose_test(watch: Watch, *, sender: str, settings: Settings,
                 now: datetime | None = None, host: str | None = None) -> Mail:
    """The one-line test message ``--test-mail SECTION`` sends to the section's recipients."""
    now, host = _when_where(now, host)
    subject = f"logalert test: {clean_header(watch.subject)}"
    msg, sender, message_id = _stamp(watch, sender=sender, subject=subject, now=now,
                                     priority=None)
    recipients = ", ".join(clean_header(address) for address in watch.to)
    msg.set_content(
        f"This is a test message from logalert {__version__} on {clean_header(host)} at "
        f"{now:%Y-%m-%d %H:%M:%S %z} for the section [{clean_header(watch.name)}], sent to "
        f"{recipients} through the configured transport. Its Message-ID is {message_id}. "
        f"Alerts for this section arrive with the subject {clean_header(watch.subject)!r}."
        + NL)
    return Mail(msg, message_id, sender, subject, watch.to, None, 0)


def _when_where(now: datetime | None, host: str | None) -> tuple[datetime, str]:
    if now is None:
        now = email.utils.localtime()
    if host is None:
        host = socket.gethostname()  # never getfqdn(): a resolver is nothing to wait for
    return now, host


def _stamp(watch: Watch, *, sender: str, subject: str, now: datetime,
           priority: Priority | None) -> tuple[EmailMessage, str, str]:
    """A message with every header in the documented order and nothing else yet: returns
    it with the sanitised sender and the Message-ID."""
    sender = clean_header(sender)
    if not is_address(sender):
        raise ValueError(f"From address {sender!r} is not a bare local@domain address; set "
                         f"from = in [{RESERVED_SECTION}]")
    message_id = email.utils.make_msgid(domain=sender.rpartition("@")[2])  # no lookup
    msg = EmailMessage(policy=POLICY)
    msg["From"] = sender
    msg["To"] = ", ".join(clean_header(address) for address in watch.to)
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(now)
    msg["Message-ID"] = message_id
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Prepared-By"] = f"logalert {__version__}"
    msg["X-Logalert-Section"] = clean_header(watch.name)
    if priority is not None:
        msg["X-Logalert-Priority"] = priority
        msg["X-Priority"] = X_PRIORITY[priority]
        msg["Importance"] = IMPORTANCE[priority]
    return msg, sender, message_id


def _summary(watch: Watch, reports: Sequence[FileReport], *, priority: Priority | None,
             matched: int, host: str, now: datetime, message_id: str) -> str:
    """Why the mail was sent, identical in both modes."""
    subject = clean_header(watch.subject)
    lines = [f"{subject}: {matched} match(es) on {host} at {now:%Y-%m-%d %H:%M:%S %z}.", ""]
    rows: list[tuple[str, list[str]]] = [("Section", [clean_header(watch.name)])]
    if priority is not None:
        rows.append(("Priority", [priority]))
    files = []
    for report in reports:
        found = f"{report.matched} match(es)" if report.matched else "no match"
        row = f"{clean_header(report.file)} -- {found} in {report.lines} line(s) read"
        if report.excluded:
            row += f", {report.excluded} dropped by an exclude"
        files.append(row)
    rows.append(("Files", files))
    rows.append(("Message-ID", [message_id]))
    for label, values in rows:
        for i, value in enumerate(values):
            lines.append((f"{label}:" if i == 0 else "").ljust(_INDENT) + value)
    return NL.join(lines) + NL


@dataclass(frozen=True)
class _ReportLine:
    """One physical line of the report: the fragments of a cut line joined."""

    kind: str
    number: int
    path: str
    text: str
    cut: bool  # still cut after the last fragment kept


def _physical(entries: Sequence[Entry]) -> Iterator[_ReportLine]:
    fragments: list[Entry] = []
    for entry in entries:
        if fragments and (entry.kind, entry.number, entry.path) != (
                fragments[0].kind, fragments[0].number, fragments[0].path):
            yield _join(fragments)
            fragments = []
        fragments.append(entry)
        if entry.kind == "gap":  # a gap is its own line, never joined with the next one
            yield _join(fragments)
            fragments = []
    if fragments:
        yield _join(fragments)


def _join(fragments: list[Entry]) -> _ReportLine:
    head, tail = fragments[0], fragments[-1]
    return _ReportLine(head.kind, head.number, head.path,
                       "".join(clean_text(f.text) for f in fragments), tail.cut)


def render(reports: Sequence[FileReport], max_lines: int) -> str:
    """The plain-text report: at most ``max_lines`` matching lines across the files, in order,
    each with its context; then the trailer with the exact remainder. After the last
    budgeted match only its own after-window follows (the next match's before-window
    would show lines of a match the reader never sees); a gap inside that window is a
    NUL-only line the reader skipped, and the window runs on past it, but a change of
    physical file closes it, as it closes the scan's."""
    out: list[str] = []
    shown = 0
    total = sum(report.matched for report in reports)
    done = False
    for report in reports:
        if done:
            break
        path: str | None = None
        gap = False
        trailing: int | None = None  # context lines still due after the last budgeted match
        for line in _physical(report.entries):
            if line.kind == "gap":
                if trailing == 0:
                    done = True  # the window closed
                    break
                gap = True
                continue
            if line.kind == "match":
                if trailing is not None or shown >= max_lines:
                    done = True
                    break
                shown += 1
                if shown >= max_lines:
                    trailing = report.context
            elif trailing is not None:
                if trailing == 0 or line.path != path:
                    done = True  # the window is spent, or closed with the file
                    break
                trailing -= 1
            if line.path != path:
                if out:
                    out.append("")
                out.append(f"==> {clean_text(line.path)} <==")  # the header is the discontinuity
                path = line.path
            elif gap:
                out.append("--")
            gap = False
            marker = ":" if line.kind == "match" else "-"
            out.append(f"{line.number}{marker} {line.text}{' [cut]' if line.cut else ''}")
        if trailing is not None:
            done = True  # the budget is spent; the next file's lines belong to unseen matches
    if total > shown:
        out.append(f"... and {total - shown} more matching line(s)")
    return NL.join(out) + NL if out else ""
