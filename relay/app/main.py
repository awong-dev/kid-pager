"""FastAPI app: the relay's request-driven HTTP surface; device traffic arrives on /webhooks/mqtt."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import alerts as alerts_module
from app import live, push_webpush, scheduler, static_web
from app.backends.registry import build_registry
from app.backends.webapp import NullFCMClient
from app.broker import BrokerClient, make_broker_client
from app.broker_mqtt import MqttBrokerClient
from app.config import Settings, auth_backend
from app.db.firestore import get_db
from app.ingest import Ingest
from app.location import Location
from app.logging_config import configure_logging, request_id_var
from app.routers import (
    admin,
    auth_home,
    battery,
    book,
    bridge,
    ca,
    conversations,
    dev,
    devdb,
    device_book,
    devices,
    family,
    family_bridges,
    fwcache,
    internal,
    me,
    push,
    webhooks,
)
from app.routing import Routing

configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("relay.api")

REQUEST_ID_HEADER = "X-Request-Id"


def create_app(
    settings: Settings | None = None,
    broker_client: BrokerClient | None = None,
) -> FastAPI:
    """Build the app; tests inject a fake `BrokerClient`."""

    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.broker = broker_client or make_broker_client(settings)
        # One backend registry per app; /api/me/backends calls `start_link()` on a new backend.
        fcm_client = None
        if settings.push_backend == "fcm":
            from app.backends.fcm import FirebaseFCMClient

            fcm_client = FirebaseFCMClient()
        # alerts keeps a module-level FCM client; reset it on every create_app().
        alerts_module.set_fcm_client(fcm_client if fcm_client is not None else NullFCMClient())
        push_webpush.set_sender(
            push_webpush.WebPushSender(
                settings.webpush_vapid_private_key,  # type: ignore[arg-type]
                settings.webpush_subject or "mailto:pager@localhost",
            )
            if settings.webpush_configured
            else push_webpush.NullWebPushSender()
        )
        app.state.backend_registry = build_registry(app.state.broker, fcm_client=fcm_client)
        # One Routing instance shared by the webhook path and every API router.
        app.state.routing = Routing(app.state.broker, registry=app.state.backend_registry)
        app.state.ingest = Ingest(app.state.broker, app.state.routing)
        # Shares the Routing instance so a claimed `loc_req` redelivers through one registry.
        app.state.location = Location(app.state.routing)
        get_db()  # fail fast at startup if Firestore/Auth are misconfigured
        # Under BROKER_MODE=mqtt, device messages go to the same dispatch as the webhook route.
        mqtt_client = app.state.broker if isinstance(app.state.broker, MqttBrokerClient) else None
        if mqtt_client is not None:
            ingest = app.state.ingest
            mqtt_client.start(lambda topic, payload, _qos: webhooks.dispatch(ingest, topic, payload))
        scheduled = None
        if settings.scheduler_mode == "internal":
            scheduled = asyncio.create_task(scheduler.run(app.state.routing))
        yield
        if scheduled is not None:
            scheduled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduled
        if mqtt_client is not None:
            mqtt_client.stop()

    app = FastAPI(title="School Pager Relay", lifespan=lifespan)

    # Correlation id: the caller's X-Request-Id if sent, else generated; echoed on the response.
    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    app.include_router(webhooks.router)
    app.include_router(bridge.router)
    app.include_router(ca.router)
    app.include_router(admin.router)
    app.include_router(dev.router)
    if settings.dev_mode:
        # Raw store access for e2e tooling; never registered in a real deployment.
        app.include_router(devdb.router)
    app.include_router(devices.router)
    app.include_router(battery.router)
    app.include_router(device_book.router)
    app.include_router(conversations.router)
    app.include_router(family.router)
    app.include_router(family_bridges.router)
    app.include_router(book.router)
    app.include_router(me.router)
    app.include_router(push.router)
    app.include_router(internal.router)
    if auth_backend() == "home":
        app.include_router(auth_home.router)
        app.include_router(auth_home.setup_router)
    if settings.fw_cache == "local":
        app.include_router(fwcache.router)
    if settings.db_backend == "sqlite":
        # Live reads over SSE need the shim's change feed.
        app.include_router(live.router)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # Checks Firestore and the broker; no Cloud Run probe uses this route.
        firestore_ok = True
        try:
            get_db().collection("settings").document("meta").get()
        except Exception:
            logger.exception("healthz: firestore check failed")
            firestore_ok = False

        broker_ok = True
        try:
            broker_ok = app.state.broker.healthcheck()
        except Exception:
            logger.exception("healthz: broker check failed")
            broker_ok = False

        ok = firestore_ok and broker_ok
        body = {"ok": ok, "firestore": firestore_ok, "broker": broker_ok}
        return JSONResponse(status_code=200 if ok else 503, content=body)

    # Registered last so it only sees unclaimed paths.
    if static_web.WEB_ROOT.is_dir():
        static_web.mount(app)

    return app


app = create_app()
