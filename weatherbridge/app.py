"""aiohttp application: intercepts console uploads, forwards or mocks, feeds Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiohttp import web
from aiohttp.abc import AbstractResolver

from . import logsafe
from .config import Config
from .esphome import ESPHomeServer
from .mqtt import HABridge
from .parsing import (WC_SECRET_KEYS, WU_SECRET_KEYS, mask_query, mask_wc_path, parse_wc,
                      parse_wu, query_pairs, redact, replace_query_values, secrets_from_query,
                      secrets_from_wc_path)
from .state import ProxyState, now_iso
from .upstream import (Forwarder, ResponseStore, UpstreamError, UpstreamResolver, UpstreamResponse,
                       write_json)

log = logging.getLogger("weatherbridge")

DEFAULT_HOSTS = {"wu": "rtupdate.wunderground.com", "wc": "api.weathercloud.net"}
HOST_SUFFIXES = {"wu": ("wunderground.com",), "wc": ("weathercloud.net",)}
SVC_LABEL = {"wu": "WU", "wc": "WC"}

CFG = web.AppKey("cfg", Config)
STATE = web.AppKey("state", ProxyState)
BRIDGE = web.AppKey("bridge", HABridge)
ESPHOME = web.AppKey("esphome", object)  # ESPHomeServer | None
FORWARDER = web.AppKey("forwarder", Forwarder)
STORE = web.AppKey("store", ResponseStore)
TASKS = web.AppKey("tasks", set)


_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")


def clean_host(host: str) -> str:
    """Normalise a Host header to a bare DNS name; anything else (paths, userinfo, IPs
    with odd characters, ...) becomes an empty string."""
    host = host.strip().lower()
    name, sep, port = host.rpartition(":")
    if sep and port.isdigit():
        host = name
    host = host.rstrip(".")
    return host if _HOSTNAME.fullmatch(host) else ""


def is_service_host(svc: str, host: str) -> bool:
    """True if `host` is one of the service's domains or a subdomain of them."""
    return any(host == d or host.endswith("." + d) for d in HOST_SUFFIXES[svc])


def classify(host: str, path: str) -> str | None:
    """Recognise the service by request path (primary) or Host header."""
    host = clean_host(host)
    if path.startswith("/weatherstation/"):
        return "wu"
    if path.startswith("/v01/") or path.startswith("/v1/"):
        return "wc"
    for svc in HOST_SUFFIXES:
        if is_service_host(svc, host) and path not in ("", "/"):
            # Known host but unexpected path: still recognised, forwarded transparently.
            return svc
    return None


def upstream_host(svc: str, host: str) -> str:
    """Only ever forward to the service's own domain (never an arbitrary Host header)."""
    host = clean_host(host)
    return host if is_service_host(svc, host) else DEFAULT_HOSTS[svc]


def notify_backends(app: web.Application) -> None:
    """Push the current state to every enabled Home Assistant backend (in the background)."""
    _spawn(app, app[BRIDGE].publish_update())
    if app[ESPHOME] is not None:
        _spawn(app, app[ESPHOME].publish_update())


def _spawn(app: web.Application, coro) -> None:
    task = asyncio.ensure_future(coro)
    app[TASKS].add(task)
    task.add_done_callback(app[TASKS].discard)


