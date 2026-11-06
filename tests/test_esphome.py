"""ESPHome Native API backend, exercised with the real aioesphomeapi client (the one HA uses)."""

import asyncio
import base64
import json

import pytest
from aioesphomeapi import (APIClient, SensorInfo, SensorState, SwitchInfo, SwitchState,
                           TextSensorInfo, TextSensorState)
from aioesphomeapi.core import (APIConnectionError, InvalidEncryptionKeyAPIError,
                                RequiresEncryptionAPIError)

from weatherbridge.config import Config
from weatherbridge.esphome import ESPHomeServer, default_mac, entity_key
from weatherbridge.state import ProxyState

KEY = base64.b64encode(bytes(range(32))).decode()
OTHER_KEY = base64.b64encode(bytes(32)).decode()


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, esphome_enabled=True, esphome_mdns=False, device_id="weather_station")


@pytest.fixture
def state(tmp_path):
    st = ProxyState(tmp_path, {"wu": True, "wc": True})
    st.sensors = {"temperature": 10.0, "humidity": 57, "relative_pressure": 1013.2}
    st.last_reading = "2026-11-06T10:00:00+00:00"
    st.upstream_status["wu"] = 200
    return st


async def start(cfg, state, key=""):
    cfg.esphome_encryption_key = key
    server = ESPHomeServer(cfg, state)
    port = await server.start(host="127.0.0.1", port=0)
    return server, port


async def connect(port, psk=None, on_stop=None):
    client = APIClient("127.0.0.1", port, None, noise_psk=psk, expected_name="weather-station",
                       client_info="pytest")
    await asyncio.wait_for(client.connect(login=True, on_stop=on_stop), 5)
    return client


@pytest.mark.parametrize("encrypted", [False, True])
async def test_device_entities_states_and_switch(cfg, state, encrypted):
    key = KEY if encrypted else ""
    server, port = await start(cfg, state, key)
    client = await connect(port, KEY if encrypted else None)
    try:
        info = await client.device_info()
        assert info.name == "weather-station"
        assert info.friendly_name == "Weather Station"
        assert info.mac_address == default_mac("weather_station")
        assert info.model == "VEVOR YT60234" and info.manufacturer == "VEVOR"
        assert info.api_encryption_supported is encrypted

        entities, _ = await client.list_entities_services()
        by_id = {e.object_id: e for e in entities}
        temp = by_id["temperature"]
        assert isinstance(temp, SensorInfo)
        assert temp.unit_of_measurement == "°C" and temp.device_class == "temperature"
        assert temp.state_class == 1 and temp.accuracy_decimals == 1 and temp.name == "Temperature"
        assert by_id["humidity"].accuracy_decimals == 0
        assert "solar_radiation" not in by_id  # not seen yet -> not listed
        assert isinstance(by_id["last_reading"], TextSensorInfo)
        assert by_id["last_reading"].device_class == "timestamp"
        assert by_id["last_reading"].entity_category == 2
        sw = by_id["forward_to_wu"]
        assert isinstance(sw, SwitchInfo) and sw.entity_category == 1
        assert isinstance(by_id["forward_to_weathercloud"], SwitchInfo)

        states: dict[int, object] = {}
        client.subscribe_states(lambda s: states.__setitem__(s.key, s))
        for _ in range(50):
            if len(states) >= 8:
                break
            await asyncio.sleep(0.02)
        t = states[entity_key("temperature")]
        assert isinstance(t, SensorState) and t.state == pytest.approx(10.0)
        assert isinstance(states[entity_key("last_reading")], TextSensorState)
        assert states[entity_key("last_reading")].state == "2026-11-06T10:00:00+00:00"
        assert states[entity_key("upstream_wu_status")].state == "200"
        assert states[entity_key("upstream_wc_status")].missing_state is True
        assert isinstance(states[entity_key("forward_to_wu")], SwitchState)
        assert states[entity_key("forward_to_wu")].state is True

        # Switch command from HA turns forwarding off and is persisted
        client.switch_command(entity_key("forward_to_wu"), False)
        for _ in range(50):
            if state.forward("wu") is False and states[entity_key("forward_to_wu")].state is False:
                break
            await asyncio.sleep(0.02)
        assert state.forward("wu") is False
        assert json.loads((cfg.data_dir / "state.json").read_text())["forward"] == {"wu": False}
        assert states[entity_key("forward_to_wu")].state is False

        # New value pushed to subscribers
        state.sensors["temperature"] = 12.5
        await server.publish_update()
        for _ in range(50):
            if states[entity_key("temperature")].state == pytest.approx(12.5):
                break
            await asyncio.sleep(0.02)
        assert states[entity_key("temperature")].state == pytest.approx(12.5)
    finally:
        await client.disconnect()
        await server.stop()


