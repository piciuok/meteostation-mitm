"""Minimal ESPHome Native API server: exposes the station as an ESPHome device.

Home Assistant connects to TCP 6053 like to any ESPHome device (added by IP or found via mDNS).
Plaintext and Noise (Noise_NNpsk0_25519_ChaChaPoly_SHA256, same as ESPHome `api.encryption.key`)
framing are supported. Protobuf messages come from the pinned `aioesphomeapi` package, which
is the client library Home Assistant itself uses.

Protocol references: aioesphomeapi `_frame_helper/{plain_text,noise}.py`, ESPHome
`components/api/api_frame_helper_*.cpp`.
"""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import logging
import socket
import zlib
from dataclasses import dataclass
from typing import Any, Callable

from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from google.protobuf.message import Message
from noise.connection import NoiseConnection

from . import __version__
from .config import Config
from .parsing import FIELDS, FIELDS_BY_KEY
from .state import ProxyState

log = logging.getLogger(__name__)

PROTO_TO_TYPE: dict[type, int] = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}

# API 1.14: newer clients would ask for DeviceCapabilitiesRequest (>= 1.15), not needed here.
API_VERSION = (1, 14)
# Reported as esphome_version; HA does not enforce a minimum.
ESPHOME_VERSION = "2026.10.0"
NOISE_PROTOCOL = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
NOISE_PROLOGUE = b"NoiseAPIInit"

# Hello (and the Noise handshake) must complete within this time.
HANDSHAKE_TIMEOUT = 10.0
# HA pings every ~20 s (aioesphomeapi keepalive); a silent client is dropped after this.
IDLE_TIMEOUT = 120.0
# Concurrent connections (HA, ESPHome dashboard, a few CLI clients).
MAX_CLIENTS = 8
# A client that does not read its data is dropped once this much is buffered.
MAX_WRITE_BUFFER = 256 * 1024

CAT_NONE, CAT_CONFIG, CAT_DIAGNOSTIC = 0, 1, 2
STATE_CLASS = {None: 0, "measurement": 1, "total_increasing": 2, "total": 3}

SWITCHES = {
    "wu": ("forward_to_wu", "Forward to WU"),
    "wc": ("forward_to_weathercloud", "Forward to Weathercloud"),
}
TEXT_SENSORS = (
    # object_id, name, device_class, icon
    ("last_reading", "Last reading", "timestamp", ""),
    ("upstream_wu_status", "Upstream WU status", "", "mdi:cloud-check-outline"),
    ("upstream_wc_status", "Upstream WC status", "", "mdi:cloud-check-outline"),
)


# Messages a client may send before HelloRequest.
PRE_HELLO = (pb.HelloRequest, pb.PingRequest, pb.DisconnectRequest)


class ProtocolError(Exception):
    pass


def entity_key(object_id: str) -> int:
    """Stable 32-bit key per entity (ESPHome uses a hash of the object id too)."""
    return zlib.crc32(object_id.encode()) or 1


def default_mac(device_id: str) -> str:
    """Deterministic locally administered MAC derived from DEVICE_ID."""
    digest = hashlib.sha256(device_id.encode()).digest()
    octets = [0x02 | (digest[0] & 0xFC)] + list(digest[1:6])
    return ":".join(f"{o:02X}" for o in octets)


def decode_psk(key: str) -> bytes:
    try:
        raw = binascii.a2b_base64(key.strip())
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ESPHOME_ENCRYPTION_KEY is not valid base64") from exc
    if len(raw) != 32:
        raise ValueError("ESPHOME_ENCRYPTION_KEY must be base64 of exactly 32 bytes")
    return raw


# --------------------------------------------------------------------------- framing

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    result = shift = 0
    while True:
        b = (await reader.readexactly(1))[0]
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result
        shift += 7
        if shift > 35:
            raise ProtocolError("varint too long")


class PlainFrames:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 first_preamble: int | None = None) -> None:
        self.reader, self.writer = reader, writer
        self._pending = first_preamble

    async def read(self) -> tuple[int, bytes]:
        if self._pending is not None:
            preamble, self._pending = self._pending, None
        else:
            preamble = (await self.reader.readexactly(1))[0]
        if preamble != 0x00:
            raise ProtocolError(f"unexpected preamble {preamble}")
        length = await _read_varint(self.reader)
        msg_type = await _read_varint(self.reader)
        if length > 65535:
            raise ProtocolError("frame too large")
        return msg_type, await self.reader.readexactly(length)

    def write(self, msg_type: int, payload: bytes) -> None:
        self.writer.write(b"\x00" + _varint(len(payload)) + _varint(msg_type) + payload)


