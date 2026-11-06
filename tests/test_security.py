"""Security regression tests (written before the fixes they cover)."""

import asyncio

import pytest
from yarl import URL

from weatherbridge.app import STATE, classify, upstream_host
from weatherbridge.config import Config
from weatherbridge.esphome import ESPHomeServer
from weatherbridge.state import ProxyState

WU_HOST = {"Host": "rtupdate.wunderground.com"}
WU_URL = URL("/weatherstation/updateweatherstation.php?ID=X&PASSWORD=Y&tempf=50", encoded=True)


# --------------------------------------------------------------------------- forwarding target

@pytest.mark.parametrize("host,expected", [
    ("rtupdate.wunderground.com", "rtupdate.wunderground.com"),
    ("RTUPDATE.WUNDERGROUND.COM:80", "rtupdate.wunderground.com"),
    ("wunderground.com", "wunderground.com"),
    ("evilwunderground.com", "rtupdate.wunderground.com"),          # no dot boundary
    ("evil.com/x.wunderground.com", "rtupdate.wunderground.com"),   # path smuggled in Host
    ("evil.com?.wunderground.com", "rtupdate.wunderground.com"),
    ("evil.com#.wunderground.com", "rtupdate.wunderground.com"),
    ("user@evil.com.wunderground.com", "rtupdate.wunderground.com"),
    ("192.168.1.1", "rtupdate.wunderground.com"),
    ("", "rtupdate.wunderground.com"),
])
def test_upstream_host_only_service_domain(host, expected):
    assert upstream_host("wu", host) == expected


def test_upstream_host_weathercloud():
    assert upstream_host("wc", "api.weathercloud.net") == "api.weathercloud.net"
    assert upstream_host("wc", "notweathercloud.net") == "api.weathercloud.net"


def test_classify_requires_dot_boundary():
    assert classify("evilwunderground.com", "/something") is None
    assert classify("x.wunderground.com", "/something") == "wu"


async def test_forward_never_targets_smuggled_host(make_cfg, make_client, upstream):
    client, _, _ = await make_client(make_cfg(upstream_port=upstream.port))
    await client.get(WU_URL, headers={"Host": "evil.com/x.wunderground.com"})
    assert upstream.requests[0]["host"] == "rtupdate.wunderground.com"


async def test_oversized_upstream_body_is_not_passed_or_recorded(make_cfg, make_client, upstream):
    upstream.body = "x" * (256 * 1024)
    cfg = make_cfg(upstream_port=upstream.port)
    client, app, _ = await make_client(cfg)
    r = await client.get(WU_URL, headers=WU_HOST)
    assert r.status == 200 and (await r.text()).strip() == "success"
    assert app[STATE].upstream_status["wu"] == "error"
    assert not (cfg.data_dir / "responses" / "wu.json").exists()


# --------------------------------------------------------------------------- config validation

@pytest.mark.parametrize("field,value", [
    ("device_id", "weather/station"),
    ("device_id", "weather+station"),
    ("device_id", ""),
    ("base_topic", "meteo/#"),
    ("base_topic", "meteo/+/x"),
    ("discovery_prefix", "home#assistant"),
])
def test_config_rejects_unsafe_identifiers(field, value):
    cfg = Config(mqtt_enabled=True, mqtt_host="h")
    setattr(cfg, field, value)
    assert cfg.validate(), f"{field}={value!r} should be rejected"


def test_config_accepts_defaults():
    assert Config(mqtt_enabled=True, mqtt_host="h", esphome_enabled=True).validate() == []


# --------------------------------------------------------------------------- ESPHome connection hygiene

@pytest.fixture
async def esphome(tmp_path):
    cfg = Config(data_dir=tmp_path, esphome_enabled=True, esphome_mdns=False)
    server = ESPHomeServer(cfg, ProxyState(tmp_path, {"wu": True, "wc": True}))
    server.handshake_timeout = 0.3
    server.idle_timeout = 0.5
    server.max_clients = 2
    port = await server.start(host="127.0.0.1", port=0)
    yield server, port
    await server.stop()


async def _closed_within(reader, seconds):
    data = await asyncio.wait_for(reader.read(1024), seconds)
    return data


async def test_silent_connection_is_dropped(esphome):
    _, port = esphome
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    assert await _closed_within(reader, 2) == b""  # EOF, server closed it
    writer.close()


async def test_messages_before_hello_are_rejected(esphome):
    _, port = esphome
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x00\x00\x09")  # plaintext DeviceInfoRequest (type 9, empty) without Hello
    await writer.drain()
    assert await _closed_within(reader, 2) == b""
    writer.close()


async def test_idle_connection_after_hello_is_dropped(esphome):
    from aioesphomeapi import api_pb2 as pb
    _, port = esphome
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = pb.HelloRequest(client_info="t", api_version_major=1, api_version_minor=10).SerializeToString()
    writer.write(b"\x00" + bytes([len(payload), 1]) + payload)
    await writer.drain()
    first = await asyncio.wait_for(reader.read(1024), 2)
    assert first[:1] == b"\x00"  # HelloResponse
    assert await _closed_within(reader, 3) == b""  # then idle timeout
    writer.close()


async def test_connection_limit(esphome):
    _, port = esphome
    conns = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
    await asyncio.sleep(0.05)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    assert await asyncio.wait_for(reader.read(1024), 0.25) == b""  # rejected before the timeout
    for _, w in conns:
        w.close()
    writer.close()
