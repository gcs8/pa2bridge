"""Minimal, synchronous client for the PA2 HiQnet text console."""

from __future__ import annotations

import math
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable


_VALUE_RE = re.compile(r'^(?P<kind>get|set|setr|subr) "(?P<path>[^"]+)" "(?P<value>[^"]*)"$')
_ERROR_RE = re.compile(r'^error "(?P<message>[^"]*)"$')
_MAX_RESPONSE_LINE_BYTES = 64 * 1024


class ProtocolError(RuntimeError):
    """The PA2 rejected a command or returned an invalid response."""


class ProtocolTimeout(ProtocolError):
    """A bounded PA2 operation timed out."""


class MalformedFrameError(ProtocolError):
    """The PA2 returned bytes that are not a valid console frame."""


class AuthenticationError(ProtocolError):
    """The PA2 did not accept the configured credentials."""


@dataclass(frozen=True)
class ValueResponse:
    kind: str
    path: tuple[str, ...]
    value: str


def _validate_atom(value: str, *, description: str, allow_backslash: bool = True) -> None:
    if not value:
        raise ValueError(f"{description} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{description} contains a protocol control character")
    forbidden = {'"', "\r", "\n"}
    if not allow_backslash:
        forbidden.add("\\")
    if any(character in value for character in forbidden):
        raise ValueError(f"{description} contains a protocol control character")


def encode_path(path: Iterable[str]) -> str:
    """Encode path components using the PA2's two-leading-slash syntax."""
    parts = tuple(path)
    if not parts:
        raise ValueError("path must not be empty")
    for part in parts:
        _validate_atom(part, description="path component", allow_backslash=False)
    return "\\\\" + "\\".join(parts)


def _decode_path(path: str) -> tuple[str, ...]:
    if not path.startswith("\\\\"):
        raise ProtocolError(f"response path is not absolute: {path!r}")
    parts = tuple(path[2:].split("\\"))
    if not parts:
        raise ProtocolError(f"response path is empty: {path!r}")
    if any(not part for part in parts):
        raise ProtocolError(f"response path contains an empty component: {path!r}")
    if any(any(ord(character) < 32 or ord(character) == 127 for character in part) for part in parts):
        raise ProtocolError(f"response path contains a control character: {path!r}")
    return parts


def parse_value_response(line: str) -> ValueResponse:
    if any(ord(character) < 32 or ord(character) == 127 for character in line):
        raise ProtocolError(f"value response contains a control character: {line!r}")
    match = _VALUE_RE.fullmatch(line)
    if not match:
        raise ProtocolError(f"invalid value response: {line!r}")
    return ValueResponse(
        kind=match.group("kind"),
        path=_decode_path(match.group("path")),
        value=match.group("value"),
    )


