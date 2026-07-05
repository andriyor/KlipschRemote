"""Linux-only BR/EDR raw-ATT backend implementing the :class:`BleakLike` seam.

Why this exists: Klipsch powered speakers serve their control GATT over the
**BR/EDR (Classic)** transport — an L2CAP channel on the ATT PSM ``0x1F`` — not
over Bluetooth LE. bleak on Linux talks GATT over *LE* (BlueZ, ATT CID 4), which
the speaker never answers, so every read fails with ``org.bluez.Error.Failed:
Not connected``. (On Windows/macOS the LE path works because those stacks bond
LE from the Classic audio pairing via CTKD; Linux/BlueZ does not persist that LE
key, so LE is a dead end here.)

This backend sidesteps all of that: it opens a raw Linux ``AF_BLUETOOTH`` L2CAP
socket to the speaker's ATT PSM and speaks the ATT wire protocol directly —
discovery, read (with Read Blob for long values), write, and value
notifications. It needs no root: just a live Classic ACL link, which the socket
``connect()`` establishes on its own for a bonded (audio-paired) speaker.

It is selected automatically as the default factory on Linux (see
``KlipschClient``). The public client code only ever talks to it through the four
``BleakLike`` methods. Never pair()/unpair() — the Classic audio bond is what
lets the L2CAP link come up.

Live notifications: ``start_notify`` is a correct ATT subscription (it writes the
CCC and the reader dispatches any Handle Value Notification), but the current
Klipsch firmware only *emits* those notifications over the LE bearer — never over
BR/EDR. Confirmed on hardware: subscribing to every notify-capable characteristic
and turning the physical volume knob produces zero notifications on this
transport. So the live knob/mute mirror is inert on Linux; everything else (every
read and write) works. All notification plumbing is kept intact in case a future
firmware pushes over BR/EDR — nothing here is artificially disabled.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import threading
from typing import Callable

# --- ATT protocol constants --------------------------------------------------
_PSM_ATT = 0x1F  # 31 — the assigned L2CAP PSM for ATT over BR/EDR

_OP_ERROR = 0x01
_OP_MTU_REQ = 0x02
_OP_MTU_RSP = 0x03
_OP_FIND_INFO_REQ = 0x04
_OP_FIND_INFO_RSP = 0x05
_OP_READ_BY_TYPE_REQ = 0x08
_OP_READ_BY_TYPE_RSP = 0x09
_OP_READ_REQ = 0x0A
_OP_READ_RSP = 0x0B
_OP_READ_BLOB_REQ = 0x0C
_OP_READ_BLOB_RSP = 0x0D
_OP_WRITE_REQ = 0x12
_OP_WRITE_RSP = 0x13
_OP_WRITE_CMD = 0x52
_OP_HANDLE_VALUE_NOTIFY = 0x1B
_OP_HANDLE_VALUE_IND = 0x1D
_OP_HANDLE_VALUE_CONFIRM = 0x1E

_UUID_CHAR_DECL = 0x2803  # «Characteristic» declaration
_UUID_CCC = 0x2902  # Client Characteristic Configuration descriptor

# Characteristic property bits (from the declaration).
_PROP_NOTIFY = 0x10
_PROP_INDICATE = 0x20

_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"


class AttError(Exception):
    """ATT transport or protocol failure."""


def _uuid_from_le(raw: bytes) -> str:
    """Normalise a little-endian ATT UUID (2 or 16 bytes) to canonical form."""
    if len(raw) == 2:
        return f"0000{struct.unpack('<H', raw)[0]:04x}{_BASE_SUFFIX}"
    if len(raw) == 16:
        h = raw[::-1].hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    raise AttError(f"unexpected UUID length {len(raw)}")


def _norm_uuid(uuid: str) -> str:
    """Normalise a caller-supplied UUID to the same canonical form as discovery."""
    u = uuid.lower()
    if len(u) == 4:  # bare 16-bit, e.g. "2a24"
        return f"0000{u}{_BASE_SUFFIX}"
    if len(u) == 8:  # bare 32-bit
        return f"{u}{_BASE_SUFFIX}"
    return u


class L2capAttClient:
    """Minimal ATT-over-BR/EDR client matching the subset of bleak we rely on.

    ``target`` is either a MAC string or a bleak ``BLEDevice`` (passed by the
    scan-prime fallback); in both cases we connect over Classic by address.
    """

    def __init__(
        self,
        target: object,
        timeout: float = 10.0,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self._address = target if isinstance(target, str) else getattr(target, "address", None)
        if not self._address:
            raise ValueError("L2CAP backend needs a Bluetooth address")
        self.timeout = timeout
        self._socket_factory = socket_factory

        self._sock: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: threading.Thread | None = None
        self._closing = False
        self._mtu = 23

        # one outstanding ATT request at a time (ATT is a serial request/response
        # protocol); the reader thread completes it.
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future | None = None

        # discovery results, keyed by canonical UUID
        self._handle: dict[str, int] = {}
        self._props: dict[str, int] = {}
        self._ccc: dict[str, int] = {}
        # active notifications, keyed by *value handle*
        self._notify: dict[int, Callable[..., None]] = {}

    # --- BleakLike -----------------------------------------------------------
    async def connect(self) -> L2capAttClient:
        if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_L2CAP"):
            raise AttError("this Python build has no Linux Bluetooth L2CAP support")
        self._loop = asyncio.get_running_loop()
        sock = self._socket_factory(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
        )
        sock.settimeout(self.timeout)
        try:
            await self._loop.run_in_executor(None, sock.connect, (self._address, _PSM_ATT))
        except OSError as exc:
            sock.close()
            raise AttError(
                f"could not open the Classic ATT channel to {self._address} "
                f"({exc}). Make sure the speaker is paired as an audio device and "
                f"reachable (e.g. `bluetoothctl connect {self._address}`)."
            ) from exc
        # switch to a short recv timeout so the reader thread can notice close.
        sock.settimeout(1.0)
        self._sock = sock
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, name="klipsch-att", daemon=True)
        self._reader.start()
        await self._exchange_mtu()
        await self._discover()
        return self

    async def disconnect(self) -> None:
        self._closing = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=2.0)
        # fail any in-flight request
        fut = self._pending
        if fut is not None and self._loop is not None and not fut.done():
            self._loop.call_soon_threadsafe(fut.cancel)

    async def read_gatt_char(self, char: str) -> bytearray:
        handle = self._value_handle(char)
        rsp = await self._request(bytes([_OP_READ_REQ]) + struct.pack("<H", handle))
        self._raise_on_error(rsp, "read")
        if not rsp or rsp[0] != _OP_READ_RSP:
            raise AttError(f"unexpected read response 0x{rsp[0]:02x}" if rsp else "empty read")
        value = bytearray(rsp[1:])
        # A full (MTU-1) payload may be truncated — pull the rest with Read Blob.
        while len(value) and len(rsp) - 1 == self._mtu - 1:
            rsp = await self._request(
                bytes([_OP_READ_BLOB_REQ]) + struct.pack("<HH", handle, len(value))
            )
            if not rsp or rsp[0] != _OP_READ_BLOB_RSP or len(rsp) == 1:
                break
            value += rsp[1:]
        return value

    async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:
        handle = self._value_handle(char)
        if response:
            rsp = await self._request(
                bytes([_OP_WRITE_REQ]) + struct.pack("<H", handle) + bytes(data)
            )
            self._raise_on_error(rsp, "write")
            if rsp != bytes([_OP_WRITE_RSP]):
                raise AttError("write not acknowledged")
        else:
            self._send(bytes([_OP_WRITE_CMD]) + struct.pack("<H", handle) + bytes(data))

    async def start_notify(self, char: str, callback: Callable[..., None]) -> None:
        key = _norm_uuid(char)
        handle = self._value_handle(char)
        ccc = self._ccc.get(key)
        if ccc is None:
            raise AttError(f"{char} has no CCC descriptor — cannot subscribe")
        self._notify[handle] = callback
        indicate = bool(self._props.get(key, 0) & _PROP_INDICATE) and not (
            self._props.get(key, 0) & _PROP_NOTIFY
        )
        value = 0x0002 if indicate else 0x0001
        rsp = await self._request(
            bytes([_OP_WRITE_REQ]) + struct.pack("<HH", ccc, value)
        )
        self._raise_on_error(rsp, "subscribe")

    async def stop_notify(self, char: str) -> None:
        key = _norm_uuid(char)
        handle = self._handle.get(key)
        ccc = self._ccc.get(key)
        if handle is not None:
            self._notify.pop(handle, None)
        if ccc is not None:
            try:
                await self._request(bytes([_OP_WRITE_REQ]) + struct.pack("<HH", ccc, 0x0000))
            except AttError:
                pass

    async def __aenter__(self) -> L2capAttClient:
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    # --- ATT plumbing --------------------------------------------------------
    async def _exchange_mtu(self) -> None:
        try:
            rsp = await self._request(bytes([_OP_MTU_REQ]) + struct.pack("<H", 517))
        except AttError:
            self._mtu = 23
            return
        if rsp and rsp[0] == _OP_MTU_RSP:
            self._mtu = max(23, min(517, struct.unpack("<H", rsp[1:3])[0]))

    async def _discover(self) -> None:
        """Build UUID→handle maps by walking the ATT attribute database."""
        chars: list[tuple[int, int, str]] = []  # (decl_handle, value_handle, uuid)
        start = 0x0001
        while start <= 0xFFFF:
            rsp = await self._request(
                bytes([_OP_READ_BY_TYPE_REQ])
                + struct.pack("<HH", start, 0xFFFF)
                + struct.pack("<H", _UUID_CHAR_DECL)
            )
            if not rsp or rsp[0] != _OP_READ_BY_TYPE_RSP:
                break  # 0x01 error = «attribute not found» → end of sweep
            length, body = rsp[1], rsp[2:]
            last = start
            for i in range(0, len(body) - length + 1, length):
                rec = body[i : i + length]
                decl = struct.unpack("<H", rec[0:2])[0]
                props = rec[2]
                vh = struct.unpack("<H", rec[3:5])[0]
                uuid = _uuid_from_le(rec[5:length])
                self._handle[uuid] = vh
                self._props[uuid] = props
                chars.append((decl, vh, uuid))
                last = decl
            start = last + 1

        # Full Find Information sweep → every (handle, uuid), used to locate the
        # CCC (0x2902) descriptor that belongs to each notify/indicate char.
        attrs: dict[int, str] = {}
        start = 0x0001
        while start <= 0xFFFF:
            rsp = await self._request(
                bytes([_OP_FIND_INFO_REQ]) + struct.pack("<HH", start, 0xFFFF)
            )
            if not rsp or rsp[0] != _OP_FIND_INFO_RSP:
                break
            fmt, body = rsp[1], rsp[2:]
            step = 4 if fmt == 1 else 18
            last = start
            for i in range(0, len(body) - step + 1, step):
                h = struct.unpack("<H", body[i : i + 2])[0]
                attrs[h] = _uuid_from_le(body[i + 2 : i + step])
                last = h
            start = last + 1

        chars.sort(key=lambda c: c[0])
        ccc_key = f"0000{_UUID_CCC:04x}{_BASE_SUFFIX}"
        for idx, (_decl, vh, uuid) in enumerate(chars):
            end = chars[idx + 1][0] if idx + 1 < len(chars) else 0xFFFF
            for h in range(vh + 1, end):
                if attrs.get(h) == ccc_key:
                    self._ccc[uuid] = h
                    break

    def _value_handle(self, char: str) -> int:
        handle = self._handle.get(_norm_uuid(char))
        if handle is None:
            raise AttError(f"characteristic {char} not found on this speaker")
        return handle

    @staticmethod
    def _raise_on_error(rsp: bytes | None, what: str) -> None:
        if rsp and rsp[0] == _OP_ERROR:
            # rsp = [0x01, req_opcode, handle_lo, handle_hi, error_code]
            code = rsp[4] if len(rsp) >= 5 else 0
            raise AttError(f"ATT {what} failed (error 0x{code:02x})")

    def _send(self, pdu: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise AttError("ATT socket is not connected")
        sock.send(pdu)

    async def _request(self, pdu: bytes) -> bytes:
        async with self._lock:
            if self._sock is None or self._loop is None:
                raise AttError("ATT socket is not connected")
            fut: asyncio.Future = self._loop.create_future()
            self._pending = fut
            try:
                self._send(pdu)
                return await asyncio.wait_for(fut, self.timeout)
            except asyncio.TimeoutError as exc:
                # ATT can't match a late response to its request, so a timed-out
                # bearer is unsafe to reuse: a delayed reply would resolve the
                # *next* request's future (Core spec Vol 3, Part F §3.3.3 —
                # disconnect on transaction timeout). Kill the socket so any
                # further request fails cleanly instead of desyncing.
                self._closing = True
                sock, self._sock = self._sock, None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                raise AttError("ATT request timed out; bearer closed") from exc
            finally:
                self._pending = None

    # --- reader thread -------------------------------------------------------
    def _read_loop(self) -> None:
        sock = self._sock
        loop = self._loop
        while not self._closing and sock is not None and loop is not None:
            try:
                pdu = sock.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not pdu:
                break
            op = pdu[0]
            if op in (_OP_HANDLE_VALUE_NOTIFY, _OP_HANDLE_VALUE_IND):
                handle = struct.unpack("<H", pdu[1:3])[0]
                value = bytearray(pdu[3:])
                if op == _OP_HANDLE_VALUE_IND:
                    try:
                        sock.send(bytes([_OP_HANDLE_VALUE_CONFIRM]))
                    except OSError:
                        pass
                cb = self._notify.get(handle)
                if cb is not None:
                    loop.call_soon_threadsafe(self._dispatch, cb, value)
            else:
                fut = self._pending
                if fut is not None:
                    loop.call_soon_threadsafe(_resolve, fut, bytes(pdu))

    @staticmethod
    def _dispatch(cb: Callable[..., None], value: bytearray) -> None:
        try:
            cb(value)
        except Exception:  # a misbehaving callback must not kill the loop
            pass


def _resolve(fut: asyncio.Future, value: bytes) -> None:
    if not fut.done():
        fut.set_result(value)


def l2cap_factory(target: object, timeout: float) -> L2capAttClient:
    """Build an unconnected Linux BR/EDR ATT client (the ``ClientFactory`` seam)."""
    return L2capAttClient(target, timeout=timeout)