class NoiseFrames:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 proto: NoiseConnection) -> None:
        self.reader, self.writer, self.proto = reader, writer, proto

    @staticmethod
    async def read_frame(reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(3)
        if header[0] != 0x01:
            raise ProtocolError(f"unexpected preamble {header[0]}")
        return await reader.readexactly((header[1] << 8) | header[2])

    @staticmethod
    def frame(payload: bytes) -> bytes:
        return bytes((0x01, len(payload) >> 8, len(payload) & 0xFF)) + payload

    async def read(self) -> tuple[int, bytes]:
        msg = self.proto.decrypt(await self.read_frame(self.reader))
        if len(msg) < 4:
            raise ProtocolError("decrypted message too short")
        return (msg[0] << 8) | msg[1], bytes(msg[4:])

    def write(self, msg_type: int, payload: bytes) -> None:
        data = bytes((msg_type >> 8, msg_type & 0xFF, len(payload) >> 8, len(payload) & 0xFF))
        self.writer.write(self.frame(self.proto.encrypt(data + payload)))


# --------------------------------------------------------------------------- server

@dataclass(eq=False)
class _Client:
    frames: Any
    writer: asyncio.StreamWriter
    peer: str
    subscribed: bool = False
    hello: bool = False
    listed: frozenset[str] = frozenset()


class ESPHomeServer:
    def __init__(self, cfg: Config, state: ProxyState,
                 on_forward_change: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.on_forward_change = on_forward_change
        self.name = cfg.esphome_name
        self.mac = (cfg.esphome_mac or default_mac(cfg.device_id)).upper()
        self.psk = decode_psk(cfg.esphome_encryption_key) if cfg.esphome_encryption_key else None
        self.clients: set[_Client] = set()
        self._server: asyncio.Server | None = None
        self._zeroconf: Any = None
        self.port: int | None = None
        # Connection hygiene (attributes so tests can shorten them).
        self.handshake_timeout = HANDSHAKE_TIMEOUT
        self.idle_timeout = IDLE_TIMEOUT
        self.max_clients = MAX_CLIENTS
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def encrypted(self) -> bool:
        return self.psk is not None

    @property
    def connected(self) -> bool:
        return any(c.subscribed for c in self.clients)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, host: str = "0.0.0.0", port: int | None = None) -> int:
        self._server = await asyncio.start_server(self._handle, host, port or self.cfg.esphome_port)
        bound = self._server.sockets[0].getsockname()[1]
        self.port = bound
        log.info("ESPHome API listening on port %s (%s), name=%s mac=%s", bound,
                 "encrypted" if self.encrypted else "plaintext", self.name, self.mac)
        if self.cfg.esphome_mdns:
            await self._start_mdns(bound)
        return bound

    async def stop(self) -> None:
        if self._zeroconf is not None:
            try:
                await self._zeroconf.async_close()
            except Exception:  # pragma: no cover
                pass
        if self._server is not None:
            self._server.close()
        for w in list(self._writers):
            w.close()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except asyncio.TimeoutError:  # pragma: no cover
                log.warning("ESPHome: connections did not close in time")

    async def _start_mdns(self, port: int) -> None:
        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

            txt = {
                "version": ESPHOME_VERSION,
                "mac": self.mac.replace(":", "").lower(),
                "friendly_name": self.cfg.device_name,
                "platform": "host",
                "board": "host",
                "network": "ethernet",
                "project_name": "vevor.weatherbridge",
                "project_version": __version__,
            }
            if self.encrypted:
                txt["api_encryption"] = NOISE_PROTOCOL.decode()
            info = AsyncServiceInfo(
                "_esphomelib._tcp.local.",
                f"{self.name}._esphomelib._tcp.local.",
                port=port,
                properties=txt,
                server=f"{self.name}.local.",
                addresses=[socket.inet_aton(ip) for ip in self._local_ips()],
            )
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await self._zeroconf.async_register_service(info)
            log.info("mDNS: advertised %s._esphomelib._tcp", self.name)
        except Exception as exc:  # mDNS is optional; adding the device by IP still works
            log.warning("mDNS advertisement failed (%s); add the device in HA by IP", exc)

    @staticmethod
    def _local_ips() -> list[str]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
                return [s.getsockname()[0]]
        except OSError:
            return []

    # ------------------------------------------------------------------ entities
    def _units(self, f) -> str:
        return (f.metric_unit if self.cfg.units == "metric" else f.imperial_unit) or ""

    def sensor_keys(self) -> list[str]:
        return [f.key for f in FIELDS if f.key in self.state.sensors]

    def entity_messages(self) -> list[Message]:
        msgs: list[Message] = []
        for key in self.sensor_keys():
            f = FIELDS_BY_KEY[key]
            digits = 0 if f.integer else (1 if self.cfg.units == "metric" else f.imperial_digits)
            msgs.append(pb.ListEntitiesSensorResponse(
                object_id=key, key=entity_key(key), name=f.name, icon=f.icon or "",
                unit_of_measurement=self._units(f), accuracy_decimals=digits,
                device_class=f.device_class or "", state_class=STATE_CLASS[f.state_class],
            ))
        for oid, name, dclass, icon in TEXT_SENSORS:
            msgs.append(pb.ListEntitiesTextSensorResponse(
                object_id=oid, key=entity_key(oid), name=name, icon=icon,
                device_class=dclass, entity_category=CAT_DIAGNOSTIC,
            ))
        for oid, name in SWITCHES.values():
            msgs.append(pb.ListEntitiesSwitchResponse(
                object_id=oid, key=entity_key(oid), name=name, icon="mdi:cloud-upload-outline",
                entity_category=CAT_CONFIG,
            ))
        return msgs

    def state_messages(self) -> list[Message]:
        msgs: list[Message] = []
        for key in self.sensor_keys():
            msgs.append(pb.SensorStateResponse(key=entity_key(key), state=float(self.state.sensors[key])))
        texts = {
            "last_reading": self.state.last_reading,
            "upstream_wu_status": self.state.upstream_status["wu"],
            "upstream_wc_status": self.state.upstream_status["wc"],
        }
        for oid, value in texts.items():
            if value is None:
                msgs.append(pb.TextSensorStateResponse(key=entity_key(oid), missing_state=True))
            else:
                msgs.append(pb.TextSensorStateResponse(key=entity_key(oid), state=str(value)))
        for svc, (oid, _) in SWITCHES.items():
            msgs.append(pb.SwitchStateResponse(key=entity_key(oid), state=self.state.forward(svc)))
        return msgs

    # ------------------------------------------------------------------ updates
    async def publish_update(self) -> None:
        """Push current states; reconnect clients whose entity list is outdated."""
        current = frozenset(self.sensor_keys())
        for c in list(self.clients):
            if not c.subscribed:
                continue
            if c.listed != current:
                # New sensor appeared: ESPHome entity lists are static per connection, so
                # make HA reconnect and list entities again (like after a firmware update).
                log.info("ESPHome: entity list changed, asking %s to reconnect", c.peer)
                self._send(c, pb.DisconnectRequest())
                c.writer.close()
                continue
            for m in self.state_messages():
                self._send(c, m)

    def _send(self, c: _Client, msg: Message) -> None:
        try:
            transport = c.writer.transport
            if transport is not None and transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
                log.warning("ESPHome: client %s is not reading, dropping it", c.peer)
                c.writer.close()
                return
            c.frames.write(PROTO_TO_TYPE[type(msg)], msg.SerializeToString())
        except Exception as exc:
            log.debug("ESPHome send to %s failed: %s", c.peer, exc)

    # ------------------------------------------------------------------ connection
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = str(writer.get_extra_info("peername", ("?",))[0])
        client: _Client | None = None
        if len(self._writers) >= self.max_clients:
            log.warning("ESPHome: connection limit (%d) reached, rejecting %s", self.max_clients, peer)
            writer.close()
            return
        self._writers.add(writer)
        try:
            start = self._noise_handshake(reader, writer) if self.encrypted else self._plain_start(reader, writer)
            frames = await asyncio.wait_for(start, self.handshake_timeout)
            if frames is None:
                return
            client = _Client(frames, writer, peer)
            self.clients.add(client)
            while True:
                timeout = self.idle_timeout if client.hello else self.handshake_timeout
                msg_type, payload = await asyncio.wait_for(frames.read(), timeout)
                if not await self._dispatch(client, msg_type, payload):
                    break
                await writer.drain()
        except asyncio.TimeoutError:
            log.info("ESPHome: closing inactive connection from %s", peer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except ProtocolError as exc:
            log.warning("ESPHome: protocol error from %s: %s", peer, exc)
        except Exception as exc:
            log.warning("ESPHome: connection from %s failed: %s", peer, exc)
        finally:
            if client is not None:
                self.clients.discard(client)
                if client.subscribed:
                    log.info("ESPHome: client %s disconnected", peer)
            self._writers.discard(writer)
            writer.close()

    async def _plain_start(self, reader, writer) -> PlainFrames | None:
        first = await reader.readexactly(1)
        if first[0] != 0x00:
            log.warning("ESPHome: non-plaintext client (encryption enabled in HA?); closing")
            return None
        return PlainFrames(reader, writer, first_preamble=first[0])

    async def _noise_handshake(self, reader, writer) -> NoiseFrames | None:
        first = await reader.readexactly(1)
        if first[0] != 0x01:
            # Plaintext client talking to an encrypted device: answer with an encrypted-style
            # frame so the client reports "requires encryption" (as real ESPHome does).
            writer.write(NoiseFrames.frame(b"\x01Bad indicator byte"))
            await writer.drain()
            return None
        rest = await reader.readexactly(2)
        client_hello = await reader.readexactly((rest[0] << 8) | rest[1])
        proto = NoiseConnection.from_name(NOISE_PROTOCOL)
        proto.set_as_responder()
        proto.set_psks(self.psk)
        proto.set_prologue(NOISE_PROLOGUE + len(client_hello).to_bytes(2, "big") + client_hello)
        proto.start_handshake()
        server_hello = (b"\x01" + self.name.encode() + b"\x00"
                        + self.mac.replace(":", "").lower().encode() + b"\x00")
        writer.write(NoiseFrames.frame(server_hello))
        handshake = await NoiseFrames.read_frame(reader)
        if not handshake or handshake[0] != 0x00:
            raise ProtocolError("invalid handshake frame")
        try:
            proto.read_message(handshake[1:])
        except Exception:
            log.warning("ESPHome: handshake failed (wrong encryption key in HA?)")
            writer.write(NoiseFrames.frame(b"\x01Handshake MAC failure"))
            await writer.drain()
            return None
        writer.write(NoiseFrames.frame(b"\x00" + proto.write_message()))
        await writer.drain()
        return NoiseFrames(reader, writer, proto)

    async def _dispatch(self, c: _Client, msg_type: int, payload: bytes) -> bool:
        cls = MESSAGE_TYPE_TO_PROTO.get(msg_type)
        if not c.hello and cls not in PRE_HELLO:
            log.warning("ESPHome: %s sent %s before HelloRequest; closing",
                        c.peer, cls.__name__ if cls else msg_type)
            return False
        if cls is pb.HelloRequest:
            c.hello = True
            req = pb.HelloRequest.FromString(payload)
            log.debug("ESPHome: hello from %s (%s)", c.peer, req.client_info)
            self._send(c, pb.HelloResponse(api_version_major=API_VERSION[0],
                                           api_version_minor=API_VERSION[1],
                                           server_info=f"vevor-weatherbridge {__version__}",
                                           name=self.name))
        elif cls is pb.DeviceInfoRequest:
            self._send(c, pb.DeviceInfoResponse(
                name=self.name, friendly_name=self.cfg.device_name, mac_address=self.mac,
                esphome_version=ESPHOME_VERSION, compilation_time="",
                model=self.cfg.device_model, manufacturer=self.cfg.device_manufacturer,
                project_name="vevor.weatherbridge", project_version=__version__,
                api_encryption_supported=self.encrypted,
            ))
        elif cls is pb.ListEntitiesRequest:
            c.listed = frozenset(self.sensor_keys())
            for m in self.entity_messages():
                self._send(c, m)
            self._send(c, pb.ListEntitiesDoneResponse())
        elif cls is pb.SubscribeStatesRequest:
            c.subscribed = True
            log.info("ESPHome: client %s subscribed", c.peer)
            for m in self.state_messages():
                self._send(c, m)
        elif cls is pb.SwitchCommandRequest:
            req = pb.SwitchCommandRequest.FromString(payload)
            for svc, (oid, _) in SWITCHES.items():
                if req.key == entity_key(oid):
                    self.state.set_forward(svc, bool(req.state))
                    log.info("Forward %s set to %s from ESPHome", svc.upper(), "ON" if req.state else "OFF")
                    if self.on_forward_change:
                        self.on_forward_change()
                    else:
                        await self.publish_update()
        elif cls is pb.PingRequest:
            self._send(c, pb.PingResponse())
        elif cls is pb.DisconnectRequest:
            self._send(c, pb.DisconnectResponse())
            await c.writer.drain()
            return False
        elif cls is pb.DisconnectResponse:
            return False
        # Everything else (auth, logs, HA states/services subscriptions, time) is ignored.
        return True
