import asyncio
import socket
from pathlib import Path

import pytest
from aiohttp import web

from weatherbridge.app import BRIDGE, create_app
from weatherbridge.config import Config
from weatherbridge.upstream import StaticResolver

WU_ID = "IWARSZAWA123"
WU_PASS = "s3cr3tPaSS"
WC_WID = "abcdef0123456789"
WC_KEY = "fedcba9876543210ffff"
SECRETS = [WU_ID, WU_PASS, WC_WID, WC_KEY]

WU_QUERY = (f"ID={WU_ID}&PASSWORD={WU_PASS}&dateutc=2026-11-06+10%3A21%3A17"
            "&tempf=50&humidity=57&dewptf=41&windchillf=48.2&indoortempf=71.6&indoorhumidity=40"
            "&windspeedmph=10&windgustmph=15.5&winddir=169&rainin=0.1&dailyrainin=0.5"
            "&weeklyrainin=1&monthlyrainin=2&yearlyrainin=10&solarRadiation=100&UV=3"
            "&baromin=29.92&absbaromin=29.5&softwaretype=EasyWeatherPro&action=updateraw"
            "&realtime=1&rtfreq=5&soilmoisture=-9999&windspdmph_avg2m=3")
WC_PATH = f"/v01/set/wid/{WC_WID}/key/{WC_KEY}/temp/215/hum/57/bar/10132/wdir/169/wspd/45"


class FakeMQTT:
    def __init__(self):
        self.messages: list[tuple[str, str, bool]] = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.messages.append((topic, payload, retain))

    def last(self, topic):
        for t, p, _ in reversed(self.messages):
            if t == topic:
                return p
        return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream:
    """Local HTTP server standing in for WU / Weathercloud."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.body = "success\n"
        self.content_type = "text/html"
        self.delay = 0.0
        self.port = None
        self._runner = None

    async def _handler(self, request: web.Request):
        self.requests.append({"host": request.headers.get("Host"), "path": request.path,
                              "query": request.rel_url.raw_query_string, "method": request.method})
        if self.delay:
            await asyncio.sleep(self.delay)
        return web.Response(status=self.status, text=self.body, content_type=self.content_type)

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self.port = free_port()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self):
        await self._runner.cleanup()


@pytest.fixture
async def upstream():
    up = FakeUpstream()
    await up.start()
    yield up
    await up.stop()


@pytest.fixture
def make_cfg(tmp_path):
    def _make(**kw):
        cfg = Config(data_dir=tmp_path / "data", mqtt_host="", upstream_timeout=1.0)
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg
    return _make


@pytest.fixture
def make_client(aiohttp_client):
    async def _make(cfg):
        app = create_app(cfg, resolver=StaticResolver("127.0.0.1"), start_backends=False)
        fake = FakeMQTT()
        app[BRIDGE].client = fake
        client = await aiohttp_client(app)
        return client, app, fake
    return _make


async def drain(app):
    """Let background MQTT publish tasks finish."""
    for _ in range(5):
        await asyncio.sleep(0)
    from weatherbridge.app import TASKS
    if app[TASKS]:
        await asyncio.gather(*list(app[TASKS]))


def read_all_data(data_dir: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in data_dir.rglob("*") if p.is_file())
