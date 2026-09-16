"""Mail delivery: the sendmail pipe and SMTP, with a timeout on everything that can hang.

This module is the single home of the transport rules; ``logalert.mail`` composes, the run
loop (issue #12) decides when to call ``deliver`` and what its answer means for the state.
The rules (decided in issue #11, measured against dma 0.13 and against an ESMTP stub on both
platforms):

  * ``transport = auto`` (the default) is sendmail when ``sendmail_path`` is a regular file
    that is executable (``config.sendmail_problem`` is the one judgement ``--check-config``
    and this module share), else a configuration error that says what to install or
    configure -- a fresh Ubuntu server image has no ``/usr/sbin/sendmail`` at all. The same
    error shape for ``transport = sendmail`` naming a binary that is absent, a directory or
    not executable.
  * sendmail: argv ``[sendmail_path, "-i", "-f", <envelope From>, <recipient>...]``. ``-i`` is
    spelled so and never ``-oi``: measured, ``-oi`` is a no-op in dma 0.13 despite its man
    page, and without the flag a body line that is a single ``.`` ends the message silently
    with exit 0. No ``-t`` (a stray header would widen the recipient list) and no ``--``
    (not every sendmail honours it; the loader's leading-``-`` rule on addresses is the
    guard: a ``-bp`` recipient would print the queue and discard the alert with exit 0).
    The message goes in as the exact LF bytes (``Mail.flatten("sendmail")``); the child is
    bounded by ``mail_timeout`` and on POSIX runs in its own process group so a forking
    wrapper (``sudo``, ``runuser``, a shell script without ``exec``) dies with its children
    when the time is up -- a wrapper that ``setsid``s itself still escapes, and a message
    the MTA had already read may still be queued; the operator docs say so. Exit 0 means
    ACCEPTED FOR QUEUEING, never delivered (dma returns it in 20 ms, before any DNS;
    ``mailq`` is the tell), and whatever the MTA wrote to stderr on exit 0 -- Postfix's
    ``postdrop: warning: unable to look up public/pickup`` with the daemon stopped is that
    shape -- is kept in the answer and logged as a WARNING. A non-zero exit is named by its
    sysexits code where it has one (dma never returns 75: its temporary failures are exit 0
    plus a queue entry; Postfix's postdrop is where 75 comes from); an ``OSError`` from
    starting the child names the path ourselves (on Windows ``exc.filename`` is None) and
    says when the file exists but its ``#!`` interpreter does not.
  * smtp: ``smtplib.SMTP(host, port, local_hostname=<the From's host>, timeout=mail_timeout)``
    -- without ``local_hostname`` smtplib runs its own ``getfqdn()``, which stalled 20 s on a
    host with dead DNS; ``smtp_starttls = yes`` calls ``starttls()`` with a default SSL
    context (which verifies the certificate against ``smtp_host`` -- a name, not an IP) and
    lets ``SMTPNotSupportedError`` be the loud failure; the message is the exact CRLF bytes
    (``Mail.flatten("smtp")``) through the low-level ``sendmail()``, which adds one byte per
    line starting with ``.`` and nothing else, and whose DATA reply (a relay's queue id) it
    discards -- the Message-ID is the correlation key. One refused recipient is a
    per-recipient failure with the others delivered; every recipient refused, a session the
    server closed at RCPT (the recipients it never tried are named as such), a refused
    sender, a rejected DATA, a dropped connection, a silent server and a refused connection
    are ``DeliveryError``. The farewell is ours, not the context manager's: a QUIT answered
    with anything but 221 after the DATA 250 can never un-accept a message, so it is logged
    at DEBUG and the delivery stands. Every failure is caught as ``OSError``:
    ``SMTPException``, ``ssl.SSLError``, ``ConnectionError`` and ``TimeoutError`` are all
    subclasses, and the STARTTLS-without-handshake failures surface as raw socket and SSL
    errors whose type differs per platform. ``timeout`` bounds each socket operation, not
    the session (a session is about eight of them, so a slow relay can hold a run for up to
    eight times ``mail_timeout`` and still succeed) and not the resolution of ``smtp_host``
    (the system resolver's own bound); a timeout after DATA was sent is named as such,
    because the server may still deliver what it read.
  * What a server or a child says is one sanitised line before it reaches the log, the
    error or the console: smtplib joins a multi-line reply with LF, and a relay's text is
    the relay's to choose (``mail.clean_header`` folds the boundaries and the controls).
  * The effective From is ``--from``, else ``from =``, else ``default_from()`` -- the only
    branch that may consult a resolver, and only then -- and must be the bare
    ``local@domain`` the loader accepts (``config.is_address``): dma accepts anything
    without a newline as ``-f`` and smtplib puts a space or a ``>`` on the wire in one line,
    so our validation is the only guard.
  * Recipients are the section's ``to``, exactly (deduplicated by the loader: smtplib
    decides "all refused" by counting), validated at config load; the log line names the
    ones handed to the transport and the size is ``len()`` of the bytes handed over.
"""

