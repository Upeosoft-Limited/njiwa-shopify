"""Installing, and getting back in.

The standard Shopify authorisation code grant, done by hand: send the merchant
to Shopify with a nonce, get a code back with a signature, swap the code for
an offline token. And the way back in afterwards: when the merchant clicks a
non-embedded app in the admin, Shopify opens the app's address with a signed
query string, the same signature as the callback, so the same check lets them
into the settings page.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from njiwa_shopify import pages, sessions, shopify
from njiwa_shopify.config import get_settings
from njiwa_shopify.db import Shop, session_scope, utc_now
from njiwa_shopify.shopify import ShopifyError

log = logging.getLogger("njiwa_shopify")

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> Response:
    """The app's address, as the Partner Dashboard has it.

    Three visitors arrive here. Shopify, sending a merchant to install, with
    shop, timestamp and hmac in the query. Shopify, opening the app from the
    admin for a shop that is installed, with the same three. And a person
    with nothing, who gets a form.
    """
    settings = get_settings()
    params = dict(request.query_params)
    shop = (params.get("shop") or "").strip().lower()

    if not shop:
        return HTMLResponse(pages.install_page(settings.app_url))
    if not shopify.valid_shop_domain(shop):
        return HTMLResponse(
            pages.message_page(
                "That is not a Shopify address",
                f"{shop} does not look like a myshopify.com domain. It should read like "
                "example.myshopify.com.",
                back_url=settings.app_url,
            ),
            status_code=400,
        )

    if params.get("hmac"):
        if not shopify.verify_query_hmac(params, settings.shopify_api_secret):
            log.warning("%s: an app link arrived with a signature that does not match.", shop)
            return HTMLResponse(
                pages.message_page(
                    "This link cannot be verified",
                    "The signature Shopify puts on this link does not match. If you run this "
                    "app, check that SHOPIFY_API_SECRET is the client secret of the app in "
                    "the Partner Dashboard.",
                ),
                status_code=403,
            )
        if not shopify.query_is_fresh(params):
            return HTMLResponse(
                pages.message_page(
                    "This link has expired",
                    "Open the app again from your Shopify admin, under Apps.",
                ),
                status_code=403,
            )
        with session_scope() as db:
            row = db.get(Shop, shop)
            usable = (
                row is not None
                and row.is_installed
                and (row.token_expires_at is None or row.can_refresh)
            )
        if usable:
            response = RedirectResponse(url=f"{settings.app_url}/settings", status_code=303)
            sessions.set_cookie(
                response,
                sessions.SESSION_COOKIE,
                sessions.sign("session", {"shop": shop}),
                sessions.SESSION_MAX_AGE,
            )
            return response

    # Not installed, or the token can no longer be refreshed. Either way the
    # answer is to run the grant, which for an installed shop is one screen.
    return RedirectResponse(url=f"{settings.app_url}/auth?shop={shop}", status_code=303)


@router.get("/auth")
async def begin(shop: str = "") -> Response:
    settings = get_settings()
    shop = shop.strip().lower()
    if shop and "." not in shop:
        shop = f"{shop}.myshopify.com"
    if not shopify.valid_shop_domain(shop):
        return HTMLResponse(
            pages.install_page(
                settings.app_url,
                note=f"{shop or 'That'} does not look like a myshopify.com address. It should "
                "read like example.myshopify.com.",
            ),
            status_code=400,
        )

    # The nonce goes to Shopify in `state` and comes back in the callback. It
    # is also signed into a short-lived cookie, so the callback can prove the
    # install it is finishing is the one this browser started.
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(url=shopify.authorize_url(shop, state), status_code=303)
    sessions.set_cookie(
        response,
        sessions.STATE_COOKIE,
        sessions.sign("state", {"shop": shop, "state": state}),
        sessions.STATE_MAX_AGE,
    )
    return response


@router.get("/auth/callback")
async def callback(request: Request) -> Response:
    settings = get_settings()
    params = dict(request.query_params)
    shop = (params.get("shop") or "").strip().lower()

    if not shopify.valid_shop_domain(shop):
        return HTMLResponse(
            pages.message_page(
                "That is not a Shopify address", f"{shop} is not a myshopify.com domain."
            ),
            status_code=400,
        )
    if not shopify.verify_query_hmac(params, settings.shopify_api_secret):
        log.warning("%s: the OAuth callback carried a signature that does not match.", shop)
        return HTMLResponse(
            pages.message_page(
                "This callback cannot be verified",
                "The signature Shopify put on the callback does not match. If you run this "
                "app, check that SHOPIFY_API_SECRET is the client secret of the app in the "
                "Partner Dashboard.",
            ),
            status_code=403,
        )

    started = sessions.load("state", request.cookies.get(sessions.STATE_COOKIE), sessions.STATE_MAX_AGE)
    if (
        not started
        or not params.get("state")
        or not secrets.compare_digest(str(started.get("state")), str(params["state"]))
        or started.get("shop") != shop
    ):
        return HTMLResponse(
            pages.message_page(
                "This install did not start here",
                "The install either began in a different browser or took longer than ten "
                "minutes. Start it again.",
                back_url=f"{settings.app_url}/auth?shop={shop}",
            ),
            status_code=403,
        )

    code = params.get("code")
    if not code:
        return HTMLResponse(
            pages.message_page("Nothing to exchange", "Shopify sent no authorisation code."),
            status_code=400,
        )

    try:
        tokens = await shopify.exchange_code(shop, code)
        info = await shopify.fetch_shop(shop, tokens.access_token)
        created = await shopify.ensure_webhooks(shop, tokens.access_token, settings.webhook_uri)
    except ShopifyError as exc:
        log.error("%s: install failed. %s (%s)", shop, exc, exc.code)
        return HTMLResponse(
            pages.message_page(
                "Shopify could not finish the install",
                f"{exc} Try again in a moment; if it keeps happening, email hello@upeo.ai.",
                back_url=f"{settings.app_url}/auth?shop={shop}",
            ),
            status_code=502,
        )

    with session_scope() as db:
        row = db.get(Shop, shop)
        if row is None:
            row = Shop(domain=shop)
            db.add(row)
        row.name = info["name"]
        row.currency = info["currency"]
        shopify.apply_tokens(row, tokens)
        row.uninstalled_at = None
        if created:
            row.webhooks_note = (
                f"Webhooks registered on {utc_now():%d %b %Y} for {', '.join(created)}."
            )
        else:
            row.webhooks_note = (
                f"Webhooks checked on {utc_now():%d %b %Y}: all in place."
            )

    log.info("%s: installed. Webhooks created: %s", shop, ", ".join(created) or "none needed")

    response = RedirectResponse(url=f"{settings.app_url}/settings", status_code=303)
    sessions.set_cookie(
        response,
        sessions.SESSION_COOKIE,
        sessions.sign("session", {"shop": shop}),
        sessions.SESSION_MAX_AGE,
    )
    sessions.clear_cookie(response, sessions.STATE_COOKIE)
    return response
