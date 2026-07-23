from __future__ import annotations

import socketserver
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pa2bridge.protocol import (
    AuthenticationError,
    HiQnetClient,
    ProtocolError,
    ProtocolTimeout,
    encode_path,
    parse_value_response,
)


class _Handler(socketserver.StreamRequestHandler):
    commands: list[str] = []
    echo_sets = False

    def handle(self) -> None:
        self.wfile.write(b"HiQnet Console\r\n")
        auth = self.rfile.readline().decode().rstrip("\r\n")
        self.commands.append(auth)
        self.wfile.write(b"connect logged in as administrator\r\n")
        self.wfile.flush()

        while raw := self.rfile.readline():
            command = raw.decode().rstrip("\r\n")
            self.commands.append(command)
            if command == 'get "\\\\Node\\AT\\Instance_Name"':
                self.wfile.write(b'get "\\\\Node\\AT\\Instance_Name" "DriveRackPA2"\r\n')
            elif command == 'ls "\\\\Storage\\Presets\\SV"':
                self.wfile.write(
                    b'ls "\\\\Storage\\Presets\\SV"\r\n'
                    b'\t.. : \r\n'
                    b'\tCurrentPreset : 1\r\n'
                    b'\tName_1 : Flat\r\n'
                    b'\tName_2 : Alternate\r\n'
                    b'\t* : \r\n'
                    b'endls\r\n'
                )
            elif command == 'get "\\\\Slow"':
                continue
            elif command.startswith("set "):
                # Firmware 1.2.0.1 accepts ordinary set writes without a
                # response frame; the following get/ls owns the next reply.
                if self.echo_sets:
                    self.wfile.write((command + "\r\n").encode())
                else:
                    continue
            else:
                self.wfile.write(b'error "unknown path"\r\n')
            self.wfile.flush()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


@contextmanager
def fake_pa2(*, echo_sets: bool = False):
    _Handler.commands = []
    _Handler.echo_sets = echo_sets
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address, _Handler.commands
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        _Handler.echo_sets = False


def test_encode_path_uses_two_leading_backslashes_and_single_separators() -> None:
    assert encode_path(("Storage", "Presets", "SV", "Recall")) == r"\\Storage\Presets\SV\Recall"


def test_parse_value_response_preserves_path_and_value() -> None:
    parsed = parse_value_response('get "\\\\Storage\\Presets\\SV\\Name_1" "Flat"')
    assert parsed.kind == "get"
    assert parsed.path == ("Storage", "Presets", "SV", "Name_1")
    assert parsed.value == "Flat"


def test_client_authenticates_and_supports_get_ls_and_set() -> None:
    with fake_pa2() as ((host, port), commands):
        client = HiQnetClient(host, port=port, timeout=1)
        client.connect("administrator", "administrator")

        assert client.get(("Node", "AT", "Instance_Name")) == "DriveRackPA2"
        assert client.ls(("Storage", "Presets", "SV")) == {
            "CurrentPreset": "1",
            "Name_1": "Flat",
            "Name_2": "Alternate",
        }
        client.set(("Storage", "Presets", "SV", "Recall"), "2")
        client.close()

    assert commands == [
        'connect administrator "administrator"',
        'get "\\\\Node\\AT\\Instance_Name"',
        'ls "\\\\Storage\\Presets\\SV"',
        'set "\\\\Storage\\Presets\\SV\\Recall" "2"',
    ]


def test_set_is_fire_and_forget_so_the_next_get_reads_its_own_response() -> None:
    with fake_pa2() as ((host, port), _):
        client = HiQnetClient(host, port=port, timeout=1)
        try:
            client.connect("administrator", "administrator")
            client.set(("Storage", "Presets", "SV", "Recall"), "2")

            assert client.get(("Node", "AT", "Instance_Name")) == "DriveRackPA2"
        finally:
            client.close()


def test_delayed_set_echoes_are_discarded_before_the_next_get_response() -> None:
    with fake_pa2(echo_sets=True) as ((host, port), _):
        client = HiQnetClient(host, port=port, timeout=1)
        try:
            client.connect("administrator", "administrator")
            client.set(("Preset", "OutputGains", "SV", "HighLeftOutputMute"), "On")
            client.set(("Storage", "Presets", "SV", "Recall"), "2")

            assert client.get(("Node", "AT", "Instance_Name")) == "DriveRackPA2"
        finally:
            client.close()


