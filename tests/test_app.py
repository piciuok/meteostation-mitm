import json
import logging

from yarl import URL

from weatherbridge.app import STATE, create_app
from weatherbridge.mqtt import HABridge
from weatherbridge.state import ProxyState

from .conftest import (SECRETS, WC_PATH, WU_ID, WU_PASS, WU_QUERY, FakeMQTT, drain, free_port,
                       read_all_data)

WU_HOST = {"Host": "rtupdate.wunderground.com", "User-Agent": "ESP8266"}
WC_HOST = {"Host": "api.weathercloud.net"}
WU_URL = URL("/weatherstation/updateweatherstation.php?" + WU_QUERY, encoded=True)


async def test_forward_on_wu(make_cfg, make_client, upstream):
    upstream.body = "success-from-upstream\n"
    cfg = make_cfg(upstream_port=upstream.port)
    client, app, _ = await make_client(cfg)
    r = await client.get(WU_URL, headers=WU_HOST)
    assert r.status == 200
    assert await r.text() == "success-from-upstream\n"
    assert r.headers["Content-Type"].startswith("text/html")
    # upstream got the original console request, untouched
    assert len(upstream.requests) == 1
    req = upstream.requests[0]
    assert req["host"] == "rtupdate.wunderground.com"
    assert req["path"] == "/weatherstation/updateweatherstation.php"
    assert req["query"] == WU_QUERY
    rec = json.loads((cfg.data_dir / "responses" / "wu.json").read_text())
    assert rec["status"] == 200 and rec["body"] == "success-from-upstream\n"
    assert rec["content_type"].startswith("text/html") and rec["recorded_at"]
    assert app[STATE].upstream_status["wu"] == 200


async def test_forward_on_wc(make_cfg, make_client, upstream):
    upstream.body, upstream.content_type = "200", "text/plain"
    cfg = make_cfg(upstream_port=upstream.port)
    client, _, _ = await make_client(cfg)
    r = await client.get(WC_PATH, headers=WC_HOST)
    assert r.status == 200 and await r.text() == "200"
    assert upstream.requests[0]["path"] == WC_PATH
    assert upstream.requests[0]["host"] == "api.weathercloud.net"
    diag = json.loads((cfg.data_dir / "last_request_wc.json").read_text())
    assert diag["pairs"]["temp"] == "215" and diag["pairs"]["wid"] == "***"


async def test_wu_credentials_override(make_cfg, make_client, upstream):
    cfg = make_cfg(upstream_port=upstream.port, wu_username="NEWID", wu_password="NEWPASS")
    client, _, _ = await make_client(cfg)
    await client.get(WU_URL, headers=WU_HOST)
    q = upstream.requests[0]["query"]
    assert "ID=NEWID" in q and "PASSWORD=NEWPASS" in q and WU_PASS not in q


async def test_forward_off_serves_recording(make_cfg, make_client, upstream):
    cfg = make_cfg(upstream_port=upstream.port)
    client, app, _ = await make_client(cfg)
    upstream.body = "recorded-body\n"
    await client.get(WU_URL, headers=WU_HOST)
    assert len(upstream.requests) == 1
    app[STATE].set_forward("wu", False)
    upstream.body = "should-not-be-seen"
    r = await client.get(WU_URL, headers=WU_HOST)
    assert r.status == 200 and await r.text() == "recorded-body\n"
    assert len(upstream.requests) == 1  # upstream not contacted


async def test_forward_off_default_mock(make_cfg, make_client, upstream):
    cfg = make_cfg(upstream_port=upstream.port, forward_wu=False, forward_wc=False)
    client, _, _ = await make_client(cfg)
    r = await client.get(WU_URL, headers=WU_HOST)
    assert r.status == 200 and (await r.text()).strip() == "success"
    r = await client.get(WC_PATH, headers=WC_HOST)
    assert r.status == 200 and await r.text() == "200"
    assert upstream.requests == []