async def test_new_sensor_forces_relist(cfg, state):
    server, port = await start(cfg, state)
    stopped = asyncio.Event()

    async def on_stop(expected):
        stopped.set()

    client = await connect(port, on_stop=on_stop)
    try:
        await client.list_entities_services()
        client.subscribe_states(lambda s: None)
        await asyncio.sleep(0.1)
        state.sensors["solar_radiation"] = 100.0
        await server.publish_update()
        await asyncio.wait_for(stopped.wait(), 5)
        client2 = await connect(port)
        entities, _ = await client2.list_entities_services()
        assert "solar_radiation" in {e.object_id for e in entities}
        await client2.disconnect()
    finally:
        await server.stop()


async def test_wrong_key_rejected(cfg, state):
    server, port = await start(cfg, state, KEY)
    try:
        with pytest.raises(InvalidEncryptionKeyAPIError):
            await connect(port, OTHER_KEY)
    finally:
        await server.stop()


async def test_plaintext_client_to_encrypted_server(cfg, state):
    server, port = await start(cfg, state, KEY)
    try:
        with pytest.raises(RequiresEncryptionAPIError):
            await connect(port, None)
    finally:
        await server.stop()


async def test_encrypted_client_to_plaintext_server(cfg, state):
    server, port = await start(cfg, state)
    try:
        with pytest.raises(APIConnectionError):
            await connect(port, KEY)
    finally:
        await server.stop()


def test_config_validation():
    assert any("No Home Assistant backend" in e for e in Config().validate())
    assert any("MQTT_HOST" in e for e in Config(mqtt_enabled=True).validate())
    assert Config(mqtt_enabled=True, mqtt_host="h").validate() == []
    assert Config(esphome_enabled=True).validate() == []
    assert Config(esphome_enabled=True, esphome_encryption_key=KEY).validate() == []
    assert Config(esphome_enabled=True, esphome_encryption_key="abc").validate()
    assert Config(esphome_enabled=True, esphome_name="Bad Name").validate()
    assert Config(esphome_enabled=True, esphome_mac="zz").validate()
    env = Config.from_env({"ESPHOME_ENABLED": "true", "MQTT_ENABLED": "false"})
    assert env.esphome_enabled and not env.mqtt_enabled and env.esphome_port == 6053


def test_default_mac_is_stable_and_local():
    mac = default_mac("weather_station")
    assert mac == default_mac("weather_station") != default_mac("other")
    assert int(mac.split(":")[0], 16) & 0x02  # locally administered


async def test_app_integration_wu_to_esphome_and_cross_backend_toggle(tmp_path, aiohttp_client):
    """WU upload reaches the ESPHome client; an ESPHome switch toggle is also published to MQTT."""
    from yarl import URL

    from weatherbridge.app import BRIDGE, ESPHOME, TASKS, create_app
    from weatherbridge.upstream import StaticResolver

    from .conftest import FakeMQTT

    cfg = Config(data_dir=tmp_path / "data", esphome_enabled=True, esphome_port=0, esphome_mdns=False,
                 forward_wu=False, forward_wc=False)
    app = create_app(cfg, resolver=StaticResolver(), start_backends=True)
    fake = FakeMQTT()
    app[BRIDGE].client = fake
    http = await aiohttp_client(app)
    host = {"Host": "rtupdate.wunderground.com"}
    url = "/weatherstation/updateweatherstation.php?ID=X&PASSWORD=Y&tempf=50&humidity=60"
    assert (await http.get(URL(url, encoded=True), headers=host)).status == 200

    client = await connect(app[ESPHOME].port)
    try:
        entities, _ = await client.list_entities_services()
        assert {"temperature", "humidity"} <= {e.object_id for e in entities}
        states = {}
        client.subscribe_states(lambda s: states.__setitem__(s.key, s))
        await http.get(URL(url.replace("tempf=50", "tempf=68"), encoded=True), headers=host)
        for _ in range(100):
            st = states.get(entity_key("temperature"))
            if st is not None and st.state == pytest.approx(20.0):
                break
            await asyncio.sleep(0.02)
        assert states[entity_key("temperature")].state == pytest.approx(20.0)

        fake.messages.clear()
        client.switch_command(entity_key("forward_to_weathercloud"), True)
        for _ in range(100):
            if fake.last("meteo_proxy/state"):
                break
            await asyncio.sleep(0.02)
        if app[TASKS]:
            await asyncio.gather(*list(app[TASKS]))
        assert json.loads(fake.last("meteo_proxy/state"))["forward_wc"] is True

        health = await (await http.get("/healthz")).json()
        assert health["esphome_enabled"] is True and health["esphome_clients"] == 1
        assert health["forward"]["wc"] is True
    finally:
        await client.disconnect()
