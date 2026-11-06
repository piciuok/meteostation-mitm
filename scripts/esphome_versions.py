#!/usr/bin/env python3
"""Print the aioesphomeapi version for a compatibility target.

  pinned     version pinned in requirements.txt
  ha-stable  version pinned by the ESPHome integration of the latest Home Assistant release
  ha-dev     version pinned on Home Assistant's dev branch
  latest     latest aioesphomeapi release on PyPI
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

MANIFEST = "https://raw.githubusercontent.com/home-assistant/core/{ref}/homeassistant/components/esphome/manifest.json"


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "vevor-weatherbridge-ci"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def ha_pin(ref: str) -> str:
    for req in fetch_json(MANIFEST.format(ref=ref))["requirements"]:
        if req.startswith("aioesphomeapi=="):
            return req.split("==", 1)[1]
    raise SystemExit(f"aioesphomeapi not found in HA {ref} esphome manifest")


def main(target: str) -> None:
    if target == "pinned":
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        print(re.search(r"^aioesphomeapi==(\S+)", text, re.M).group(1))
    elif target == "ha-stable":
        ha = fetch_json("https://pypi.org/pypi/homeassistant/json")["info"]["version"]
        print(ha_pin(ha), "# Home Assistant", ha, file=sys.stderr)
        print(ha_pin(ha))
    elif target == "ha-dev":
        print(ha_pin("dev"))
    elif target == "latest":
        print(fetch_json("https://pypi.org/pypi/aioesphomeapi/json")["info"]["version"])
    else:
        raise SystemExit(f"unknown target {target!r}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pinned")
