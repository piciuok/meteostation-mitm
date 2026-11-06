"""Forwarding to the real services and recorded (mock) responses."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import dns.asyncresolver
import dns.exception
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

log = logging.getLogger(__name__)

DEFAULT_RESPONSES = {
    # Weather Underground answers "success" to a valid upload.
    "wu": {"status": 200, "content_type": "text/html", "body": "success\n"},
    # Weathercloud answers the status code as plain text.
    "wc": {"status": 200, "content_type": "text/plain", "body": "200"},
}

# Upstream answers are a few bytes ("success", "200"); anything larger is not passed on.
MAX_UPSTREAM_BODY = 64 * 1024

# Headers copied from the console request to the upstream request.
_FORWARD_HEADERS = ("User-Agent", "Accept", "Content-Type")


class UpstreamResolver(AbstractResolver):
    """Resolve upstream hosts via explicit DNS servers.

    The LAN resolver (router) points the service hosts at this container, so the
    system resolver must not be used for upstream lookups (it would loop).
    """

    def __init__(self, nameservers: list[str], timeout: float = 4.0) -> None:
        self._resolver = dns.asyncresolver.Resolver(configure=False)
        self._resolver.nameservers = list(nameservers)
        self._resolver.lifetime = timeout

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        try:
            ipaddress.ip_address(host)
            addrs = [host]
        except ValueError:
            answer = await self._resolver.resolve(host, "A")
            addrs = [r.to_text() for r in answer]
        return [ResolveResult(hostname=host, host=a, port=port, family=socket.AF_INET,
                              proto=0, flags=socket.AI_NUMERICHOST) for a in addrs]

    async def close(self) -> None:
        return None


class StaticResolver(AbstractResolver):
    """Resolve every host to one address (used by tests)."""

    def __init__(self, address: str = "127.0.0.1") -> None:
        self.address = address

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        return [ResolveResult(hostname=host, host=self.address, port=port, family=socket.AF_INET,
                              proto=0, flags=socket.AI_NUMERICHOST)]

    async def close(self) -> None:
        return None


@dataclass
class UpstreamResponse:
    status: int
    content_type: str
    body: bytes


class UpstreamError(Exception):
    pass


class Forwarder:
    def __init__(self, resolver: AbstractResolver, timeout: float, port: int = 80) -> None:
        self._resolver = resolver
        self._timeout = timeout
        self._port = port
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(resolver=self._resolver, ttl_dns_cache=300,
                                             force_close=True)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                auto_decompress=False,
                skip_auto_headers=("User-Agent",),
            )
        return self._session

    async def forward(self, method: str, host: str, path: str, raw_query: str,
                      headers: dict[str, str], body: bytes) -> UpstreamResponse:
        """Send the console request to the real host. Raises UpstreamError on failure."""
        target = f"http://{host}:{self._port}{path}"
        if raw_query:
            target += f"?{raw_query}"
        out_headers = {h: headers[h] for h in _FORWARD_HEADERS if h in headers}
        out_headers["Host"] = host
        session = await self._get_session()
        try:
            async with session.request(method, URL(target, encoded=True), headers=out_headers,
                                       data=body or None, allow_redirects=False) as resp:
                buf = bytearray()
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    if len(buf) > MAX_UPSTREAM_BODY:
                        raise UpstreamError(f"response larger than {MAX_UPSTREAM_BODY} bytes")
                data = bytes(buf)
                ctype = resp.headers.get("Content-Type", "text/plain")
                return UpstreamResponse(resp.status, ctype, data)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, dns.exception.DNSException) as exc:
            # Exception text may include the URL; callers must redact before logging.
            raise UpstreamError(f"{type(exc).__name__}: {exc}") from exc

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class ResponseStore:
    """Recorded upstream responses in data/responses/<svc>.json."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "responses"

    def get(self, svc: str) -> dict[str, Any]:
        path = self.dir / f"{svc}.json"
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and "status" in rec and "body" in rec:
                return rec
        except (OSError, ValueError):
            pass
        return dict(DEFAULT_RESPONSES[svc])

    def record(self, svc: str, resp: UpstreamResponse) -> None:
        rec = {
            "status": resp.status,
            "content_type": resp.content_type,
            "body": resp.body.decode("utf-8", errors="replace"),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_json(self.dir / f"{svc}.json", rec)


def write_json(path: Path, data: Any) -> None:
    """Atomic JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
