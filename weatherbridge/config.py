"""Configuration loaded from environment variables (see .env.example)."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


_IDENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOPIC = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")


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

    # Home Assistant backends (both off by default; at least one must be enabled)
    mqtt_enabled: bool = False
    esphome_enabled: bool = False
    esphome_port: int = 6053
    esphome_name: str = "weather-station"
    esphome_mac: str = ""  # empty = derived from DEVICE_ID
    esphome_encryption_key: str = ""  # base64 32 bytes; empty = plaintext
    esphome_mdns: bool = True

    # MQTT
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    discovery_prefix: str = "homeassistant"
    base_topic: str = "meteo_proxy"
    device_id: str = "weather_station"
    device_name: str = "Weather Station"
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
            mqtt_enabled=_bool(e.get("MQTT_ENABLED"), False),
            esphome_enabled=_bool(e.get("ESPHOME_ENABLED"), False),
            esphome_port=int(e.get("ESPHOME_PORT", "6053") or 6053),
            esphome_name=(e.get("ESPHOME_NAME") or "weather-station").strip(),
            esphome_mac=(e.get("ESPHOME_MAC") or "").strip(),
            esphome_encryption_key=(e.get("ESPHOME_ENCRYPTION_KEY") or "").strip(),
            esphome_mdns=_bool(e.get("ESPHOME_MDNS"), True),
            mqtt_host=e.get("MQTT_HOST", "").strip(),
            mqtt_port=int(e.get("MQTT_PORT", "1883") or 1883),
            mqtt_user=e.get("MQTT_USER") or None,
            mqtt_password=e.get("MQTT_PASSWORD") or None,
            discovery_prefix=e.get("DISCOVERY_PREFIX", "homeassistant") or "homeassistant",
            base_topic=e.get("BASE_TOPIC", "meteo_proxy") or "meteo_proxy",
            device_id=e.get("DEVICE_ID", "weather_station") or "weather_station",
            device_name=e.get("DEVICE_NAME", "Weather Station") or "Weather Station",
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

    def validate(self) -> list[str]:
        """Return configuration errors that must stop the service."""
        errors = []
        # Used in MQTT topics, unique ids and entity ids: no wildcards, separators or spaces.
        if not _IDENT.fullmatch(self.device_id):
            errors.append("DEVICE_ID must be 1-64 chars of A-Z, a-z, 0-9, '_' or '-'")
        for name, value in (("BASE_TOPIC", self.base_topic), ("DISCOVERY_PREFIX", self.discovery_prefix)):
            if not _TOPIC.fullmatch(value):
                errors.append(f"{name} must be MQTT topic levels of A-Z, a-z, 0-9, '_' or '-' "
                              "separated by '/' (no wildcards)")
        if not self.mqtt_enabled and not self.esphome_enabled:
            errors.append("No Home Assistant backend enabled: set MQTT_ENABLED=true and/or "
                          "ESPHOME_ENABLED=true in .env")
        if self.mqtt_enabled and not self.mqtt_host:
            errors.append("MQTT_ENABLED=true but MQTT_HOST is empty")
        if self.esphome_enabled:
            if not re.fullmatch(r"[a-z0-9-]{1,31}", self.esphome_name):
                errors.append("ESPHOME_NAME must be 1-31 chars of a-z, 0-9, '-'")
            if self.esphome_mac and not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", self.esphome_mac):
                errors.append("ESPHOME_MAC must look like 02:AB:CD:EF:01:23")
            if self.esphome_encryption_key:
                try:
                    if len(base64.b64decode(self.esphome_encryption_key, validate=True)) != 32:
                        raise ValueError
                except ValueError:
                    errors.append("ESPHOME_ENCRYPTION_KEY must be base64 of exactly 32 bytes "
                                  "(generate: openssl rand -base64 32)")
        return errors
