"""A small MQTT 3.1.1 client: connect, subscribe, publish (QoS 0), read, disconnect.

Python's standard library has no MQTT client and Fusion add-ins should not
depend on extra packages, so this covers exactly what the printer
connectors need: one short session that logs in, subscribes to a report
topic, sends a request and reads the answers. No persistence, no QoS 1/2,
no reconnect logic.

The caller passes an already connected socket (plain or TLS), so this
module knows nothing about certificates.
"""

from __future__ import annotations

import socket
import struct
import time
from typing import Optional, Tuple

#: CONNACK return codes (MQTT 3.1.1, section 3.2.2.3).
CONNACK_ERRORS = {
    1: "unacceptable protocol version",
    2: "client id rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorized",
}

_CONNECT, _CONNACK, _PUBLISH, _SUBSCRIBE, _SUBACK = 0x10, 0x20, 0x30, 0x82, 0x90
_PINGREQ, _DISCONNECT = 0xC0, 0xE0

#: Largest packet accepted; a printer report is about 30 KB.
MAX_PACKET_BYTES = 4 * 1024 * 1024


class MqttError(Exception):
    """The broker refused us or the connection broke."""


class MqttAuthError(MqttError):
    """The broker rejected the user name or password (CONNACK 4 or 5)."""


def _encode_length(length: int) -> bytes:
    """Encode MQTT's variable-length 'remaining length' field."""
    out = bytearray()
    while True:
        digit, length = length % 128, length // 128
        out.append(digit | (0x80 if length else 0))
        if not length:
            return bytes(out)


def _string(value: str) -> bytes:
    """Encode a UTF-8 string with its 2-byte length prefix."""
    data = value.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def _packet(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + _encode_length(len(body)) + body


class MqttClient:
    """One MQTT session over a connected socket."""

    def __init__(self, sock: socket.socket, timeout: float = 6.0) -> None:
        self._sock = sock
        self._sock.settimeout(timeout)
        self._next_id = 1

    # -- low level ---------------------------------------------------------

    def _recv_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise MqttError("the connection was closed")
            data.extend(chunk)
        return bytes(data)

    def _read_packet(self) -> Tuple[int, bytes]:
        kind = self._recv_exact(1)[0]
        length, multiplier = 0, 1
        for _ in range(4):
            digit = self._recv_exact(1)[0]
            length += (digit & 0x7F) * multiplier
            multiplier *= 128
            if not digit & 0x80:
                break
        else:
            raise MqttError("malformed packet length")
        if length > MAX_PACKET_BYTES:
            raise MqttError("packet too large ({} bytes)".format(length))
        return kind, self._recv_exact(length)

    # -- session ------------------------------------------------------------

    def connect(self, client_id: str, username: str, password: str, keepalive: int = 30) -> None:
        """Log in; raise :class:`MqttAuthError` or :class:`MqttError` when refused."""
        flags = 0x80 | 0x40 | 0x02  # user name, password, clean session
        body = (
            _string("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
            + _string(client_id) + _string(username) + _string(password)
        )
        self._sock.sendall(_packet(_CONNECT, body))
        kind, data = self._read_packet()
        if kind & 0xF0 != _CONNACK or len(data) < 2:
            raise MqttError("unexpected answer to the login")
        code = data[1]
        if code in (4, 5):
            raise MqttAuthError(CONNACK_ERRORS[code])
        if code:
            raise MqttError(CONNACK_ERRORS.get(code, "login refused (code {})".format(code)))

    def subscribe(self, topic: str) -> None:
        """Subscribe to one topic with QoS 0 and wait for the acknowledgement."""
        packet_id = self._next_id
        self._next_id += 1
        self._sock.sendall(_packet(_SUBSCRIBE, struct.pack("!H", packet_id) + _string(topic) + b"\x00"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            kind, data = self._read_packet()
            if kind & 0xF0 == _SUBACK:
                if len(data) >= 3 and data[2] == 0x80:
                    raise MqttError("subscription to {!r} refused".format(topic))
                return
        raise MqttError("no subscription acknowledgement")

    def publish(self, topic: str, payload: bytes) -> None:
        """Publish one QoS 0 message."""
        self._sock.sendall(_packet(_PUBLISH, _string(topic) + payload))

    def read_message(self, timeout: float) -> Optional[Tuple[str, bytes]]:
        """Return the next ``(topic, payload)``, or None when ``timeout`` passes."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._sock.settimeout(left)
            try:
                kind, data = self._read_packet()
            except socket.timeout:
                return None
            if kind & 0xF0 != _PUBLISH:
                continue
            topic_length = struct.unpack("!H", data[:2])[0]
            offset = 2 + topic_length
            if (kind >> 1) & 0x03:  # QoS 1/2 messages carry a packet id
                offset += 2
            return data[2:2 + topic_length].decode("utf-8", "replace"), data[offset:]

    def ping(self) -> None:
        self._sock.sendall(bytes([_PINGREQ, 0]))

    def close(self) -> None:
        """Say goodbye and close the socket (errors ignored)."""
        try:
            self._sock.sendall(bytes([_DISCONNECT, 0]))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Self-test (no network: a fake socket plays the broker)
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Replays broker bytes and records what the client sent."""

    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def settimeout(self, value: float) -> None:
        pass

    def recv(self, count: int) -> bytes:
        if not self.incoming:
            raise socket.timeout()
        data, self.incoming = bytes(self.incoming[:count]), self.incoming[count:]
        return data

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def close(self) -> None:
        pass


def _selftest() -> int:
    import traceback

    def check_length_encoding() -> None:
        for value, encoded in ((0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"), (16383, b"\xff\x7f"), (2097152, b"\x80\x80\x80\x01")):
            assert _encode_length(value) == encoded, value

    def check_session() -> None:
        report = _string("device/X/report") + b'{"print":{"ams":{}}}'
        broker = (
            _packet(_CONNACK, b"\x00\x00")
            + _packet(_SUBACK, b"\x00\x01\x00")
            + _packet(_PUBLISH, report)
        )
        fake = _FakeSocket(broker)
        client = MqttClient(fake)  # type: ignore[arg-type]
        client.connect("filamatch", "bblp", "secret")
        client.subscribe("device/X/report")
        client.publish("device/X/request", b'{"pushing":{}}')
        topic, payload = client.read_message(1.0)
        assert topic == "device/X/report" and payload == b'{"print":{"ams":{}}}', (topic, payload)
        assert client.read_message(0.1) is None
        assert bytes(fake.sent).startswith(bytes([_CONNECT]))
        assert b"bblp" in bytes(fake.sent) and _string("device/X/request") in bytes(fake.sent)

    def check_auth_error() -> None:
        client = MqttClient(_FakeSocket(_packet(_CONNACK, b"\x00\x05")))  # type: ignore[arg-type]
        try:
            client.connect("filamatch", "bblp", "wrong")
        except MqttAuthError:
            return
        raise AssertionError("CONNACK 5 must raise MqttAuthError")

    checks = (
        ("remaining-length encoding", check_length_encoding),
        ("login, subscribe, publish and read", check_session),
        ("a refused login raises MqttAuthError", check_auth_error),
    )
    failures = 0
    for label, check in checks:
        try:
            check()
            print("PASS:", label)
        except Exception:
            failures += 1
            print("FAIL:", label)
            traceback.print_exc()
    if failures:
        return 1
    print("PASS: all mqtt_lite self-tests completed")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