import logging
import os
import signal
import smtplib
import ssl
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from logalert.config import (
    RESERVED_SECTION,
    ConfigError,
    Settings,
    default_from,
    from_warning,
    is_address,
    sendmail_problem,
)
from logalert.mail import Mail, clean_header

log = logging.getLogger("logalert.transport")

Transport = Literal["sendmail", "smtp"]

# sysexits.h, spelled here because os.EX_* is absent on Windows (only EX_OK exists there)
SYSEXITS: dict[int, str] = {
    64: "EX_USAGE", 65: "EX_DATAERR", 66: "EX_NOINPUT", 67: "EX_NOUSER", 68: "EX_NOHOST",
    69: "EX_UNAVAILABLE", 70: "EX_SOFTWARE", 71: "EX_OSERR", 72: "EX_OSFILE",
    73: "EX_CANTCREAT", 74: "EX_IOERR", 75: "EX_TEMPFAIL", 76: "EX_PROTOCOL", 77: "EX_NOPERM",
    78: "EX_CONFIG",
}

INSTALL_HINT = ("install an MTA (Ubuntu: apt install postfix, dma or msmtp-mta; FreeBSD 14+: "
                "dma is in base) or set transport = smtp and smtp_host")


class DeliveryError(Exception):
    """The mail was not accepted; the message is the transport's answer, one line."""


@dataclass(frozen=True)
class Delivery:
    """What the transport did with one mail: the run loop logs it and decides the state."""

    transport: Transport
    sender: str  # the envelope sender, the header From
    accepted: tuple[str, ...]  # the message left the box for these (never empty)
    refused: tuple[tuple[str, str], ...]  # (recipient, the server's answer); SMTP only
    size: int  # bytes handed to the transport
    answer: str  # one line: what the transport said


def choose(settings: Settings, *, command: Sequence[str] | None = None) -> Transport:
    """The transport this configuration uses, or the configuration error that says what to
    install or set. ``auto`` needs a sendmail that is a regular, executable file; a
    ``command`` (the tests' fake) stands in for the binary."""
    if settings.transport == "smtp":
        return "smtp"
    if command is not None:
        return "sendmail"
    path = settings.sendmail_path
    problem = sendmail_problem(path)
    if problem is not None:
        remedy = "fix its mode, or " if problem == "is not executable" else ""
        raise ConfigError(f"transport = {settings.transport} but {path} {problem}: {remedy}"
                          f"{INSTALL_HINT}")
    return "sendmail"


