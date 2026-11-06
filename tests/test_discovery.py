from weatherbridge.config import Config
from weatherbridge.mqtt import HABridge
from weatherbridge.parsing import FIELDS
from weatherbridge.state import ProxyState

EXPECTED = {
    # key: (unit, device_class, state_class)
    "temperature": ("°C", "temperature", "measurement"),
    "humidity": ("%", "humidity", "measurement"),
    "dew_point": ("°C", "temperature", "measurement"),
    "wind_chill": ("°C", "temperature", "measurement"),
    "indoor_temperature": ("°C", "temperature", "measurement"),
    "indoor_humidity": ("%", "humidity", "measurement"),
    "wind_speed": ("km/h", "wind_speed", "measurement"),
    "wind_gust": ("km/h", "wind_speed", "measurement"),
    "wind_direction": ("°", None, None),
    "rain_rate": ("mm/h", "precipitation_intensity", "measurement"),
    "daily_rain": ("mm", "precipitation", "total_increasing"),
    "weekly_rain": ("mm", "precipitation", "total_increasing"),
    "monthly_rain": ("mm", "precipitation", "total_increasing"),
    "yearly_rain": ("mm", "precipitation", "total_increasing"),
    "solar_radiation": ("W/m²", "irradiance", "measurement"),
    "uv_index": (None, None, "measurement"),
    "relative_pressure": ("hPa", "atmospheric_pressure", "measurement"),
    "absolute_pressure": ("hPa", "atmospheric_pressure", "measurement"),
}


def _bridge(tmp_path):
    cfg = Config(data_dir=tmp_path)
    return HABridge(cfg, ProxyState(tmp_path, {"wu": True, "wc": True}))


def test_all_fields_covered():
    assert {f.key for f in FIELDS} == set(EXPECTED)


def test_sensor_discovery_payloads(tmp_path):
    b = _bridge(tmp_path)
    for key, (unit, dc, sc) in EXPECTED.items():
        topic, p = b.sensor_discovery(key)
        assert topic == f"homeassistant/sensor/weather_station/{key}/config"
        assert p["unique_id"] == f"weather_station_{key}"
        assert p["default_entity_id"] == f"sensor.weather_station_{key}"
        assert p.get("unit_of_measurement") == unit
        assert p.get("device_class") == dc
        assert p.get("state_class") == sc
        assert p["has_entity_name"] is True
        assert p["state_topic"] == "meteo_proxy/state"
        assert p["value_template"] == f"{{{{ value_json.sensors.{key} }}}}"
        assert p["availability_topic"] == "meteo_proxy/status"
        assert p["payload_available"] == "online" and p["payload_not_available"] == "offline"
        assert p["device"]["identifiers"] == ["weather_station"]
        assert p["device"]["name"] == "Weather Station"
        assert p["device"]["model"] == "VEVOR YT60234"
    assert b.sensor_discovery("wind_direction")[1]["icon"] == "mdi:compass"


def test_static_discovery(tmp_path):
    b = _bridge(tmp_path)
    items = dict(b.static_discovery())
    last = items["homeassistant/sensor/weather_station/last_reading/config"]
    assert last["device_class"] == "timestamp" and last["entity_category"] == "diagnostic"
    assert last["json_attributes_topic"] == "meteo_proxy/state"
    assert last["default_entity_id"] == "sensor.weather_station_last_reading"
    for svc, oid in (("wu", "forward_to_wu"), ("wc", "forward_to_weathercloud")):
        sw = items[f"homeassistant/switch/weather_station/{oid}/config"]
        assert sw["default_entity_id"] == f"switch.weather_station_{oid}"
        assert sw["command_topic"] == f"meteo_proxy/forward/{svc}/set"
        assert sw["state_topic"] == "meteo_proxy/state"
        assert sw["entity_category"] == "config" and sw["retain"] is True
        assert sw["value_template"] == f"{{{{ 'ON' if value_json.forward_{svc} else 'OFF' }}}}"
    for svc in ("wu", "wc"):
        st = items[f"homeassistant/sensor/weather_station/upstream_{svc}_status/config"]
        assert st["entity_category"] == "diagnostic"
        assert st["default_entity_id"] == f"sensor.weather_station_upstream_{svc}_status"
    assert not any("/weather/" in t for t in items)


def test_imperial_units(tmp_path):
    b = _bridge(tmp_path)
    b.cfg.units = "imperial"
    assert b.sensor_discovery("temperature")[1]["unit_of_measurement"] == "°F"
    assert b.sensor_discovery("relative_pressure")[1]["unit_of_measurement"] == "inHg"