async def handle_upload(request: web.Request) -> web.StreamResponse:
    app = request.app
    cfg, state, store = app[CFG], app[STATE], app[STORE]
    host = request.headers.get("Host", "")
    path = request.rel_url.raw_path
    raw_query = request.rel_url.raw_query_string
    svc = classify(host, path)

    if svc is None:
        safe_query = mask_query(raw_query, WU_SECRET_KEYS | WC_SECRET_KEYS)
        log.info("404 %s host=%s path=%s%s", request.method, clean_host(host) or "-",
                 mask_wc_path(path)[:200], f"?{safe_query[:300]}" if safe_query else "")
        return web.Response(status=404, text="not found")

    pairs = query_pairs(raw_query)
    if svc == "wu":
        secrets = secrets_from_query(pairs, WU_SECRET_KEYS)
    else:
        secrets = secrets_from_wc_path(path) + secrets_from_query(pairs, WC_SECRET_KEYS)
    masked_path = mask_wc_path(path)
    masked_query = mask_query(raw_query, WU_SECRET_KEYS | WC_SECRET_KEYS)
    ts = now_iso()
    state.requests[svc] += 1
    state.last_seen[svc] = ts

    # ---------------------------------------------------------------- parse + diagnostics
    diag: dict[str, Any] = {
        "received_at": ts,
        "method": request.method,
        "host": clean_host(host) or None,
        "path": masked_path,
        "query": masked_query,
        "user_agent": request.headers.get("User-Agent"),
        "remote": request.remote,
    }
    summary = ""
    if svc == "wu":
        res = parse_wu(pairs, cfg.units)
        state.sensors.update(res.values)
        if res.values:
            state.last_reading = ts
        state.wu_fields = res.fields
        state.wu_unmapped = res.unmapped
        diag.update({
            "fields": res.fields,
            "unmapped_fields": res.unmapped,
            "invalid_values": res.invalid,
            "skipped_fields": res.skipped,
            "dateutc": res.dateutc,
            "values": res.values,
        })
        summary = f"{len(res.values)} fields → HA"
        if res.invalid:
            log.warning("WU: rejected invalid values %s", res.invalid)
    else:
        res_wc = parse_wc(path, pairs)
        diag.update({"pairs": res_wc.values, "unmapped_fields": sorted(res_wc.values)})
        summary = f"{len(res_wc.values)} pairs (diagnostics only)"

    # ---------------------------------------------------------------- forward / mock
    body = await request.read()
    resp: UpstreamResponse | None = None
    outcome: str
    if state.forward(svc):
        query = raw_query
        if svc == "wu" and (cfg.wu_username or cfg.wu_password):
            repl = {}
            if cfg.wu_username:
                repl["ID"] = cfg.wu_username
                secrets.append(cfg.wu_username)
            if cfg.wu_password:
                repl["PASSWORD"] = cfg.wu_password
                secrets.append(cfg.wu_password)
            query = replace_query_values(raw_query, repl)
        try:
            up = await app[FORWARDER].forward(request.method, upstream_host(svc, host), path, query,
                                              dict(request.headers), body)
            state.upstream_status[svc] = up.status
            if up.status >= 500:
                state.upstream_errors[svc] += 1
                log.warning("%s upstream HTTP %d → serving recorded response", SVC_LABEL[svc], up.status)
                outcome = f"upstream {up.status}, mock"
            else:
                resp = up
                outcome = f"forward {up.status}"
                if 200 <= up.status < 300:
                    store.record(svc, up)
                else:
                    log.warning("%s upstream HTTP %d passed to console", SVC_LABEL[svc], up.status)
        except UpstreamError as exc:
            state.upstream_status[svc] = "error"
            state.upstream_errors[svc] += 1
            log.warning("%s upstream failed (%s) → serving recorded response", SVC_LABEL[svc],
                        redact(str(exc), secrets)[:300])
            outcome = "upstream error, mock"
        except Exception as exc:  # never fail the console because of the proxy
            state.upstream_status[svc] = "error"
            state.upstream_errors[svc] += 1
            log.error("%s forward crashed: %s", SVC_LABEL[svc], redact(repr(exc), secrets)[:300])
            outcome = "proxy error, mock"
    else:
        outcome = "mock (forward off)"

    if resp is None:
        rec = store.get(svc)
        resp = UpstreamResponse(int(rec.get("status", 200)), str(rec.get("content_type", "text/plain")),
                                str(rec.get("body", "")).encode("utf-8"))

    diag["response"] = {"source": outcome, "status": resp.status}
    try:
        write_json(cfg.data_dir / f"last_request_{svc}.json", diag)
    except OSError as exc:
        log.error("Cannot write diagnostics: %s", exc)
    if svc == "wu":
        state.save()

    log.info("%s: %s, %s", SVC_LABEL[svc], summary, outcome)
    notify_backends(app)

    ctype = resp.content_type.split(";")[0].strip() or "text/plain"
    charset = None
    if "charset=" in resp.content_type:
        charset = resp.content_type.split("charset=")[-1].split(";")[0].strip()
    return web.Response(status=resp.status, body=resp.body, content_type=ctype, charset=charset)


