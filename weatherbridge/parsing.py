"""Parsing of Weather Underground / Weathercloud requests and secret masking."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote_plus

MASK = "***"

# Query/path keys that carry station credentials (compared case-insensitively).
WU_SECRET_KEYS = {"id", "password"}
WC_SECRET_KEYS = {"wid", "key"}

# WU fields that are metadata, not measurements (never reported as "unmapped").
WU_META_KEYS = {"id", "password", "action", "dateutc", "softwaretype", "realtime", "rtfreq"}


def f_to_c(v: float) -> float:
    return (v - 32.0) * 5.0 / 9.0


def mph_to_kmh(v: float) -> float:
    return v * 1.609344


def in_to_mm(v: float) -> float:
    return v * 25.4


def inhg_to_hpa(v: float) -> float:
    return v * 33.8639


def _identity(v: float) -> float:
    return v


@dataclass(frozen=True)
class Field:
    wu: str  # lower-case WU parameter name
    key: str  # state key / entity object id suffix
    name: str  # entity name in HA (slugifies to `key`)
    metric_unit: str | None
    imperial_unit: str | None
    convert: Callable[[float], float]
    device_class: str | None = None
    state_class: str | None = "measurement"
    icon: str | None = None
    integer: bool = False
    imperial_digits: int = 1


FIELDS: tuple[Field, ...] = (
    Field("tempf", "temperature", "Temperature", "°C", "°F", f_to_c, "temperature"),
    Field("humidity", "humidity", "Humidity", "%", "%", _identity, "humidity", integer=True),
    Field("dewptf", "dew_point", "Dew point", "°C", "°F", f_to_c, "temperature"),
    Field("windchillf", "wind_chill", "Wind chill", "°C", "°F", f_to_c, "temperature"),
    Field("indoortempf", "indoor_temperature", "Indoor temperature", "°C", "°F", f_to_c, "temperature"),
    Field("indoorhumidity", "indoor_humidity", "Indoor humidity", "%", "%", _identity, "humidity", integer=True),
    Field("windspeedmph", "wind_speed", "Wind speed", "km/h", "mph", mph_to_kmh, "wind_speed"),
    Field("windgustmph", "wind_gust", "Wind gust", "km/h", "mph", mph_to_kmh, "wind_speed"),
    Field("winddir", "wind_direction", "Wind direction", "°", "°", _identity, None, None, "mdi:compass", integer=True),
    Field("rainin", "rain_rate", "Rain rate", "mm/h", "in/h", in_to_mm,
          "precipitation_intensity", imperial_digits=2),
    Field("dailyrainin", "daily_rain", "Daily rain", "mm", "in", in_to_mm, "precipitation",
          "total_increasing", imperial_digits=2),
    Field("weeklyrainin", "weekly_rain", "Weekly rain", "mm", "in", in_to_mm, "precipitation",
          "total_increasing", imperial_digits=2),
    Field("monthlyrainin", "monthly_rain", "Monthly rain", "mm", "in", in_to_mm, "precipitation",
          "total_increasing", imperial_digits=2),
    Field("yearlyrainin", "yearly_rain", "Yearly rain", "mm", "in", in_to_mm, "precipitation",
          "total_increasing", imperial_digits=2),
    Field("solarradiation", "solar_radiation", "Solar radiation", "W/m²", "W/m²", _identity, "irradiance"),
    Field("uv", "uv_index", "UV index", None, None, _identity, None, "measurement", "mdi:weather-sunny-alert"),
    Field("baromin", "relative_pressure", "Relative pressure", "hPa", "inHg", inhg_to_hpa,
          "atmospheric_pressure", imperial_digits=2),
    Field("absbaromin", "absolute_pressure", "Absolute pressure", "hPa", "inHg", inhg_to_hpa,
          "atmospheric_pressure", imperial_digits=2),
)

FIELDS_BY_WU = {f.wu: f for f in FIELDS}
FIELDS_BY_KEY = {f.key: f for f in FIELDS}

# Fields that are physically non-negative. A negative value there is a known console
# firmware glitch (upstream issue #9: solarRadiation=-117.-5) and is rejected.
NON_NEGATIVE = {"humidity", "indoorhumidity", "windspeedmph", "windgustmph", "winddir", "rainin",
                "dailyrainin", "weeklyrainin", "monthlyrainin", "yearlyrainin", "solarradiation",
                "uv", "baromin", "absbaromin"}


def _number(raw: str) -> float | None:
    """Parse a numeric value; None for empty, -9999, NaN/inf or garbage."""
    raw = raw.strip()
    if raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v) or v <= -9999:
        return None
    return v


@dataclass
class WUResult:
    values: dict[str, float | int] = field(default_factory=dict)  # key -> converted value
    fields: list[str] = field(default_factory=list)  # all received parameter names (as sent)
    unmapped: list[str] = field(default_factory=list)  # unknown parameter names (no values)
    invalid: dict[str, str] = field(default_factory=dict)  # name -> raw value rejected as garbage
    skipped: list[str] = field(default_factory=list)  # empty / -9999 / NaN placeholders
    dateutc: str | None = None


def parse_wu(pairs: list[tuple[str, str]], units: str = "metric") -> WUResult:
    """Parse WU query parameters (case-insensitive names) into converted values."""
    res = WUResult()
    seen: dict[str, tuple[str, str]] = {}
    for name, value in pairs:
        res.fields.append(name)
        seen[name.lower()] = (name, value)

    for lname, (name, raw) in seen.items():
        if lname == "dateutc":
            res.dateutc = raw
            continue
        if lname in WU_META_KEYS:
            continue
        fdef = FIELDS_BY_WU.get(lname)
        if fdef is None:
            res.unmapped.append(name)
            continue
        v = _number(raw)
        if v is None:
            stripped = raw.strip().lower()
            if stripped in ("", "nan") or stripped.startswith("-9999"):
                res.skipped.append(name)
            else:
                res.invalid[name] = raw
            continue
        if lname in NON_NEGATIVE and v < 0:
            res.invalid[name] = raw
            continue
        if units == "metric":
            out = fdef.convert(v)
            digits = 1
        else:
            out = v
            digits = fdef.imperial_digits
        res.values[fdef.key] = int(round(out)) if fdef.integer else round(out, digits)
    return res


@dataclass
class WCResult:
    values: dict[str, str] = field(default_factory=dict)  # masked key/value pairs


def parse_wc(path: str, query_pairs: list[tuple[str, str]] | None = None) -> WCResult:
    """Parse a Weathercloud request: /v01/set/wid/<id>/key/<key>/<name>/<value>/..."""
    res = WCResult()
    segments = [unquote_plus(s) for s in path.strip("/").split("/") if s != ""]
    # Drop the leading "v01/set" (or any version prefix ending at "set").
    if "set" in segments:
        segments = segments[segments.index("set") + 1:]
    for i in range(0, len(segments) - 1, 2):
        k, v = segments[i], segments[i + 1]
        res.values[k] = MASK if k.lower() in WC_SECRET_KEYS else v
    if len(segments) % 2 == 1:
        res.values[segments[-1]] = ""
    for k, v in query_pairs or []:
        res.values[k] = MASK if k.lower() in WC_SECRET_KEYS else v
    return res


# --------------------------------------------------------------------------- masking

def secrets_from_query(pairs: list[tuple[str, str]], keys: set[str]) -> list[str]:
    return [v for k, v in pairs if k.lower() in keys and v]


def secrets_from_wc_path(path: str) -> list[str]:
    segments = [s for s in path.strip("/").split("/") if s != ""]
    out = []
    for i, s in enumerate(segments[:-1]):
        if s.lower() in WC_SECRET_KEYS:
            out.append(segments[i + 1])
            out.append(unquote_plus(segments[i + 1]))
    return [s for s in out if s]


def mask_query(raw_query: str, keys: set[str]) -> str:
    """Replace values of secret keys in a raw query string, preserving everything else."""
    if not raw_query:
        return raw_query
    parts = []
    for part in raw_query.split("&"):
        name, sep, _ = part.partition("=")
        if sep and unquote_plus(name).lower() in keys:
            parts.append(f"{name}={MASK}")
        else:
            parts.append(part)
    return "&".join(parts)


def replace_query_values(raw_query: str, replacements: dict[str, str]) -> str:
    """Replace values of given keys (case-insensitive) in a raw query string."""
    lower = {k.lower(): v for k, v in replacements.items()}
    parts = []
    for part in raw_query.split("&"):
        name, sep, _ = part.partition("=")
        key = unquote_plus(name).lower()
        if sep and key in lower:
            parts.append(f"{name}={quote(lower[key], safe='')}")
        else:
            parts.append(part)
    return "&".join(parts)


def mask_wc_path(path: str) -> str:
    segments = path.split("/")
    for i in range(len(segments) - 1):
        if segments[i].lower() in WC_SECRET_KEYS and segments[i + 1]:
            segments[i + 1] = MASK
    return "/".join(segments)


def redact(text: str, secrets: list[str]) -> str:
    """Remove any occurrence of secret values from arbitrary text (e.g. exception messages)."""
    for s in sorted(set(secrets), key=len, reverse=True):
        if s and len(s) >= 2:
            text = text.replace(s, MASK)
    return text


def query_pairs(raw_query: str) -> list[tuple[str, str]]:
    return parse_qsl(raw_query, keep_blank_values=True)


_SLUG = re.compile(r"[^a-z0-9_]+")


def slug(value: str) -> str:
    return _SLUG.sub("_", value.lower()).strip("_")
