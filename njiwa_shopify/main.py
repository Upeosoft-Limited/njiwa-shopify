"""The application. Run it on its own, or mount its router in another.

    uvicorn njiwa_shopify.main:create_app --factory

To mount it inside another FastAPI app instead, include `router` under a
prefix, register `not_signed_in_handler` for `NotSignedIn`, call `init_db()`
at startup, and set NJIWA_SHOPIFY_APP_URL to the public address including
that prefix.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from njiwa_shopify import __version__, notifier, pages
from njiwa_shopify.config import get_settings
from njiwa_shopify.db import init_db
from njiwa_shopify.routers import router
from njiwa_shopify.routers.settings import NotSignedIn

__all__ = ["create_app", "router", "init_db", "NotSignedIn", "not_signed_in_handler"]


async def not_signed_in_handler(request: Request, exc: Exception) -> Response:
    return HTMLResponse(
        pages.install_page(
            get_settings().app_url,
            note="You are not signed in. Open the app from your Shopify admin, under Apps, "
            "and you will land here signed in.",
        ),
        status_code=401,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("njiwa_shopify")
    init_db()
    log.info("njiwa-shopify %s starting at %s", __version__, settings.app_url)
    # A restart is the likeliest moment for a message to have been stranded
    # between "decided" and "sent", and this process is starting from one.
    await notifier.resend_stranded()
    yield
    log.info("njiwa-shopify stopping")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Njiwa for Shopify",
        version=__version__,
        lifespan=lifespan,
        # No API docs: the only machine that calls this service is Shopify,
        # and it already knows the one address it uses.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_exception_handler(NotSignedIn, not_signed_in_handler)
    app.include_router(router)
    return app