class HiQnetClient:
    """One persistent, serialized PA2 TCP session.

    PA2 command responses are not tagged with request IDs. All operations share a
    lock so callers cannot interleave commands on the same console session.
    """

    def __init__(self, host: str, *, port: int = 19272, timeout: float = 3.0) -> None:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 60
        ):
            raise ValueError("timeout must be finite, greater than zero, and at most 60")
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._pending_set_echoes: deque[str] = deque()
        self._lock = threading.RLock()
        self._credentials: tuple[str, str] | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self, username: str = "administrator", password: str = "administrator") -> None:
        self._connect(
            username,
            password,
            deadline=self._bounded_deadline(None),
        )

    def connect_before(
        self,
        username: str = "administrator",
        password: str = "administrator",
        *,
        deadline: float,
    ) -> None:
        self._connect(
            username,
            password,
            deadline=self._bounded_deadline(deadline),
        )

    def _connect(self, username: str, password: str, *, deadline: float) -> None:
        _validate_atom(username, description="username")
        _validate_atom(password, description="password")
        with self._lock:
            self._credentials = None
            self.close()
            self._require_before_deadline(deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolTimeout(
                    f"timed out connecting to {self.host}:{self.port}"
                )
            try:
                sock = socket.create_connection(
                    (self.host, self.port),
                    timeout=min(self.timeout, remaining),
                )
            except TimeoutError as error:
                raise ProtocolTimeout(
                    f"timed out connecting to {self.host}:{self.port}"
                ) from error
            except OSError as error:
                raise ProtocolError(f"could not connect to {self.host}:{self.port}: {error}") from error
            self._socket = sock
            self._buffer.clear()
            self._pending_set_echoes.clear()
            self._send(f'connect {username} "{password}"', deadline=deadline)
            try:
                line = self._read_line(deadline)
                if line == "HiQnet Console":
                    line = self._read_line(deadline)
                if line == f"connect logged in as {username}":
                    self._credentials = (username, password)
                    return
                if line.startswith("connect logged in"):
                    raise AuthenticationError(
                        f"unexpected authentication response: {line!r}"
                    )
                if line.startswith("error "):
                    raise AuthenticationError(line)
                raise AuthenticationError(f"unexpected authentication frame: {line!r}")
            except MalformedFrameError as error:
                self.close()
                raise AuthenticationError(
                    "malformed authentication response"
                ) from error
            except Exception:
                self.close()
                raise

    def reconnect(self) -> None:
        self._reconnect(deadline=self._bounded_deadline(None))

    def reconnect_before(self, *, deadline: float) -> None:
        self._reconnect(deadline=self._bounded_deadline(deadline))

    def _reconnect(self, *, deadline: float) -> None:
        with self._lock:
            if self._credentials is None:
                raise ProtocolError("PA2 session has no successful credentials to reconnect with")
            credentials = self._credentials
            try:
                self._connect(*credentials, deadline=deadline)
            except AuthenticationError:
                # A definitive authentication rejection invalidates the saved
                # credentials; silently retrying them later is unsafe.
                self._credentials = None
                raise
            except Exception:
                # A transient reconnect failure must not prevent a later retry.
                self._credentials = credentials
                raise

    def close(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
            self._buffer.clear()
            self._pending_set_echoes.clear()
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def get(self, path: Iterable[str]) -> str:
        return self._get(path, deadline=self._bounded_deadline(None))

    def get_before(self, path: Iterable[str], *, deadline: float) -> str:
        return self._get(path, deadline=self._bounded_deadline(deadline))

    def _get(self, path: Iterable[str], *, deadline: float) -> str:
        expected_path = tuple(path)
        encoded = encode_path(expected_path)
        with self._lock:
            self._send(f'get "{encoded}"', deadline=deadline)
            while True:
                line = self._read_response_line(deadline)
                self._raise_if_error(line)
                try:
                    response = parse_value_response(line)
                except ProtocolError:
                    self.close()
                    raise
                if response.kind == "get" and response.path == expected_path:
                    return response.value
                self.close()
                raise ProtocolError(
                    f"unexpected response to get {expected_path!r}: {line!r}"
                )

    def set(self, path: Iterable[str], value: str) -> None:
        self._set(path, value, deadline=self._bounded_deadline(None))

    def set_before(
        self,
        path: Iterable[str],
        value: str,
        *,
        deadline: float,
    ) -> None:
        self._set(path, value, deadline=self._bounded_deadline(deadline))

    def _set(self, path: Iterable[str], value: str, *, deadline: float) -> None:
        _validate_atom(value, description="value")
        encoded = encode_path(path)
        with self._lock:
            command = f'set "{encoded}" "{value}"'
            self._send(command, deadline=deadline)
            self._pending_set_echoes.append(command)

    def ls(self, path: Iterable[str]) -> dict[str, str]:
        return self._ls(path, deadline=self._bounded_deadline(None))

    def ls_before(
        self,
        path: Iterable[str],
        *,
        deadline: float,
    ) -> dict[str, str]:
        return self._ls(path, deadline=self._bounded_deadline(deadline))

    def _ls(self, path: Iterable[str], *, deadline: float) -> dict[str, str]:
        expected_path = tuple(path)
        encoded = encode_path(expected_path)
        header = f'ls "{encoded}"'
        with self._lock:
            self._send(header, deadline=deadline)
            while True:
                line = self._read_response_line(deadline)
                self._raise_if_error(line)
                if line == header:
                    break
                self.close()
                raise ProtocolError(f"unexpected response before ls header: {line!r}")

            entries: dict[str, str] = {}
            while True:
                line = self._read_line(deadline, allow_tab=True)
                self._raise_if_error(line)
                if line == "endls":
                    return entries
                # PA2 commonly prefixes ls entries with one indentation tab.
                # No tab is data: reject any additional control-tab padding
                # rather than normalizing malformed numeric metadata.
                if line.startswith("\t"):
                    line = line[1:]
                if "\t" in line or " : " not in line:
                    self.close()
                    raise ProtocolError(f"malformed ls entry: {line!r}")
                key, value = line.split(" : ", 1)
                if not key or key != key.strip() or value != value.strip():
                    self.close()
                    raise ProtocolError(f"malformed ls entry: {line!r}")
                if key not in {"..", "*"}:
                    if key in entries:
                        self.close()
                        raise ProtocolError(f"duplicate ls key: {key!r}")
                    entries[key] = value

    def _read_response_line(self, deadline: float) -> str:
        """Read a response while tolerating exact delayed set acknowledgements."""
        line = self._read_line(deadline)
        while self._pending_set_echoes:
            pending = self._pending_set_echoes[0]
            if line not in {pending, "setr" + pending[3:]}:
                break
            self._pending_set_echoes.popleft()
            line = self._read_line(deadline)
        if self._pending_set_echoes:
            # TCP preserves order. A non-echo response proves that the device
            # emitted no exact echo for the remaining queued writes.
            self._pending_set_echoes.clear()
        return line

    def _bounded_deadline(self, deadline: float | None) -> float:
        now = time.monotonic()
        local_deadline = now + self.timeout
        if deadline is None:
            return local_deadline
        if (
            not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)
            or math.isnan(float(deadline))
        ):
            raise ValueError("deadline must be an absolute monotonic timestamp")
        return min(local_deadline, float(deadline))

    def _send(self, command: str, *, deadline: float | None = None) -> None:
        if deadline is not None:
            self._require_before_deadline(deadline)
        if self._socket is None:
            raise ProtocolError("PA2 session is not connected")
        try:
            if deadline is not None:
                self._require_before_deadline(deadline)
            self._socket.sendall((command + "\n").encode("utf-8"))
        except OSError as error:
            self.close()
            raise ProtocolError(f"PA2 command send failed: {error}") from error
        if deadline is not None:
            self._require_before_deadline(deadline)

    def _read_line(self, deadline: float, *, allow_tab: bool = False) -> str:
        while True:
            self._require_before_deadline(deadline)
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > _MAX_RESPONSE_LINE_BYTES:
                    self.close()
                    raise MalformedFrameError(
                        "PA2 response exceeded maximum frame size"
                    )
                break
            if len(self._buffer) > _MAX_RESPONSE_LINE_BYTES:
                self.close()
                raise MalformedFrameError(
                    "PA2 response exceeded maximum frame size"
                )
            if self._socket is None:
                raise ProtocolError("PA2 session is not connected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ProtocolTimeout("timed out waiting for PA2 response")
            self._socket.settimeout(remaining)
            try:
                chunk = self._socket.recv(65536)
            except TimeoutError as error:
                self.close()
                raise ProtocolTimeout("timed out waiting for PA2 response") from error
            except OSError as error:
                self.close()
                raise ProtocolError(f"PA2 response read failed: {error}") from error
            self._require_before_deadline(deadline)
            if not chunk:
                self.close()
                raise ProtocolError("PA2 closed the console session")
            self._buffer.extend(chunk)

        self._require_before_deadline(deadline)
        raw = bytes(self._buffer[:newline])
        del self._buffer[: newline + 1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        try:
            line = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            self.close()
            raise MalformedFrameError("PA2 response was not valid UTF-8") from error
        if any(
            (ord(character) < 32 and (character != "\t" or not allow_tab))
            or ord(character) == 127
            for character in line
        ):
            self.close()
            raise MalformedFrameError(
                "PA2 response contained a protocol control character"
            )
        return line

    def _require_before_deadline(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            self.close()
            raise ProtocolTimeout("timed out waiting for PA2 response")

    def _raise_if_error(self, line: str) -> None:
        match = _ERROR_RE.fullmatch(line)
        if match:
            # Error frames are not correlated to request IDs. Destroy the
            # session so a delayed reply cannot satisfy a future request.
            self.close()
            raise ProtocolError(match.group("message"))
