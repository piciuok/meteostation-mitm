#!/usr/bin/env python3
"""Resolve the aioesphomeapi versions the ESPHome backend is tested against.

Usage:
  esphome_versions.py <target>   print one aioesphomeapi version
  esphome_versions.py snapshot   print JSON with all upstream versions (used by upstream-watch)

Targets:
  pinned     version pinned in requirements.txt
  ha-stable  pinned by the ESPHome integration of the latest Home Assistant release
  ha-dev     pinned on Home Assistant's dev branch
  esphome    pinned by the latest ESPHome release (dashboard / CLI client)
  latest     latest aioesphomeapi release on PyPI
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

# PEP 440 subset seen in practice (46.28.0, 2026.12.0b3, 1.0.post1, 1.0rc1). Everything printed
# by this script ends up in CI shell steps, so anything else is rejected.
VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?")

MANIFEST = ("https://raw.githubusercontent.com/home-assistant/core/{ref}"
            "/homeassistant/components/esphome/manifest.json")


def checked(version: str) -> str:
    if not VERSION_RE.fullmatch(version or ""):
        raise SystemExit(f"refusing unexpected version string {version!r}")
    return version


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "vevor-weatherbridge-ci"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def pypi(package: str) -> dict:
    return fetch_json(f"https://pypi.org/pypi/{package}/json")["info"]


def pin_from(requirements: list[str]) -> str:
    for req in requirements or []:
        m = re.match(r"aioesphomeapi\s*==\s*([^\s;]+)", req)
        if m:
            return checked(m.group(1))
    raise SystemExit("aioesphomeapi pin not found")


def ha_pin(ref: str) -> str:
    return pin_from(fetch_json(MANIFEST.format(ref=ref))["requirements"])


def pinned() -> str:
    text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    return checked(re.search(r"^aioesphomeapi==(\S+)", text, re.M).group(1))


def snapshot() -> dict:
    ha = checked(pypi("homeassistant")["version"])
    esphome = pypi("esphome")
    return {
        "home_assistant": ha,
        "ha_stable_pin": ha_pin(ha),
        "ha_dev_pin": ha_pin("dev"),
        "esphome": checked(esphome["version"]),
        "esphome_pin": pin_from(esphome["requires_dist"]),
        "aioesphomeapi_latest": checked(pypi("aioesphomeapi")["version"]),
        "pinned": pinned(),
    }


def main(target: str) -> None:
    if target == "snapshot":
        print(json.dumps(snapshot(), sort_keys=True))
        return
    resolvers = {
        "pinned": pinned,
        "ha-stable": lambda: ha_pin(checked(pypi("homeassistant")["version"])),
        "ha-dev": lambda: ha_pin("dev"),
        "esphome": lambda: pin_from(pypi("esphome")["requires_dist"]),
        "latest": lambda: checked(pypi("aioesphomeapi")["version"]),
    }
    if target not in resolvers:
        raise SystemExit(f"unknown target {target!r}")
    print(resolvers[target]())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pinned")