def test_get_turns_device_error_into_protocol_error() -> None:
    with fake_pa2() as ((host, port), _):
        client = HiQnetClient(host, port=port, timeout=1)
        try:
            client.connect("administrator", "administrator")
            with pytest.raises(ProtocolError, match="unknown path"):
                client.get(("No", "Such", "Path"))
            assert client.connected is False
            with pytest.raises(ProtocolError, match="not connected"):
                client.get(("No", "Such", "Path"))
        finally:
            client.close()


def test_invalid_paths_and_disconnected_operations_fail_closed() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        encode_path(())
    with pytest.raises(ValueError, match="control character"):
        encode_path(("bad\\component",))
    for unsafe in ("bad\x00component", "bad\tcomponent", "bad\x7fcomponent"):
        with pytest.raises(ValueError, match="control character"):
            encode_path((unsafe,))
    with pytest.raises(ProtocolError, match="not absolute"):
        parse_value_response('get "relative" "value"')
    with pytest.raises(ProtocolError, match="empty component"):
        parse_value_response('get "\\\\Preset\\\\OutputGains" "On"')
    with pytest.raises(ProtocolError, match="control character"):
        parse_value_response('get "\\\\Preset\\Bad\x00Path" "On"')
    with pytest.raises(ProtocolError, match="control character"):
        parse_value_response('get "\\\\Preset\\OutputGains" "On\t"')

    client = HiQnetClient("127.0.0.1")
    with pytest.raises(ProtocolError, match="not connected"):
        client.get(("Node",))


def test_get_has_a_bounded_response_timeout() -> None:
    with fake_pa2() as ((host, port), _):
        client = HiQnetClient(host, port=port, timeout=0.05)
        try:
            client.connect("administrator", "administrator")
            with pytest.raises(ProtocolTimeout):
                client.get(("Slow",))
            assert client.connected is False
            with pytest.raises(ProtocolError, match="not connected"):
                client.get(("Slow",))
        finally:
            client.close()


def test_authentication_requires_exact_requested_identity() -> None:
    with fake_pa2() as ((host, port), _):
        client = HiQnetClient(host, port=port, timeout=0.1)
        try:
            with pytest.raises(AuthenticationError, match="unexpected authentication response"):
                client.connect("different-user", "administrator")
            assert client.connected is False
        finally:
            client.close()


def test_authentication_rejects_a_stale_frame_before_the_success_banner(monkeypatch) -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(
        clock,
        b'get "\\\\Node\\AT\\Instance_Name" "stale"\n'
        b"connect logged in as administrator\n",
    )
    monkeypatch.setattr(
        "pa2bridge.protocol.socket.create_connection",
        lambda *args, **kwargs: sock,
    )
    client = HiQnetClient("example.invalid", timeout=1)

    with pytest.raises(ProtocolError, match="unexpected authentication frame"):
        client.connect("administrator", "administrator")

    assert client.connected is False
    assert client._credentials is None
    assert sock.closed is True


def test_reconnect_requires_prior_authentication_and_reuses_successful_credentials() -> None:
    client = HiQnetClient("127.0.0.1")
    with pytest.raises(ProtocolError, match="no successful credentials"):
        client.reconnect()

    with fake_pa2() as ((host, port), _):
        client = HiQnetClient(host, port=port)
        client.connect("administrator", "administrator")
        client.close()
        client.reconnect()
        assert client.get(("Node", "AT", "Instance_Name")) == "DriveRackPA2"
        client.close()


def test_reconnect_authentication_rejection_forgets_stale_credentials(monkeypatch) -> None:
    client = HiQnetClient("example.invalid")
    client._credentials = ("administrator", "stale-secret")

    def rejected(username: str, password: str, *, deadline: float) -> None:
        del username, password, deadline
        raise AuthenticationError("credentials rejected")

    monkeypatch.setattr(client, "_connect", rejected)

    with pytest.raises(AuthenticationError):
        client.reconnect()

    assert client._credentials is None
    with pytest.raises(ProtocolError, match="no successful credentials"):
        client.reconnect()


