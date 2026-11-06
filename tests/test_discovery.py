from weatherbridge.config import Config
from weatherbridge.mqtt import HABridge
from weatherbridge.parsing import FIELDS
from weatherbridge.state import ProxyState

EXPECTED = {
    # key: (unit, device_class, state_class)
    "temperatura": ("°C", "temperature", "measurement"),
    "wilgotnosc": ("%", "humidity", "measurement"),
    "punkt_rosy": ("°C", "temperature", "measurement"),
    "temperatura_odczuwalna": ("°C", "temperature", "measurement"),
    "temperatura_konsola": ("°C", "temperature", "measurement"),
    "wilgotnosc_konsola": ("%", "humidity", "measurement"),
    "wiatr_predkosc": ("km/h", "wind_speed", "measurement"),
    "wiatr_porywy": ("km/h", "wind_speed", "measurement"),
    "wiatr_kierunek": ("°", None, None),
    "opad_intensywnosc": ("mm/h", "precipitation_intensity", "measurement"),
    "opad_dzienny": ("mm", "precipitation", "total_increasing"),
    "opad_tygodniowy": ("mm", "precipitation", "total_increasing"),
    "opad_miesieczny": ("mm", "precipitation", "total_increasing"),
    "opad_roczny": ("mm", "precipitation", "total_increasing"),
    "promieniowanie": ("W/m²", "irradiance", "measurement"),
    "uv": (None, None, "measurement"),
    "cisnienie_wzgledne": ("hPa", "atmospheric_pressure", "measurement"),
    "cisnienie_absolutne": ("hPa", "atmospheric_pressure", "measurement"),
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
        assert topic == f"homeassistant/sensor/stacja_meteo/{key}/config"
        assert p["unique_id"] == f"stacja_meteo_{key}"
        assert p["default_entity_id"] == f"sensor.stacja_meteo_{key}"
        assert p.get("unit_of_measurement") == unit
        assert p.get("device_class") == dc
        assert p.get("state_class") == sc
        assert p["has_entity_name"] is True
        assert p["state_topic"] == "meteo_proxy/state"
        assert p["value_template"] == f"{{{{ value_json.sensors.{key} }}}}"
        assert p["availability_topic"] == "meteo_proxy/status"
        assert p["payload_available"] == "online" and p["payload_not_available"] == "offline"
        assert p["device"]["identifiers"] == ["stacja_meteo"]
        assert p["device"]["name"] == "Stacja meteo"
        assert p["device"]["model"] == "VEVOR YT60234"
    assert b.sensor_discovery("wiatr_kierunek")[1]["icon"] == "mdi:compass"


def test_static_discovery(tmp_path):
    b = _bridge(tmp_path)
    items = dict(b.static_discovery())
    last = items["homeassistant/sensor/stacja_meteo/ostatni_odczyt/config"]
    assert last["device_class"] == "timestamp" and last["entity_category"] == "diagnostic"
    assert last["json_attributes_topic"] == "meteo_proxy/state"
    assert last["default_entity_id"] == "sensor.stacja_meteo_ostatni_odczyt"
    for svc, oid in (("wu", "przekazuj_do_wu"), ("wc", "przekazuj_do_weathercloud")):
        sw = items[f"homeassistant/switch/stacja_meteo/{oid}/config"]
        assert sw["default_entity_id"] == f"switch.stacja_meteo_{oid}"
        assert sw["command_topic"] == f"meteo_proxy/forward/{svc}/set"
        assert sw["state_topic"] == "meteo_proxy/state"
        assert sw["entity_category"] == "config" and sw["retain"] is True
        assert sw["value_template"] == f"{{{{ 'ON' if value_json.forward_{svc} else 'OFF' }}}}"
    for svc in ("wu", "wc"):
        st = items[f"homeassistant/sensor/stacja_meteo/upstream_{svc}_status/config"]
        assert st["entity_category"] == "diagnostic"
        assert st["default_entity_id"] == f"sensor.stacja_meteo_upstream_{svc}_status"
    assert not any("/weather/" in t for t in items)


def test_imperial_units(tmp_path):
    b = _bridge(tmp_path)
    b.cfg.units = "imperial"
    assert b.sensor_discovery("temperatura")[1]["unit_of_measurement"] == "°F"
    assert b.sensor_discovery("cisnienie_wzgledne")[1]["unit_of_measurement"] == "inHg"