async def test_upstream_unreachable(make_cfg, make_client):
    cfg = make_cfg(upstream_port=free_port())  # nothing listens there
    client, app, _ = await make_client(cfg)
    r = await client.get(WU_URL, headers=WU_HOST)
    assert r.status == 200 and (await r.text()).strip() == "success"
    assert app[STATE].upstream_status["wu"] == "error"
    assert app[STATE].upstream_errors["wu"] == 1


async def test_upstream_timeout(make_cfg, make_client, upstream):
    upstream.delay = 2.0
    cfg = make_cfg(upstream_port=upstream.port, upstream_timeout=0.3)
    client, app, _ = await make_client(cfg)
    r = await client.get(WC_PATH, headers=WC_HOST)
    assert r.status == 200 and await r.text() == "200"
    assert app[STATE].upstream_status["wc"] == "error"


async def test_upstream_5xx_falls_back_to_mock(make_cfg, make_client, upstream):
    upstream.status, upstream.body = 503, "down"
    cfg = make_cfg(upstream_port=upstream.port)
    client, app, _ = await make_client(cfg)
    r = await client.get(WU_URL, headers=WU_HOST)
    assert r.status == 200 and (await r.text()).strip() == "success"
    assert app[STATE].upstream_status["wu"] == 503


async def test_no_secrets_anywhere(make_cfg, make_client, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(upstream_port=upstream.port)
    client, app, fake = await make_client(cfg)
    await client.get(WU_URL, headers=WU_HOST)
    await client.get(WC_PATH, headers=WC_HOST)
    # failing upstream path (error messages must be redacted too)
    app[app_forwarder_key()]._port = free_port()
    await client.get(WU_URL, headers=WU_HOST)
    await client.get(WC_PATH, headers=WC_HOST)
    # unknown path containing secrets
    await client.get(f"/foo?ID={WU_ID}&PASSWORD={WU_PASS}", headers={"Host": "example.com"})
    await drain(app)
    await app[app_bridge_key()].announce_all()

    logs = caplog.text
    files = read_all_data(cfg.data_dir)
    mqtt = "\n".join(t + p for t, p, _ in fake.messages)
    assert "***" in files
    for s in SECRETS:
        assert s not in logs, f"secret in logs: {s}"
        assert s not in files, f"secret in data files: {s}"
        assert s not in mqtt, f"secret in MQTT: {s}"
    diag = json.loads((cfg.data_dir / "last_request_wu.json").read_text())
    assert "ID=***" in diag["query"] and "PASSWORD=***" in diag["query"]
    assert diag["unmapped_fields"] == ["soilmoisture", "windspdmph_avg2m"]


def app_forwarder_key():
    from weatherbridge.app import FORWARDER
    return FORWARDER


def app_bridge_key():
    from weatherbridge.app import BRIDGE
    return BRIDGE


async def test_unknown_path_404(make_cfg, make_client, caplog):
    caplog.set_level(logging.INFO)
    client, _, _ = await make_client(make_cfg())
    r = await client.get("/index.html", headers={"Host": "example.com"})
    assert r.status == 404
    assert "404 GET host=example.com path=/index.html" in caplog.text


async def test_healthz(make_cfg, make_client, upstream):
    client, _, _ = await make_client(make_cfg(upstream_port=upstream.port))
    await client.get(WU_URL, headers=WU_HOST)
    r = await client.get("/healthz")
    data = await r.json()
    assert r.status == 200 and data["ok"] is True
    assert data["requests"]["wu"] == 1 and data["forward"] == {"wu": True, "wc": True}
    assert data["upstream_status"]["wu"] == 200 and data["last_seen"]["wu"]


async def test_mqtt_state_after_request(make_cfg, make_client, upstream):
    cfg = make_cfg(upstream_port=upstream.port)
    client, app, fake = await make_client(cfg)
    await client.get(WU_URL, headers=WU_HOST)
    await drain(app)
    state = json.loads(fake.last("meteo_proxy/state"))
    assert state["sensors"]["temperature"] == 10.0
    assert state["sensors"]["solar_radiation"] == 100.0
    assert state["upstream"]["wu"] == 200 and state["forward_wu"] is True
    assert state["diag"]["requests_wu"] == 1
    assert state["diag"]["wu_unmapped"] == ["soilmoisture", "windspdmph_avg2m"]
    topics = [t for t, _, _ in fake.messages]
    assert "homeassistant/sensor/weather_station/temperature/config" in topics
    # discovery is published before the state
    assert topics.index("homeassistant/sensor/weather_station/temperature/config") < topics.index("meteo_proxy/state")
    # second request does not re-announce
    fake.messages.clear()
    await client.get(WU_URL, headers=WU_HOST)
    await drain(app)
    assert [t for t, _, _ in fake.messages] == ["meteo_proxy/state"]
    # values accumulate: a request with one field keeps the others
    await client.get("/weatherstation/updateweatherstation.php?tempf=68", headers=WU_HOST)
    await drain(app)
    state = json.loads(fake.last("meteo_proxy/state"))
    assert state["sensors"]["temperature"] == 20.0 and state["sensors"]["humidity"] == 57


async def test_toggle_forward_via_mqtt_and_restart(make_cfg, upstream):
    cfg = make_cfg(upstream_port=upstream.port)
    cfg.data_dir.mkdir(parents=True)
    state = ProxyState(cfg.data_dir, {"wu": True, "wc": True})
    bridge = HABridge(cfg, state)
    fake = FakeMQTT()
    bridge.client = fake
    await bridge.handle_message("meteo_proxy/forward/wu/set", b"OFF")
    assert state.forward("wu") is False and state.forward("wc") is True
    assert json.loads(fake.last("meteo_proxy/state"))["forward_wu"] is False
    saved = json.loads((cfg.data_dir / "state.json").read_text())
    assert saved["forward"] == {"wu": False}
    await bridge.handle_message("meteo_proxy/forward/wc/set", "off")
    await bridge.handle_message("meteo_proxy/forward/wc/set", "garbage")  # ignored
    # "restart": new state from disk, env defaults say True but state.json wins
    state2 = ProxyState(cfg.data_dir, {"wu": True, "wc": True})
    assert state2.forward("wu") is False and state2.forward("wc") is False
    app = create_app(cfg, start_mqtt=False)
    assert app[STATE].forward("wu") is False


async def test_ha_online_reannounces(make_cfg):
    cfg = make_cfg()
    cfg.data_dir.mkdir(parents=True)
    state = ProxyState(cfg.data_dir, {"wu": True, "wc": True})
    state.sensors = {"temperature": 1.0}
    bridge = HABridge(cfg, state)
    fake = FakeMQTT()
    bridge.client = fake
    await bridge.handle_message("homeassistant/status", b"online")
    topics = [t for t, _, _ in fake.messages]
    assert "homeassistant/sensor/weather_station/temperature/config" in topics
    assert "homeassistant/switch/weather_station/forward_to_wu/config" in topics
    assert topics[-1] == "meteo_proxy/state"
    assert all(retain for _, _, retain in fake.messages)
    fake.messages.clear()
    await bridge.handle_message("homeassistant/status", b"offline")
    assert fake.messages == []


async def test_malformed_request_line_is_scrubbed(make_cfg, make_client, caplog):
    """aiohttp logs the raw request line on parse errors; credentials must be scrubbed."""
    import asyncio
    caplog.set_level(logging.DEBUG)
    client, _, _ = await make_client(make_cfg())
    reader, writer = await asyncio.open_connection(client.host, client.port)
    writer.write(f"GET /weatherstation/x.php?ID={WU_ID}&PASSWORD={WU_PASS}&d=a b c HTTP/1.1\r\n"
                 f"Host: x\r\n\r\n".encode())
    await writer.drain()
    await reader.read(1000)
    writer.close()
    for s in SECRETS:
        assert s not in caplog.text


def test_scrub_patterns():
    from weatherbridge.logsafe import scrub
    assert scrub(f"?ID={WU_ID}&PASSWORD={WU_PASS}&tempf=1") == "?ID=***&PASSWORD=***&tempf=1"
    assert scrub("/v01/set/wid/abc/key/def/temp/1") == "/v01/set/wid/***/key/***/temp/1"
    assert scrub("monkey=1&api_key=2") == "monkey=1&api_key=2"