def test_reconnect_malformed_authentication_frame_forgets_stale_credentials(
    monkeypatch,
) -> None:
    client = HiQnetClient("example.invalid")
    client._credentials = ("administrator", "stale-secret")
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(
        clock,
        b"connect logged in as administrator\r\r\n",
    )
    monkeypatch.setattr(
        "pa2bridge.protocol.socket.create_connection",
        lambda *_args, **_kwargs: sock,
    )

    with pytest.raises(AuthenticationError, match="malformed authentication"):
        client.reconnect()

    assert client._credentials is None
    assert client.connected is False
    assert sock.closed is True


def test_failed_explicit_login_clears_previously_successful_credentials() -> None:
    with fake_pa2() as ((host, port), _):
        client = HiQnetClient(host, port=port, timeout=0.1)
        try:
            client.connect("administrator", "administrator")
            with pytest.raises(AuthenticationError):
                client.connect("different-user", "administrator")
            with pytest.raises(ProtocolError, match="no successful credentials"):
                client.reconnect()
        finally:
            client.close()


class _LateSocket:
    def __init__(self, clock: SimpleNamespace, response: bytes) -> None:
        self.clock = clock
        self.response = response
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def sendall(self, data: bytes) -> None:
        assert data

    def recv(self, size: int) -> bytes:
        del size
        self.clock.now = 2.0
        return self.response

    def shutdown(self, how: int) -> None:
        del how

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0.0, -1.0, 61.0, True])
def test_client_rejects_unsafe_timeout_values(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        HiQnetClient("127.0.0.1", timeout=timeout)


def test_get_rejects_a_response_received_after_the_absolute_deadline(monkeypatch) -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b'get "\\\\Node\\AT\\Instance_Name" "late"\r\n')
    client = HiQnetClient("127.0.0.1", timeout=1)
    client._socket = sock
    monkeypatch.setattr("pa2bridge.protocol.time.monotonic", lambda: clock.now)
    monkeypatch.setattr(client, "_send", lambda command, **kwargs: None)

    with pytest.raises(ProtocolTimeout):
        client.get(("Node", "AT", "Instance_Name"))

    assert client.connected is False
    assert sock.closed is True


def test_get_before_does_not_start_receive_after_controller_deadline(monkeypatch) -> None:
    clock = SimpleNamespace(now=0.0)

    class SendCrossesDeadlineSocket(_LateSocket):
        def __init__(self) -> None:
            super().__init__(clock, b"")
            self.recv_calls = 0

        def sendall(self, data: bytes) -> None:
            assert data
            clock.now = 2.0

        def recv(self, size: int) -> bytes:
            self.recv_calls += 1
            return super().recv(size)

    sock = SendCrossesDeadlineSocket()
    client = HiQnetClient("127.0.0.1", timeout=10)
    client._socket = sock
    monkeypatch.setattr("pa2bridge.protocol.time.monotonic", lambda: clock.now)

    with pytest.raises(ProtocolTimeout):
        client.get_before(("Node", "AT", "Instance_Name"), deadline=1.0)

    assert sock.recv_calls == 0
    assert sock.closed is True


def test_reconnect_before_does_not_authenticate_after_controller_deadline(
    monkeypatch,
) -> None:
    clock = SimpleNamespace(now=0.0)

    class ConnectCrossesDeadlineSocket(_LateSocket):
        def __init__(self) -> None:
            super().__init__(clock, b"")
            self.send_calls = 0

        def sendall(self, data: bytes) -> None:
            self.send_calls += 1

    sock = ConnectCrossesDeadlineSocket()

    def connect_late(*args, **kwargs):
        del args, kwargs
        clock.now = 2.0
        return sock

    monkeypatch.setattr("pa2bridge.protocol.time.monotonic", lambda: clock.now)
    monkeypatch.setattr(
        "pa2bridge.protocol.socket.create_connection",
        connect_late,
    )
    client = HiQnetClient("127.0.0.1", timeout=10)
    client._credentials = ("administrator", "fake-password")

    with pytest.raises(ProtocolTimeout):
        client.reconnect_before(deadline=1.0)

    assert sock.send_calls == 0
    assert sock.closed is True


