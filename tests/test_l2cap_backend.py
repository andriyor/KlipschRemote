"""Tests for the Linux BR/EDR raw-ATT backend (klipsch_ble/l2cap_backend.py).

The backend opens a real ``AF_BLUETOOTH`` L2CAP socket and runs a reader thread,
so — mirroring ``test_winrt_backend`` — these inject a fake socket that plays a
tiny in-memory ATT server. That exercises the real discovery / read / write /
notify code paths with no Bluetooth hardware. The pure UUID helpers (the
byte-order-sensitive bits) are tested directly.
"""

from __future__ import annotations

import asyncio
import queue
import socket
import struct

import pytest

from klipsch_ble.l2cap_backend import (
    AttError,
    L2capAttClient,
    _norm_uuid,
    _uuid_from_le,
    l2cap_factory,
)

VOL_UUID = "da6d0fa2-0d18-442c-babe-f85b5baa6f11"
MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"  # DIS Model Number (16-bit)


# ---- pure helpers -----------------------------------------------------------
def test_uuid_from_le_16bit():
    assert _uuid_from_le(struct.pack("<H", 0x2A24)) == MODEL_UUID


def test_uuid_from_le_128bit_byte_order():
    # canonical UUID is stored on the wire as reversed (little-endian) bytes
    wire = bytes.fromhex(VOL_UUID.replace("-", ""))[::-1]
    assert _uuid_from_le(wire) == VOL_UUID


def test_norm_uuid_expands_and_lowercases():
    assert _norm_uuid("2A24") == MODEL_UUID
    assert _norm_uuid(VOL_UUID.upper()) == VOL_UUID


# ---- fake ATT server socket -------------------------------------------------
_SENTINEL = object()


class FakeAttSocket:
    """A scripted in-memory ATT server behind the socket API the backend uses.

    Attribute map (handle -> type uuid):
        0x0002 char decl (0x2803) for VOL_UUID, value handle 0x0003, props R|W|N
        0x0003 VOL_UUID value
        0x0004 CCC (0x2902)
        0x0005 char decl (0x2803) for MODEL (0x2a24), value handle 0x0006, props R
        0x0006 MODEL value
    """

    def __init__(self, *_args):
        self._rx: queue.Queue = queue.Queue()
        self._closed = False
        self.values = {0x0003: bytearray([12]), 0x0006: b"1067563"}
        self.writes: list[tuple[int, bytes]] = []
        # (decl, props, value_handle, uuid_le)
        self._chars = [
            (0x0002, 0x02 | 0x08 | 0x10, 0x0003, bytes.fromhex(VOL_UUID.replace("-", ""))[::-1]),
            (0x0005, 0x02, 0x0006, struct.pack("<H", 0x2A24)),
        ]
        # (handle, type-uuid) — 16-bit ints or 16-byte little-endian
        self._attrs = [
            (0x0002, 0x2803),
            (0x0003, self._chars[0][3]),
            (0x0004, 0x2902),
            (0x0005, 0x2803),
            (0x0006, 0x2A24),
        ]

    # -- socket API the backend calls --
    def settimeout(self, _t):
        pass

    def connect(self, _addr):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        self._closed = True
        self._rx.put(_SENTINEL)

    def send(self, pdu: bytes):
        rsp = self._handle(pdu)
        if rsp is not None:
            self._rx.put(rsp)

    def recv(self, _n):
        if self._closed:
            raise OSError("closed")
        try:
            item = self._rx.get(timeout=1.0)
        except queue.Empty:
            raise socket.timeout() from None
        if item is _SENTINEL:
            raise OSError("closed")
        return item

    # -- test helper: speaker pushes a notification --
    def push_notify(self, handle: int, value: bytes):
        self._rx.put(bytes([0x1B]) + struct.pack("<H", handle) + value)

    # -- the tiny ATT server --
    def _handle(self, pdu: bytes):
        op = pdu[0]
        if op == 0x02:  # Exchange MTU
            return bytes([0x03]) + struct.pack("<H", 23)
        if op == 0x08:  # Read By Type (only 0x2803 is asked)
            start, end = struct.unpack("<HH", pdu[1:5])
            hits = [c for c in self._chars if start <= c[0] <= end]
            if not hits:
                return bytes([0x01, 0x08]) + struct.pack("<H", start) + bytes([0x0A])
            first = hits[0]
            ulen = len(first[3])
            group = [c for c in hits if len(c[3]) == ulen]
            rec_len = 2 + 1 + 2 + ulen
            body = b"".join(
                struct.pack("<H", d) + bytes([p]) + struct.pack("<H", vh) + u
                for (d, p, vh, u) in group
            )
            return bytes([0x09, rec_len]) + body
        if op == 0x04:  # Find Information
            start, end = struct.unpack("<HH", pdu[1:5])
            hits = [a for a in self._attrs if start <= a[0] <= end]
            if not hits:
                return bytes([0x01, 0x04]) + struct.pack("<H", start) + bytes([0x0A])
            first_is16 = isinstance(hits[0][1], int)
            run = []
            for h, u in hits:
                if isinstance(u, int) != first_is16:
                    break
                run.append((h, u))
            if first_is16:
                body = b"".join(struct.pack("<HH", h, u) for h, u in run)
                return bytes([0x05, 0x01]) + body
            body = b"".join(struct.pack("<H", h) + u for h, u in run)
            return bytes([0x05, 0x02]) + body
        if op == 0x0A:  # Read
            handle = struct.unpack("<H", pdu[1:3])[0]
            return bytes([0x0B]) + bytes(self.values.get(handle, b""))
        if op == 0x12:  # Write Request
            handle = struct.unpack("<H", pdu[1:3])[0]
            self.writes.append((handle, pdu[3:]))
            self.values[handle] = pdu[3:]
            return bytes([0x13])
        return bytes([0x01, op]) + struct.pack("<H", 0) + bytes([0x06])