def resolve_sender(settings: Settings, override: str | None = None) -> tuple[str, str | None]:
    """The effective From and the warning about it, if any: ``--from``, else ``from =``, else
    the user logalert runs as at this host (the only case that may look up a name)."""
    if override is not None:
        address, source = override, "--from"
    elif settings.from_address is not None:
        address, source = settings.from_address, "from"
    else:
        address, source = default_from(), "the default From"
    if not is_address(address):
        raise ConfigError(f"{source}: {address!r} is not a bare local@domain address; set "
                          f"from = in [{RESERVED_SECTION}]")
    return address, from_warning(address)


def deliver(mail: Mail, settings: Settings, *, command: Sequence[str] | None = None) -> Delivery:
    """Hand the mail to the configured transport. ``command`` replaces ``[sendmail_path]`` as
    the sendmail argv head (the tests' fake); raises ``DeliveryError`` when nothing was
    accepted, and logs the delivery line the activity log carries per section."""
    transport = choose(settings, command=command)
    if transport == "sendmail":
        delivery = via_sendmail(mail, command or [settings.sendmail_path],
                                timeout=settings.mail_timeout)
    else:
        host = settings.smtp_host
        if host is None:  # the loader requires it; a hand-built Settings may not carry it
            raise ConfigError("smtp_host: required when transport = smtp")
        delivery = via_smtp(mail, host, settings.smtp_port,
                            starttls=settings.smtp_starttls,
                            local_hostname=mail.sender.rpartition("@")[2],
                            timeout=settings.mail_timeout)
    log.info("sent via %s to %s: %d bytes, %s (%s)", delivery.transport,
             ", ".join(delivery.accepted), delivery.size, mail.message_id, delivery.answer)
    for recipient, answer in delivery.refused:
        log.warning("refused by the server: %s -- %s", recipient, answer)
    return delivery


def via_sendmail(mail: Mail, command: Sequence[str], *, timeout: float) -> Delivery:
    """Pipe the message to a sendmail binary; exit 0 is "accepted for queueing"."""
    argv = [*command, "-i", "-f", mail.sender, *mail.recipients]
    data = mail.flatten("sendmail")
    try:
        # bytes mode throughout: text mode turns LF into CRLF on Windows and raises a
        # different exception per platform when handed bytes
        returncode, stderr = _run(argv, data, timeout)
    except subprocess.TimeoutExpired as exc:
        said = _first_line(exc.stderr)  # None on Linux, b"" on Windows when nothing was said
        raise DeliveryError(f"sendmail did not finish within {timeout:g} s"
                            + (f" (it said: {said})" if said else "")) from exc
    except (OverflowError, ValueError) as exc:  # a timeout the platform cannot represent
        raise DeliveryError(f"mail_timeout {timeout:g} s is not usable here: {exc}") from exc
    except OSError as exc:
        # exc.filename is None on Windows; a broken shebang is ENOENT on a path that exists
        hint = (" (the file exists: the interpreter on its #! line does not)"
                if isinstance(exc, FileNotFoundError) and os.path.exists(command[0]) else "")
        raise DeliveryError(f"could not execute {command[0]}: {exc.strerror or exc}{hint}"
                            ) from exc
    said = _first_line(stderr)
    if returncode != 0:
        name = SYSEXITS.get(returncode)
        code = f"exit {returncode}" + (f" ({name})" if name else "")
        raise DeliveryError(f"sendmail {code}" + (f": {said}" if said else ""))
    answer = "accepted for queueing (exit 0)"
    if said:  # postdrop with the daemon stopped warns on stderr and still exits 0
        log.warning("sendmail exit 0 but it said: %s", said)
        answer += f"; it said: {said}"
    return Delivery("sendmail", mail.sender, mail.recipients, (), len(data), answer)


def _run(argv: list[str], data: bytes, timeout: float) -> tuple[int, bytes | None]:
    """``subprocess.run`` with one difference: on POSIX the child leads its own process group
    and a timeout kills the group, so a forking wrapper's grandchild dies too."""
    if sys.platform == "win32":
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    else:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    with proc:
        try:
            _, stderr = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if sys.platform == "win32":
                proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:  # the group is already gone
                    pass
            _, exc.stderr = proc.communicate()  # reap, close the pipes, keep what it said
            raise
        except BaseException:
            proc.kill()
            raise
    return proc.returncode, stderr


