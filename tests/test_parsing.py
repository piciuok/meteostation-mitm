import pytest

from weatherbridge.parsing import (mask_query, mask_wc_path, parse_wc, parse_wu, query_pairs,
                                   replace_query_values)

from .conftest import WC_KEY, WC_PATH, WC_WID, WU_ID, WU_PASS, WU_QUERY


@pytest.mark.parametrize("field,raw,key,expected", [
    ("tempf", "50", "temperature", 10.0),
    ("humidity", "57.4", "humidity", 57),
    ("dewptf", "41", "dew_point", 5.0),
    ("windchillf", "48.2", "wind_chill", 9.0),
    ("indoortempf", "71.6", "indoor_temperature", 22.0),
    ("indoorhumidity", "40", "indoor_humidity", 40),
    ("windspeedmph", "10", "wind_speed", 16.1),
    ("windgustmph", "15.5", "wind_gust", 24.9),
    ("winddir", "169.4", "wind_direction", 169),
    ("rainin", "0.1", "rain_rate", 2.5),
    ("dailyrainin", "0.5", "daily_rain", 12.7),
    ("weeklyrainin", "1", "weekly_rain", 25.4),
    ("monthlyrainin", "2", "monthly_rain", 50.8),
    ("yearlyrainin", "10", "yearly_rain", 254.0),
    ("solarradiation", "514.9", "solar_radiation", 514.9),
    ("UV", "3", "uv_index", 3.0),
    ("baromin", "29.92", "relative_pressure", 1013.2),
    ("absbaromin", "29.5", "absolute_pressure", 999.0),
])
def test_wu_conversions(field, raw, key, expected):
    res = parse_wu([(field, raw)])
    assert res.values == {key: expected}
    assert type(res.values[key]) is type(expected)


def test_wu_case_insensitive_names():
    res = parse_wu([("solarRadiation", "100"), ("TEMPF", "32"), ("uv", "1")])
    assert res.values == {"solar_radiation": 100.0, "temperature": 0.0, "uv_index": 1.0}
    assert res.unmapped == []


@pytest.mark.parametrize("raw", ["-9999", "-9999.0", "", "NaN", "nan"])
def test_wu_placeholders_skipped(raw):
    res = parse_wu([("tempf", raw)])
    assert res.values == {}
    assert res.skipped == ["tempf"]


def test_wu_firmware_garbage_rejected():
    # upstream issue #9: console sends e.g. solarRadiation=-117.-5
    res = parse_wu([("solarRadiation", "-117.-5"), ("solarradiation", "-117.5")])
    assert "solar_radiation" not in res.values
    res = parse_wu([("solarRadiation", "-117.-5")])
    assert res.invalid == {"solarRadiation": "-117.-5"}


def test_wu_negative_temperature_allowed():
    assert parse_wu([("tempf", "-4")]).values == {"temperature": -20.0}


def test_wu_unmapped_and_meta():
    res = parse_wu(query_pairs(WU_QUERY))
    assert set(res.unmapped) == {"soilmoisture", "windspdmph_avg2m"}
    assert "ID" in res.fields and "PASSWORD" in res.fields
    assert len(res.values) == 18
    assert res.dateutc == "2026-11-06 10:21:17"


def test_wu_imperial():
    res = parse_wu([("tempf", "50.04"), ("baromin", "29.921"), ("windspeedmph", "10")], "imperial")
    assert res.values == {"temperature": 50.0, "relative_pressure": 29.92, "wind_speed": 10.0}


def test_wc_path():
    res = parse_wc(WC_PATH)
    assert res.values == {"wid": "***", "key": "***", "temp": "215", "hum": "57", "bar": "10132",
                          "wdir": "169", "wspd": "45"}


def test_wc_query_variant():
    res = parse_wc("/v01/set", [("wid", WC_WID), ("key", WC_KEY), ("temp", "100")])
    assert res.values == {"wid": "***", "key": "***", "temp": "100"}


def test_mask_helpers():
    q = mask_query(WU_QUERY, {"id", "password"})
    assert WU_ID not in q and WU_PASS not in q and "ID=***" in q and "tempf=50" in q
    p = mask_wc_path(WC_PATH)
    assert WC_WID not in p and WC_KEY not in p and p.startswith("/v01/set/wid/***/key/***/temp/215")


def test_replace_query_values_preserves_encoding():
    q = replace_query_values("id=a&dateutc=2026-11-06+10%3A21%3A17&PASSWORD=b", {"ID": "x y", "PASSWORD": "z"})
    assert q == "id=x%20y&dateutc=2026-11-06+10%3A21%3A17&PASSWORD=z"
