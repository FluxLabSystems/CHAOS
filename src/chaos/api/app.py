"""FastAPI application factory.

Routers are discovered from ``chaos.api.routers`` so that subsystems
can be added without editing a central import list.

Plugins add a second, deliberately separate surface. Their routers are mounted
under ``/api/v1/ext/<plugin>`` and are included *after* every core router, so a
third-party integration can neither shadow a core route nor change the order in
which core routes are matched. On a platform where a URL can start a pump, which
half of the API you are talking to should be visible in the URL.
"""

from __future__ import annotations

import importlib
import ipaddress
import logging
import pkgutil
import secrets
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from chaos import __version__
from chaos.api.metrics import METRICS_CONTENT_TYPE, render_metrics
from chaos.config import Settings, get_settings
from chaos.db import build_engine, create_all, get_session_factory
from chaos.mqtt import MessageBus, build_bus
from chaos.plugins.manager import PluginManager
from chaos.runtime import ServiceManager, build_services

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


#: Routers are matched in registration order, and a ``{x:path}`` parameter is
#: greedy -- ``/points/{point_id:path}`` would otherwise swallow
#: ``/points/{point_id:path}/current``. Modules may declare ``ROUTER_PRIORITY``
#: (lower = registered earlier) so the more specific route wins.
DEFAULT_ROUTER_PRIORITY = 100

#: The graceful-stop route the process supervisor calls (docs/api.md, "Unlisted
#: operational routes"). It sits outside every prefix in the gateway's
#: route-ownership manifest on purpose: the gateway never proxies an unclaimed
#: path, so the only way to reach it is a direct connection to the backend's
#: loopback listener, which is where the supervisor lives.
SUPERVISOR_STOP_PATH = "/_supervisor/stop"
SUPERVISOR_SECRET_HEADER = "X-Chaos-Supervisor-Secret"


def is_loopback_peer(host: str | None) -> bool:
    """True for ``127.0.0.0/8``, ``::1`` and the IPv4-mapped form of either.

    uvicorn's ``proxy_headers`` default trusts ``X-Forwarded-For`` from
    ``127.0.0.1``, and the gateway always sets that header when it relays a
    request. A request that came *through* the gateway therefore presents the
    LAN client's address here and is refused -- which is the point: the stop
    route is for the supervisor's own connection, never for a browser.
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def _interrupt_process() -> None:
    """Fallback when no shutdown hook is registered: behave like Ctrl-C."""
    logger.warning("Supervisor stop requested with no shutdown hook registered; raising SIGINT")
    signal.raise_signal(signal.SIGINT)


def discover_routers() -> list[APIRouter]:
    """Import every module under ``api.routers`` and collect its ``router``."""
    import chaos.api.routers as routers_pkg

    found: list[tuple[int, str, APIRouter]] = []
    for module_info in pkgutil.iter_modules(routers_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module_name = f"{routers_pkg.__name__}.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.exception("Failed to import router module %s", module_name)
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            priority = getattr(module, "ROUTER_PRIORITY", DEFAULT_ROUTER_PRIORITY)
            found.append((priority, module_info.name, router))
        else:
            logger.warning("Router module %s exposes no APIRouter named 'router'", module_name)

    found.sort(key=lambda item: (item[0], item[1]))
    return [router for _, _, router in found]


def create_app(
    settings: Settings | None = None,
    *,
    bus: MessageBus | None = None,
    start_services: bool | None = None,
    init_db: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    should_start = settings.mqtt_enabled if start_services is None else start_services

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init_db:
            create_all(app.state.engine)
        manager: ServiceManager = build_services(
            settings,
            app.state.session_factory,
            app.state.bus,
            plugins=getattr(app.state, "plugins", None),
        )
        app.state.services = manager
        if should_start:
            app.state.bus.start()
            manager.start_all()
        try:
            yield
        finally:
            if should_start:
                manager.stop_all()
                app.state.bus.stop()

    app = FastAPI(
        title=settings.api_title,
        version=__version__,
        root_path=settings.api_root_path,
        lifespan=lifespan,
        description=(
            "Local-first operational control plane for the homestead. "
            "Write endpoints require a named operator and an audit reason."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    engine = build_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    from chaos import db as _db

    _db.configure(engine)
    app.state.session_factory = get_session_factory()
    app.state.bus = bus if bus is not None else build_bus(settings)
    app.state.plugins = PluginManager.discover(
        settings,
        session_factory=app.state.session_factory,
        bus=app.state.bus,
    )

    for router in discover_routers():
        app.include_router(router, prefix="/api/v1")

    for record, plugin_router, prefix in app.state.plugins.routers():
        try:
            app.include_router(plugin_router, prefix=f"/api/v1{prefix}")
        except Exception:
            # A malformed router -- a duplicate operation ID, an unresolvable
            # response model -- must cost that plugin its API and nothing else.
            logger.exception("Plugin %s contributed a router that could not be mounted", record.name)

    @app.get("/health", tags=["platform"])
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "node_role": settings.node_role,
            "site_id": settings.site_id,
            "physical_control_enabled": settings.allow_physical_control,
        }

    @app.get(
        "/metrics",
        tags=["platform"],
        summary="Prometheus exposition of the platform's own counters",
        response_class=PlainTextResponse,
    )
    def metrics() -> PlainTextResponse:
        # Read-only and unauthenticated, like /health: it carries counts, never
        # values of points, never credentials. deploy/prometheus/prometheus.yml
        # scrapes it.
        return PlainTextResponse(render_metrics(app), media_type=METRICS_CONTENT_TYPE)

    @app.post(SUPERVISOR_STOP_PATH, include_in_schema=False)
    def supervisor_stop(request: Request) -> JSONResponse:
        # Contract (shared with windows/src/Chaos.Host.Supervisor): 404 while no
        # secret is configured, so a container or a checkout has no such route
        # at all; 403 for a non-loopback peer or a wrong secret; 202 with
        # {"stopping": true}, after which the server shuts down the way Ctrl-C
        # would -- the lifespan's finally block stops the services and the bus.
        secret = settings.supervisor_stop_secret
        if not secret:
            raise HTTPException(status_code=404, detail="Not Found")
        peer = request.client.host if request.client else None
        if not is_loopback_peer(peer):
            raise HTTPException(status_code=403, detail="The stop route accepts loopback peers only.")
        presented = request.headers.get(SUPERVISOR_SECRET_HEADER, "")
        if not secrets.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
            raise HTTPException(status_code=403, detail="Supervisor secret mismatch.")
        # The hook is set by `chaos serve` (it flips uvicorn's should_exit).
        # Run after the response has gone out, so the caller sees the 202.
        hook = getattr(app.state, "request_shutdown", None)
        trigger = hook if callable(hook) else _interrupt_process
        logger.info("Graceful stop requested by the supervisor from %s", peer)
        return JSONResponse({"stopping": True}, status_code=202, background=BackgroundTask(trigger))

    if WEB_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index():
            index_file = WEB_DIR / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)
            # A missing operator UI must not present as a server fault -- the
            # API is the control path and stays usable on its own.
            return JSONResponse(
                {
                    "status": "ok",
                    "detail": "Operator UI is not installed; the API is available at /api/v1.",
                    "docs": "/docs",
                    "health": "/health",
                },
                status_code=200,
            )

    return app
