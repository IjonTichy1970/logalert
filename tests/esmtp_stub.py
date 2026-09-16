"""A threaded ESMTP stub for the transport tests: 127.0.0.1, port 0, stdlib only.

The stub records every command line it receives (raw bytes, terminator included) and the raw
bytes of every DATA payload up to and including the ``.<CRLF>`` line. Verbs are matched
case-insensitively: measured, 3.12 already sends ``ehlo`` / ``mail FROM:`` / ``rcpt TO:`` /
``data`` / ``rset`` / ``quit`` in lowercase while ``starttls()`` and the context manager's
``QUIT`` are uppercase. Nothing here validates sequencing: the stub answers whatever it is
asked, so a test can put smtplib into any state.

Knobs (``StubConfig``): the EHLO name; whether STARTTLS is advertised (when it is, the stub
answers 220 and keeps reading plain text -- it never performs a handshake); the recipients
answered 550; the reply code after DATA (250 or 552); a verb after which the connection is
dropped (``drop_after``: reply, then close; ``drop_on``: close instead of replying); whether
the banner is sent at all; a per-verb reply override (``replies``, e.g. a 421 on MAIL); a delay
before every reply. Never the sandbox's port 25: under mirrored networking it belongs to
another distro's MTA.
"""

import contextlib
import socketserver
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

BANNER = "220 {name} ESMTP logalert-stub"
CRLF = chr(13) + chr(10)


@dataclass(frozen=True)
class StubConfig:
    name: str = "stub.example.net"
    advertise_starttls: bool = False
    refuse: frozenset[str] = frozenset()
    data_code: int = 250
    drop_after: str | None = None
    drop_on: str | None = None
    send_banner: bool = True
    replies: dict[str, str] = field(default_factory=dict)
    reply_delay: float = 0.0
    read_timeout: float = 10.0


class _Handler(socketserver.StreamRequestHandler):
    """One connection; ``self.server`` is the ``StubServer``."""

    server: "StubServer"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.config.read_timeout)  # typeshed: class var

    def _reply(self, line: str) -> None:
        self._reply_lines([line])

    def _reply_lines(self, lines: list[str]) -> None:
        if self.server.config.reply_delay:
            time.sleep(self.server.config.reply_delay)
        self.wfile.write("".join(line + CRLF for line in lines).encode("ascii"))
        self.wfile.flush()

    def _readline(self) -> bytes:
        try:
            return bytes(self.rfile.readline(65536))
        except OSError as exc:
            self.server.note(f"read error: {type(exc).__name__}")
            return b""

    def handle(self) -> None:
        cfg = self.server.config
        conn = self.server.connection_opened()
        try:
            if cfg.send_banner:
                self._reply(BANNER.format(name=cfg.name))
            else:
                self.server.note(f"conn {conn}: banner withheld")
            while True:
                raw = self._readline()
                if not raw:
                    self.server.note(f"conn {conn}: eof")
                    return
                self.server.record_command(raw)
                head, _, arg = raw.rstrip(b"\r\n").partition(b" ")
                verb = head.decode("ascii", errors="replace").upper()
                if cfg.drop_on == verb:
                    self.server.note(f"conn {conn}: dropped on {verb}")
                    return
                if not self._dispatch(verb, arg):
                    return
                if cfg.drop_after == verb:
                    self.server.note(f"conn {conn}: dropped after {verb}")
                    return
        except OSError as exc:
            self.server.note(f"conn {conn}: write error: {type(exc).__name__}")
        finally:
            self.server.connection_closed()

    def _dispatch(self, verb: str, arg: bytes) -> bool:
        """Answer one command; False means stop serving this connection."""
        cfg = self.server.config
        if verb in cfg.replies:
            self._reply(cfg.replies[verb])
            return True
        if verb == "EHLO":
            lines = ["250-" + cfg.name, "250-SIZE 10240000", "250-8BITMIME"]
            if cfg.advertise_starttls:
                lines.append("250-STARTTLS")
            lines.append("250 HELP")  # a real keyword: "250 OK" registers an "ok" extension
            self._reply_lines(lines)
        elif verb == "HELO":
            self._reply("250 " + cfg.name)
        elif verb == "MAIL":
            self._reply("250 2.1.0 Ok")
        elif verb == "RCPT":
            addr = _angle_addr(arg)
            if addr in cfg.refuse:
                self._reply(f"550 5.1.1 <{addr}>: Recipient address rejected: User unknown")
            else:
                self._reply("250 2.1.5 Ok")
        elif verb == "DATA":
            self._reply("354 End data with <CR><LF>.<CR><LF>")
            payload = self._read_data()
            self.server.record_data(payload)
            if payload is None:
                return False
            if cfg.data_code == 250:
                self._reply("250 2.0.0 Ok: queued as STUB0001")
            else:
                self._reply(f"{cfg.data_code} 5.3.4 Error: message rejected by stub")
        elif verb in ("RSET", "NOOP"):
            self._reply("250 2.0.0 Ok")
        elif verb == "QUIT":
            self._reply("221 2.0.0 Bye")
            return False
        elif verb == "STARTTLS":
            if cfg.advertise_starttls:
                self._reply("220 2.0.0 Ready to start TLS")
            else:
                self._reply("502 5.5.1 Error: command not implemented")
        else:
            self._reply("500 5.5.2 Error: command not recognized")
        return True

    def _read_data(self) -> bytes | None:
        """Read up to and including the ``.<CRLF>`` line; None on EOF."""
        chunks: list[bytes] = []
        while True:
            line = self._readline()
            if not line:
                self.server.note("eof inside DATA")
                return None
            chunks.append(line)
            if line in (b".\r\n", b".\n"):
                return b"".join(chunks)


def _angle_addr(arg: bytes) -> str:
    """``TO:<a@b>`` -> ``a@b``; anything else is returned as text."""
    text = arg.decode("ascii", errors="replace")
    start = text.find("<")
    end = text.find(">", start + 1)
    if start >= 0 and end > start:
        return text[start + 1:end]
    return text.partition(":")[2].strip()


class StubServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, config: StubConfig) -> None:
        self.config = config
        self.commands: list[bytes] = []
        self.data: list[bytes | None] = []
        self.notes: list[str] = []
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._active = 0
        self._connections = 0
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def record_command(self, raw: bytes) -> None:
        with self._lock:
            self.commands.append(raw)

    def record_data(self, payload: bytes | None) -> None:
        with self._lock:
            self.data.append(payload)

    def note(self, text: str) -> None:
        with self._lock:
            self.notes.append(text)

    def connection_opened(self) -> int:
        with self._idle:
            self._active += 1
            self._connections += 1
            return self._connections

    def connection_closed(self) -> None:
        with self._idle:
            self._active -= 1
            self._idle.notify_all()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until no handler thread is running; False on timeout."""
        with self._idle:
            return self._idle.wait_for(lambda: self._active == 0, timeout)

    def verbs(self) -> list[str]:
        """The verbs received, upper-cased (the client's case is mixed), in order."""
        return [c.split(b" ", 1)[0].decode("ascii", errors="replace").strip().upper()
                for c in self.commands]

    def command_lines(self) -> list[str]:
        return [c.decode("ascii", errors="replace").rstrip("\r\n") for c in self.commands]


@contextlib.contextmanager
def run_stub(config: StubConfig | None = None) -> Iterator[StubServer]:
    """Start a stub in a daemon thread; shut it down on exit."""
    server = StubServer(config or StubConfig())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5.0)
