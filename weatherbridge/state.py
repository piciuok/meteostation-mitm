"""Runtime state: forward switches, last values, counters. Persisted to data/state.json."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .upstream import write_json

log = logging.getLogger(__name__)

SERVICES = ("wu", "wc")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProxyState:
    def __init__(self, data_dir: Path, forward_defaults: dict[str, bool]) -> None:
        self.path = data_dir / "state.json"
        self.forward_overrides: dict[str, bool] = {}
        self.forward_defaults = dict(forward_defaults)
        self.sensors: dict[str, float | int] = {}
        self.last_reading: str | None = None
        self.requests = {s: 0 for s in SERVICES}
        self.upstream_errors = {s: 0 for s in SERVICES}
        self.upstream_status: dict[str, int | str | None] = {s: None for s in SERVICES}
        self.last_seen: dict[str, str | None] = {s: None for s in SERVICES}
        self.wu_fields: list[str] = []
        self.wu_unmapped: list[str] = []
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable %s: %s", self.path, exc)
            return
        fwd = data.get("forward") or {}
        self.forward_overrides = {s: bool(fwd[s]) for s in SERVICES if s in fwd}
        sensors = data.get("sensors") or {}
        if isinstance(sensors, dict):
            self.sensors = {k: v for k, v in sensors.items() if isinstance(v, (int, float))}
        self.last_reading = data.get("last_reading")

    def save(self) -> None:
        try:
            write_json(self.path, {
                "forward": self.forward_overrides,
                "sensors": self.sensors,
                "last_reading": self.last_reading,
            })
        except OSError as exc:
            log.error("Cannot write %s: %s", self.path, exc)

    # ------------------------------------------------------------------ forward
    def forward(self, svc: str) -> bool:
        return self.forward_overrides.get(svc, self.forward_defaults.get(svc, True))

    def set_forward(self, svc: str, enabled: bool) -> None:
        self.forward_overrides[svc] = enabled
        self.save()

    # ------------------------------------------------------------------ views
    def diagnostics(self) -> dict[str, Any]:
        return {
            "requests_wu": self.requests["wu"],
            "requests_wc": self.requests["wc"],
            "upstream_errors_wu": self.upstream_errors["wu"],
            "upstream_errors_wc": self.upstream_errors["wc"],
            "last_seen_wu": self.last_seen["wu"],
            "last_seen_wc": self.last_seen["wc"],
            "forward_wu": self.forward("wu"),
            "forward_wc": self.forward("wc"),
            "wu_fields": self.wu_fields,
            "wu_unmapped": self.wu_unmapped,
        }

    def mqtt_payload(self) -> dict[str, Any]:
        return {
            "sensors": dict(self.sensors),
            "last_reading": self.last_reading,
            "forward_wu": self.forward("wu"),
            "forward_wc": self.forward("wc"),
            "upstream": dict(self.upstream_status),
            "diag": self.diagnostics(),
        }