def test_reconnect_before_does_not_open_socket_if_deadline_expires_before_connect(
    monkeypatch,
) -> None:
    clock_reads = iter((0.0, 0.0, 2.0))
    create_calls = 0

    def unexpected_connect(*args, **kwargs):
        nonlocal create_calls
        del args, kwargs
        create_calls += 1
        raise AssertionError("socket connection must not start after the deadline")

    monkeypatch.setattr("pa2bridge.protocol.time.monotonic", lambda: next(clock_reads))
    monkeypatch.setattr(
        "pa2bridge.protocol.socket.create_connection",
        unexpected_connect,
    )
    client = HiQnetClient("127.0.0.1", timeout=10)
    client._credentials = ("administrator", "fake-password")

    with pytest.raises(ProtocolTimeout):
        client.reconnect_before(deadline=1.0)

    assert create_calls == 0
    assert client._credentials == ("administrator", "fake-password")


def test_authentication_does_not_cache_a_late_success(monkeypatch) -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b"connect logged in as administrator\r\n")
    monkeypatch.setattr("pa2bridge.protocol.time.monotonic", lambda: clock.now)
    monkeypatch.setattr(
        "pa2bridge.protocol.socket.create_connection", lambda *args, **kwargs: sock
    )
    client = HiQnetClient("127.0.0.1", timeout=1)

    with pytest.raises(ProtocolTimeout):
        client.connect("administrator", "secret")

    assert client.connected is False
    assert client._credentials is None
    assert sock.closed is True


@pytest.mark.parametrize(
    "first_line",
    [
        'get "\\\\Node\\AT\\Bad\x00Path" "poison"',
        'get "\\\\Node\\AT\\Other" "wrong path"',
    ],
)
def test_invalid_or_mismatched_get_response_invalidates_session(first_line: str) -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b"")
    client = HiQnetClient("example.invalid", timeout=1.0)
    client._socket = sock
    client._buffer.extend(
        (
            first_line
            + '\nget "\\\\Node\\AT\\Instance_Name" "accepted"\n'
        ).encode()
    )

    with pytest.raises(ProtocolError):
        client.get(("Node", "AT", "Instance_Name"))

    assert client.connected is False
    assert sock.closed is True


def test_multiple_carriage_returns_in_frame_ending_invalidate_session() -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b"")
    client = HiQnetClient("example.invalid", timeout=1.0)
    client._socket = sock
    client._buffer.extend(
        b'get "\\\\Storage\\Presets\\SV\\CurrentPreset" "2"\r\r\n'
    )

    with pytest.raises(ProtocolError, match="control character"):
        client.get(("Storage", "Presets", "SV", "CurrentPreset"))

    assert client.connected is False
    assert sock.closed is True


def test_tab_in_get_response_value_invalidates_session() -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b"")
    client = HiQnetClient("example.invalid", timeout=1.0)
    client._socket = sock
    client._buffer.extend(
        b'get "\\\\Storage\\Presets\\SV\\CurrentPreset" "2\t"\n'
    )

    with pytest.raises(ProtocolError, match="control character"):
        client.get(("Storage", "Presets", "SV", "CurrentPreset"))

    assert client.connected is False
    assert sock.closed is True


def test_duplicate_ls_key_invalidates_session() -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b"")
    client = HiQnetClient("example.invalid", timeout=1.0)
    client._socket = sock
    client._buffer.extend(
        (
            'ls "\\\\Storage\\Presets\\SV"\n'
            'Name_1 : Approved\n'
            'Name_1 : Different\n'
            'endls\n'
        ).encode()
    )

    with pytest.raises(ProtocolError, match="duplicate ls key"):
        client.ls(("Storage", "Presets", "SV"))

    assert client.connected is False


def test_ls_rejects_control_tabs_outside_the_single_indent_prefix() -> None:
    clock = SimpleNamespace(now=0.0)
    sock = _LateSocket(clock, b"")
    client = HiQnetClient("example.invalid", timeout=1.0)
    client._socket = sock
    client._buffer.extend(
        (
            'ls "\\\\Storage\\Presets\\SV"\n'
            "\tNumPresets : 1\t\n"
            "\tCurrentPreset : 1\n"
            "\tName_1 : Flat\n"
            "endls\n"
        ).encode()
    )

    with pytest.raises(ProtocolError, match="malformed ls entry"):
        client.ls(("Storage", "Presets", "SV"))

    assert client.connected is False
