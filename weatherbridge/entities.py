"""Non-weather entities shared by both Home Assistant backends (MQTT and ESPHome).

Weather sensors are defined by the WU field table in parsing.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    object_id: str
    name: str
    icon: str = ""


# Diagnostic: time of the last WU reading (device_class timestamp).
LAST_READING = Entity("last_reading", "Last reading")

# Diagnostic: last upstream HTTP status (or "error") per service.
UPSTREAM_STATUS = {
    "wu": Entity("upstream_wu_status", "Upstream WU status", "mdi:cloud-check-outline"),
    "wc": Entity("upstream_wc_status", "Upstream WC status", "mdi:cloud-check-outline"),
}

# Config: forward uploads to the real service, per service.
FORWARD_SWITCHES = {
    "wu": Entity("forward_to_wu", "Forward to WU", "mdi:cloud-upload-outline"),
    "wc": Entity("forward_to_weathercloud", "Forward to Weathercloud", "mdi:cloud-upload-outline"),
}