@pytest.fixture
def bt_constants(monkeypatch):
    """Let ``L2capAttClient.connect()``'s ``AF_BLUETOOTH``/``BTPROTO_L2CAP`` guard
    pass on any OS.

    These tests inject a fake socket, so nothing real is ever opened — the only
    thing gating them off Linux is that attribute check. Stub the two constants
    (restored on teardown; real values kept where present) so the ATT protocol
    logic is exercised on *every* runner. Otherwise coverage would hinge on how
    the CI CPython was built: ``actions/setup-python`` images can lack the bluez
    headers, dropping the constants and silently skipping the whole backend.
    """
    for name in ("AF_BLUETOOTH", "BTPROTO_L2CAP"):
        monkeypatch.setattr(socket, name, getattr(socket, name, 0), raising=False)


def test_discover_read_write_over_fake_att(bt_constants):
    fake = FakeAttSocket()

    async def run():
        client = L2capAttClient("AA:BB:CC:DD:EE:FF", socket_factory=lambda *a: fake)
        await client.connect()
        # discovery mapped both a 128-bit vendor char and a 16-bit DIS char
        assert client._handle[VOL_UUID] == 0x0003
        assert client._handle[MODEL_UUID] == 0x0006
        assert client._ccc[VOL_UUID] == 0x0004
        # read
        assert bytes(await client.read_gatt_char(VOL_UUID)) == bytes([12])
        assert bytes(await client.read_gatt_char(MODEL_UUID)) == b"1067563"
        # write
        await client.write_gatt_char(VOL_UUID, bytes([20]))
        assert fake.writes[-1] == (0x0003, bytes([20]))
        await client.disconnect()

    asyncio.run(run())


def test_start_notify_dispatches_pushed_value(bt_constants):
    fake = FakeAttSocket()

    async def run():
        client = L2capAttClient("AA:BB:CC:DD:EE:FF", socket_factory=lambda *a: fake)
        await client.connect()
        got: list[bytes] = []
        await client.start_notify(VOL_UUID, lambda v: got.append(bytes(v)))
        # subscribing writes 0x0001 to the CCC
        assert (0x0004, struct.pack("<H", 0x0001)) in fake.writes
        # speaker pushes a knob update on the value handle
        fake.push_notify(0x0003, bytes([7]))
        await asyncio.sleep(0.2)
        assert got == [bytes([7])]
        await client.disconnect()

    asyncio.run(run())


def test_unknown_characteristic_raises(bt_constants):
    fake = FakeAttSocket()

    async def run():
        client = L2capAttClient("AA:BB:CC:DD:EE:FF", socket_factory=lambda *a: fake)
        await client.connect()
        with pytest.raises(AttError):
            await client.read_gatt_char("00000000-0000-1000-8000-00805f9b34fb")
        await client.disconnect()

    asyncio.run(run())


def test_factory_builds_client():
    c = l2cap_factory("AA:BB:CC:DD:EE:FF", 5.0)
    assert isinstance(c, L2capAttClient)
    assert c.timeout == 5.0
