"""Configuration loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    # HTTP
    http_host: str = "0.0.0.0"
    http_port: int = 80
    data_dir: Path = Path("/data")

    # MQTT / Home Assistant
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    discovery_prefix: str = "homeassistant"
    base_topic: str = "meteo_proxy"
    device_id: str = "stacja_meteo"
    device_name: str = "Stacja meteo"
    device_manufacturer: str = "VEVOR"
    device_model: str = "VEVOR YT60234"
    units: str = "metric"

    # Forwarding
    forward_wu: bool = True
    forward_wc: bool = True
    upstream_dns: list[str] = field(default_factory=lambda: ["1.1.1.1", "9.9.9.9"])
    upstream_timeout: float = 8.0
    upstream_port: int = 80  # not exposed via env; overridden in tests
    wu_username: str | None = None
    wu_password: str | None = None

    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        units = e.get("UNITS", "metric").strip().lower() or "metric"
        if units not in ("metric", "imperial"):
            raise ValueError(f"UNITS must be 'metric' or 'imperial', got {units!r}")
        return cls(
            http_port=int(e.get("HTTP_PORT", "80") or 80),
            data_dir=Path(e.get("DATA_DIR", "/data") or "/data"),
            mqtt_host=e.get("MQTT_HOST", "").strip(),
            mqtt_port=int(e.get("MQTT_PORT", "1883") or 1883),
            mqtt_user=e.get("MQTT_USER") or None,
            mqtt_password=e.get("MQTT_PASSWORD") or None,
            discovery_prefix=e.get("DISCOVERY_PREFIX", "homeassistant") or "homeassistant",
            base_topic=e.get("BASE_TOPIC", "meteo_proxy") or "meteo_proxy",
            device_id=e.get("DEVICE_ID", "stacja_meteo") or "stacja_meteo",
            device_name=e.get("DEVICE_NAME", "Stacja meteo") or "Stacja meteo",
            device_manufacturer=e.get("DEVICE_MANUFACTURER", "VEVOR") or "VEVOR",
            device_model=e.get("DEVICE_MODEL", "VEVOR YT60234") or "VEVOR YT60234",
            units=units,
            forward_wu=_bool(e.get("FORWARD_WU"), True),
            forward_wc=_bool(e.get("FORWARD_WC"), True),
            upstream_dns=_list(e.get("UPSTREAM_DNS"), ["1.1.1.1", "9.9.9.9"]),
            upstream_timeout=float(e.get("UPSTREAM_TIMEOUT", "8") or 8),
            wu_username=e.get("WU_USERNAME") or None,
            wu_password=e.get("WU_PASSWORD") or None,
            log_level=(e.get("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