async def handle_health(request: web.Request) -> web.Response:
    state = request.app[STATE]
    cfg = request.app[CFG]
    esphome = request.app[ESPHOME]
    return web.json_response({
        "ok": True,
        "mqtt_enabled": cfg.mqtt_enabled,
        "mqtt_connected": request.app[BRIDGE].connected,
        "esphome_enabled": cfg.esphome_enabled,
        "esphome_clients": len([c for c in esphome.clients if c.subscribed]) if esphome else 0,
        "forward": {"wu": state.forward("wu"), "wc": state.forward("wc")},
        "requests": state.requests,
        "upstream_errors": state.upstream_errors,
        "upstream_status": state.upstream_status,
        "last_seen": state.last_seen,
        "last_reading": state.last_reading,
    })


def create_app(cfg: Config, resolver: AbstractResolver | None = None,
               start_backends: bool = True) -> web.Application:
    logsafe.install()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app[CFG] = cfg
    state = ProxyState(cfg.data_dir, {"wu": cfg.forward_wu, "wc": cfg.forward_wc})
    app[STATE] = state
    app[STORE] = ResponseStore(cfg.data_dir)
    app[BRIDGE] = HABridge(cfg, state, on_change=lambda: notify_backends(app))
    app[ESPHOME] = (ESPHomeServer(cfg, state, on_forward_change=lambda: notify_backends(app))
                    if cfg.esphome_enabled else None)
    app[FORWARDER] = Forwarder(resolver or UpstreamResolver(cfg.upstream_dns), cfg.upstream_timeout,
                               cfg.upstream_port)
    app[TASKS] = set()

    app.router.add_get("/healthz", handle_health)
    app.router.add_route("*", "/{tail:.*}", handle_upload)

    async def lifecycle(app: web.Application):
        mqtt_task = None
        if start_backends and cfg.mqtt_enabled:
            mqtt_task = asyncio.create_task(app[BRIDGE].run())
        if start_backends and app[ESPHOME] is not None:
            await app[ESPHOME].start()
        log.info("Forward WU=%s WC=%s, upstream DNS %s", state.forward("wu"), state.forward("wc"),
                 ",".join(cfg.upstream_dns))
        yield
        bridge = app[BRIDGE]
        if bridge.client is not None:
            try:
                await asyncio.wait_for(bridge.client.publish(bridge.status_topic, "offline", qos=1,
                                                             retain=True), 2)
            except Exception:
                pass
        if mqtt_task:
            mqtt_task.cancel()
            await asyncio.gather(mqtt_task, return_exceptions=True)
        if app[ESPHOME] is not None:
            await app[ESPHOME].stop()
        for t in list(app[TASKS]):
            t.cancel()
        await app[FORWARDER].close()

    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    errors = cfg.validate()
    if errors:
        for err in errors:
            log.error("Configuration error: %s", err)
        raise SystemExit(2)
    log.info("Backends: MQTT=%s ESPHome=%s", "on" if cfg.mqtt_enabled else "off",
             "on" if cfg.esphome_enabled else "off")
    # Never log raw request lines (they contain station credentials).
    logging.getLogger("aiohttp.access").disabled = True
    web.run_app(create_app(cfg), host=cfg.http_host, port=cfg.http_port, access_log=None,
                print=None)