def _first_line(output: bytes | None) -> str:
    """The first non-empty line a child wrote, decoded, CR-stripped and header-clean, for one
    log line."""
    if not output:
        return ""
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return clean_header(line)
    return ""


def via_smtp(mail: Mail, host: str, port: int, *, starttls: bool, local_hostname: str,
             timeout: float) -> Delivery:
    """One SMTP session; ``timeout`` bounds each socket operation."""
    data = mail.flatten("smtp")
    smtp: smtplib.SMTP | None = None
    try:
        smtp = smtplib.SMTP(host, port, local_hostname=local_hostname, timeout=timeout)
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
        refused = smtp.sendmail(mail.sender, list(mail.recipients), data)
    except smtplib.SMTPRecipientsRefused as exc:
        _farewell(smtp)
        answers = "; ".join(f"{rcpt} -- {_reply(code, text)}"
                            for rcpt, (code, text) in exc.recipients.items())
        untried = [r for r in mail.recipients if r not in exc.recipients]
        if untried:  # a 421 mid-RCPT: smtplib stops there and closes
            raise DeliveryError(f"the server ended the session at RCPT ({answers}); nothing "
                                f"was sent, not tried: {', '.join(untried)}") from exc
        raise DeliveryError(f"every recipient refused: {answers}") from exc
    except smtplib.SMTPResponseException as exc:  # sender refused, DATA rejected, HELO, ...
        _farewell(smtp)
        raise DeliveryError(f"{type(exc).__name__}: {_reply(exc.smtp_code, exc.smtp_error)}"
                            ) from exc
    except (OverflowError, ValueError) as exc:  # a timeout the platform cannot represent
        _farewell(smtp)
        raise DeliveryError(f"mail_timeout {timeout:g} s is not usable here: {exc}") from exc
    except OSError as exc:  # SMTPException, ssl.SSLError, ConnectionError, TimeoutError
        _farewell(smtp)
        said = clean_header(f"{type(exc).__name__}: {exc}")
        if isinstance(exc.__context__, TimeoutError) or "timed out" in said:
            raise DeliveryError(f"no reply within {timeout:g} s ({said}); a message the server "
                                f"had read at DATA may still be delivered") from exc
        raise DeliveryError(said) from exc
    _farewell(smtp)  # a QUIT answered with anything but 221 cannot un-accept the message
    refused_pairs = tuple((rcpt, _reply(code, text)) for rcpt, (code, text) in refused.items())
    accepted = tuple(r for r in mail.recipients if r not in refused)
    if not accepted:  # a server that let DATA through with every recipient refused
        raise DeliveryError("every recipient refused: "
                            + "; ".join(f"{r} -- {a}" for r, a in refused_pairs))
    return Delivery("smtp", mail.sender, accepted, refused_pairs, len(data),
                    f"accepted by {host}:{port}")


def _farewell(smtp: smtplib.SMTP | None) -> None:
    """QUIT and close, whatever the server makes of it: the outcome was decided at DATA."""
    if smtp is None:
        return
    try:
        code, text = smtp.quit()  # returns the reply, whatever its code: never raises on it
        if code != 221:
            log.debug("QUIT after the outcome was known: %s", _reply(code, text))
    except (OSError, smtplib.SMTPException) as exc:  # a drop, a closed socket
        log.debug("QUIT after the outcome was known: %s: %s", type(exc).__name__, exc)
    finally:
        smtp.close()


def _reply(code: int, text: bytes | str) -> str:
    """An SMTP reply as one header-clean line: smtplib joins a multi-line reply with LF, and
    the text is the relay's to choose."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return clean_header(f"{code} {text}")
