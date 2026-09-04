"""Where Shopify delivers.

One address for every topic, the order webhooks and the compliance ones
alike, dispatched on the X-Shopify-Topic header. Every request is checked
against the client secret before its body is read as anything, and a bad
signature is answered 401, which is what Shopify's own check of the
compliance endpoint expects to see.

Shopify gives an endpoint five seconds and retries on anything but a 2xx.
Nothing here waits on Njiwa: what is decided is decided and written down,
the 200 goes back, and the sending happens after it.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from njiwa_shopify import events, notifier, shopify
from njiwa_shopify.config import get_settings
from njiwa_shopify.db import Delivery, Shop, WebhookReceipt, session_scope, utc_now
from njiwa_shopify.orders import Order

log = logging.getLogger("njiwa_shopify")

router = APIRouter()

OK = "ok"


@router.post("/webhooks/shopify")
async def receive(request: Request, background: BackgroundTasks) -> Response:
    body = await request.body()
    if not shopify.verify_webhook_hmac(
        body, request.headers.get("x-shopify-hmac-sha256"), get_settings().shopify_api_secret
    ):
        log.warning(
            "A webhook arrived for %s whose signature does not match. It was refused.",
            request.headers.get("x-shopify-shop-domain", "an unknown shop"),
        )
        return PlainTextResponse(
            "The X-Shopify-Hmac-Sha256 header does not match this body.", status_code=401
        )

    topic = request.headers.get("x-shopify-topic", "").strip()
    domain = request.headers.get("x-shopify-shop-domain", "").strip().lower()
    webhook_id = request.headers.get("x-shopify-webhook-id", "").strip()

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        # Signed by Shopify and still not JSON. There is nothing to do with
        # it, and a non-200 would only bring it back.
        log.error("%s: webhook %s (%s) was not JSON. Ignored.", domain, webhook_id, topic)
        return PlainTextResponse(OK)
    if not isinstance(payload, dict):
        payload = {}

    queued: list[int] = []
    with session_scope() as db:
        if webhook_id:
            if db.get(WebhookReceipt, webhook_id) is not None:
                log.info(
                    "%s: webhook %s (%s) was delivered before. Nothing more to do.",
                    domain,
                    webhook_id,
                    topic,
                )
                return PlainTextResponse(OK)
            # Recorded in the same transaction as everything decided below,
            # so a failure past this point leaves no receipt and the retry
            # Shopify then makes is processed as if it were the first.
            db.add(WebhookReceipt(webhook_id=webhook_id, shop_domain=domain, topic=topic))

        if topic in events.COMPLIANCE_TOPICS:
            _comply(db, topic, domain, payload)
            return PlainTextResponse(OK)

        shop = db.get(Shop, domain)

        if topic == "app/uninstalled":
            if shop is not None:
                _uninstalled(shop)
            return PlainTextResponse(OK)

        if shop is None or not shop.is_installed:
            log.warning(
                "%s: a %s webhook arrived for a shop that is not installed here. Ignored.",
                domain,
                topic,
            )
            return PlainTextResponse(OK)

        if topic == "refunds/create":
            # A refund carries only its order's id. Finding the customer means
            # asking Shopify, and that is not done while Shopify waits.
            background.add_task(notifier.process_refund, domain, payload)
        elif topic in events.TOPICS:
            queued = notifier.plan(db, shop, topic, Order.from_webhook(payload))
        else:
            log.warning("%s: a %s webhook arrived, which this app did not ask for. Ignored.", domain, topic)

    for delivery_id in queued:
        background.add_task(notifier.deliver, delivery_id)
    return PlainTextResponse(OK)


def _uninstalled(shop: Shop) -> None:
    """Removing the app removes the key.

    A live Njiwa key left in this database after somebody deleted the app is
    a key nobody is looking after any more, so the settings go with the
    token. The delivery log stays for the 48 hours until Shopify sends
    shop/redact, which removes the rest.
    """
    shop.access_token = None
    shop.refresh_token = None
    shop.token_expires_at = None
    shop.refresh_token_expires_at = None
    shop.settings_json = "{}"
    shop.webhooks_note = ""
    shop.uninstalled_at = utc_now()
    log.info("%s: uninstalled. Token and settings cleared.", shop.domain)


def _comply(db: Session, topic: str, domain: str, payload: dict) -> None:
    """Shopify's mandatory privacy webhooks.

    This app holds very little about a customer: the order number and the
    last four digits of the number a message went to, and the message text
    only until it has been sent. So a data request has nothing to return, a
    customer redaction removes those rows, and a shop redaction removes
    everything about the shop.
    """
    if topic == "customers/data_request":
        customer = payload.get("customer") or {}
        log.warning(
            "%s: customers/data_request %s for customer %s. This app keeps no customer "
            "data beyond order numbers and the last four digits of a recipient number in "
            "its delivery log, and no message text once sent. Nothing to hand over; "
            "answer the shop within 30 days.",
            domain,
            (payload.get("data_request") or {}).get("id", "?"),
            customer.get("id", "?"),
        )
        return

    if topic == "customers/redact":
        order_ids = [str(o) for o in (payload.get("orders_to_redact") or [])]
        removed = 0
        for order_id in order_ids:
            removed += db.execute(
                delete(Delivery).where(
                    Delivery.shop_domain == domain,
                    or_(Delivery.subject == order_id, Delivery.subject.like(f"{order_id}:%")),
                )
            ).rowcount
        log.info(
            "%s: customers/redact removed %d delivery row(s) for %d order(s).",
            domain,
            removed,
            len(order_ids),
        )
        return

    if topic == "shop/redact":
        db.execute(delete(Delivery).where(Delivery.shop_domain == domain))
        db.execute(delete(WebhookReceipt).where(WebhookReceipt.shop_domain == domain))
        shop = db.get(Shop, domain)
        if shop is not None:
            db.delete(shop)
        log.info("%s: shop/redact removed everything about the shop.", domain)
