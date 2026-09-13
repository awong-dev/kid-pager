
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager



logger = logging.getLogger("relay.api")


def create_app(
    settings: Settings | None = None,
) -> FastAPI:

    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings

    app = FastAPI(title="School Pager Relay", lifespan=lifespan)

    return app


app = create_app()
