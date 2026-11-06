"""Home Assistant integration over MQTT discovery (single retained state topic)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Protocol

from . import __version__
from .config import Config
from .parsing import FIELDS_BY_KEY
from .state import ProxyState

log = logging.getLogger(__name__)

SWITCHES = {
    # svc: (object id suffix, entity name)
    "wu": ("forward_to_wu", "Forward to WU"),
    "wc": ("forward_to_weathercloud", "Forward to Weathercloud"),
}
STATUS_SENSORS = {
    "wu": ("upstream_wu_status", "Upstream WU status"),
    "wc": ("upstream_wc_status", "Upstream WC status"),
}


class Publisher(Protocol):
    async def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> Any: ...


class HABridge:
    def __init__(self, cfg: Config, state: ProxyState,
                 on_change: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.on_change = on_change
        self.client: Publisher | None = None
        self.announced: set[str] = set()
        self._lock = asyncio.Lock()
        base = cfg.base_topic
        self.state_topic = f"{base}/state"
        self.status_topic = f"{base}/status"
        self.command_topics = {svc: f"{base}/forward/{svc}/set" for svc in SWITCHES}
        self.ha_status_topic = f"{cfg.discovery_prefix}/status"

    @property
    def connected(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------------ discovery payloads
    def _device(self) -> dict[str, Any]:
        return {
            "identifiers": [self.cfg.device_id],
            "name": self.cfg.device_name,
            "manufacturer": self.cfg.device_manufacturer,
            "model": self.cfg.device_model,
            "sw_version": __version__,
        }

    def _common(self, component: str, object_id: str, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "has_entity_name": True,
            "unique_id": f"{self.cfg.device_id}_{object_id}",
            "default_entity_id": f"{component}.{self.cfg.device_id}_{object_id}",
            "state_topic": self.state_topic,
            "availability_topic": self.status_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self._device(),
            "origin": {"name": "vevor-weatherbridge", "sw_version": __version__},
        }

    def _topic(self, component: str, object_id: str) -> str:
        return f"{self.cfg.discovery_prefix}/{component}/{self.cfg.device_id}/{object_id}/config"

    def sensor_discovery(self, key: str) -> tuple[str, dict[str, Any]]:
        f = FIELDS_BY_KEY[key]
        p = self._common("sensor", key, f.name)
        p["value_template"] = f"{{{{ value_json.sensors.{key} }}}}"
        unit = f.metric_unit if self.cfg.units == "metric" else f.imperial_unit
        if unit:
            p["unit_of_measurement"] = unit
        if f.device_class:
            p["device_class"] = f.device_class
        if f.state_class:
            p["state_class"] = f.state_class
        if f.icon:
            p["icon"] = f.icon
        return self._topic("sensor", key), p

    def static_discovery(self) -> list[tuple[str, dict[str, Any]]]:
        out = []
        p = self._common("sensor", "last_reading", "Last reading")
        p.update({
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            "value_template": "{{ value_json.last_reading }}",
            "json_attributes_topic": self.state_topic,
            "json_attributes_template": "{{ value_json.diag | tojson }}",
        })
        out.append((self._topic("sensor", "last_reading"), p))
        for svc, (oid, name) in STATUS_SENSORS.items():
            p = self._common("sensor", oid, name)
            p.update({
                "entity_category": "diagnostic",
                "icon": "mdi:cloud-check-outline",
                "value_template": f"{{{{ value_json.upstream.{svc} }}}}",
            })
            out.append((self._topic("sensor", oid), p))
        for svc, (oid, name) in SWITCHES.items():
            p = self._common("switch", oid, name)
            p.update({
                "entity_category": "config",
                "icon": "mdi:cloud-upload-outline",
                "command_topic": self.command_topics[svc],
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "value_template": f"{{{{ 'ON' if value_json.forward_{svc} else 'OFF' }}}}",
                "retain": True,
            })
            out.append((self._topic("switch", oid), p))
        return out

    # ------------------------------------------------------------------ publishing
    async def _publish(self, topic: str, payload: str, retain: bool = True) -> bool:
        client = self.client
        if client is None:
            return False
        try:
            await client.publish(topic, payload, qos=1, retain=retain)
            return True
        except Exception as exc:  # broker gone mid-publish; reconnect loop handles it
            log.warning("MQTT publish failed: %s", exc)
            return False

    async def announce_all(self) -> None:
        """(Re)publish all discovery configs and the current state."""
        async with self._lock:
            self.announced.clear()
            for topic, payload in self.static_discovery():
                await self._publish(topic, json.dumps(payload, ensure_ascii=False))
            for key in self.state.sensors:
                if key in FIELDS_BY_KEY:
                    topic, payload = self.sensor_discovery(key)
                    if await self._publish(topic, json.dumps(payload, ensure_ascii=False)):
                        self.announced.add(key)
        await self.publish_state()

    async def publish_update(self) -> None:
        """Announce newly seen sensors, then publish state."""
        async with self._lock:
            for key in self.state.sensors:
                if key not in self.announced and key in FIELDS_BY_KEY:
                    topic, payload = self.sensor_discovery(key)
                    if await self._publish(topic, json.dumps(payload, ensure_ascii=False)):
                        self.announced.add(key)
        await self.publish_state()

    async def publish_state(self) -> None:
        await self._publish(self.state_topic, json.dumps(self.state.mqtt_payload(), ensure_ascii=False))

    # ------------------------------------------------------------------ incoming
    async def handle_message(self, topic: str, payload: bytes | str) -> None:
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        text = text.strip()
        if topic == self.ha_status_topic:
            if text.lower() == "online":
                log.info("Home Assistant online, re-announcing discovery")
                await self.announce_all()
            return
        for svc, cmd_topic in self.command_topics.items():
            if topic == cmd_topic:
                value = text.upper()
                if value in ("ON", "TRUE", "1"):
                    enabled = True
                elif value in ("OFF", "FALSE", "0"):
                    enabled = False
                else:
                    log.warning("Ignoring invalid forward command for %s: %r", svc, text[:20])
                    return
                self.state.set_forward(svc, enabled)
                log.info("Forward %s set to %s from MQTT", svc.upper(), "ON" if enabled else "OFF")
                if self.on_change:
                    self.on_change()  # publishes to all backends, this one included
                else:
                    await self.publish_state()
                return

    # ------------------------------------------------------------------ connection loop
    async def run(self) -> None:
        import aiomqtt

        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.cfg.mqtt_host,
                    port=self.cfg.mqtt_port,
                    username=self.cfg.mqtt_user,
                    password=self.cfg.mqtt_password,
                    identifier=f"{self.cfg.base_topic}-{self.cfg.device_id}",
                    keepalive=60,
                    will=aiomqtt.Will(self.status_topic, "offline", qos=1, retain=True),
                ) as client:
                    log.info("MQTT connected to %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
                    backoff = 1.0
                    await client.subscribe(self.ha_status_topic, qos=1)
                    for t in self.command_topics.values():
                        await client.subscribe(t, qos=1)
                    self.client = client
                    await client.publish(self.status_topic, "online", qos=1, retain=True)
                    await self.announce_all()
                    async for msg in client.messages:
                        await self.handle_message(str(msg.topic), msg.payload)
            except asyncio.CancelledError:
                self.client = None
                raise
            except Exception as exc:
                self.client = None
                log.warning("MQTT disconnected (%s), retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
