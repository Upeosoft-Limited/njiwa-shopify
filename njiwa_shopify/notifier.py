"""When a message goes out, and to whom.

One rule runs the whole app: an order reaching a moment sends the message for
that moment, once. Shopify is answered before anything is sent, and nothing
that fails here is allowed to become a non-200 that would make Shopify send
the webhook again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from njiwa_shopify import client, events, numbers, shopify, templates
from njiwa_shopify.client import NjiwaError
from njiwa_shopify.db import Delivery, Shop, session_scope, utc_now
from njiwa_shopify.orders import Order, Refund
from njiwa_shopify.shop_settings import ShopSettings
from njiwa_shopify.shopify import ShopifyError

log = logging.getLogger("njiwa_shopify")

# How many times a send that never reached Njiwa is tried before the row is
# marked failed. A refusal from Njiwa is never retried: it said no, and it
# will say no again.
NETWORK_ATTEMPTS = 3


def plan(
    db: Session, shop: Shop, topic: str, order: Order, refund: Refund | None = None
) -> list[int]:
    """Decide who hears about this webhook, and write the markers.

    Returns the ids of the deliveries that now need sending. Nothing here
    touches the network, so it is safe to run before Shopify is answered, and
    a row is on disk before the send is attempted.
    """
    settings = ShopSettings.from_json(shop.settings_json)
    if not settings.enabled:
        # The master switch. Said out loud, so that a merchant wondering why
        # nothing arrives finds this line rather than silence.
        log.warning(
            "%s: WhatsApp messages are switched off in the settings, so order %s (%s) "
            "sent nothing.",
            shop.domain,
            order.name,
            topic,
        )
        return []
    if not settings.is_configured():
        log.warning(
            "%s: there is no Njiwa API key saved, so order %s (%s) sent nothing.",
            shop.domain,
            order.name,
            topic,
        )
        return []
    if order.test and not settings.send_test_orders:
        # Shopify marks an order test when it came through the Bogus gateway
        # or a payment provider's test mode. The phone number on one is still
        # a real phone number, usually the merchant's own or one typed at
        # random, and a live key would message it and charge for it. Said out
        # loud, because silence here looks like a broken app.
        log.info(
            "%s: order %s (%s) is a Shopify test order, so nothing was sent. Turn on "
            "\"Message test orders too\" in the settings if you want these messaged.",
            shop.domain,
            order.name,
            topic,
        )
        return []

    # A second partial refund is a second real event, so a refund's subject
    # carries the refund id. Everything else is once per order.
    subject = order.id if refund is None else f"{order.id}:refund:{refund.id}"
    queued: list[int] = []

    event = events.customer_event_for(topic, order)
    if event and settings.event_on(event):
        queued += _plan_customer(db, shop, settings, event, subject, order, refund)

    if topic in events.ADMIN_ALERT_TOPICS and settings.event_on(events.ADMIN_EVENT):
        queued += _plan_admin(db, shop, settings, order)

    return queued


def _plan_customer(
    db: Session,
    shop: Shop,
    settings: ShopSettings,
    event: str,
    subject: str,
    order: Order,
    refund: Refund | None,
) -> list[int]:
    number = numbers.to_msisdn(order.phone, order.country)
    if not number:
        # No number, no message, and no error either. A customer without a
        # phone number is normal. It is written down so that the merchant
        # looking at a quiet order can see why.
        _insert(db, shop, event, subject, "", "", order.name, status="no_number")
        log.info(
            "%s: order %s has no usable phone number, so the %s message was not sent.",
            shop.domain,
            order.name,
            event,
        )
        return []

    text = templates.render(
        settings.template_for(event),
        order,
        event,
        shop_name=shop.name,
        shop_domain=shop.domain,
        refund=refund,
    )
    if not text:
        # Clearing the box is how a merchant turns one message off without
        # turning the event off.
        log.warning(
            "%s: the message template for %s is empty, so order %s sent nothing.",
            shop.domain,
            event,
            order.name,
        )
        return []

    row = _insert(db, shop, event, subject, number, text, order.name)
    return [row.id] if row else []


def _plan_admin(db: Session, shop: Shop, settings: ShopSettings, order: Order) -> list[int]:
    recipients = settings.admin_number_list()
    if not recipients:
        return []

    text = templates.render(
        settings.template_for(events.ADMIN_EVENT),
        order,
        events.ADMIN_EVENT,
        shop_name=shop.name,
        shop_domain=shop.domain,
    )
    if not text:
        log.warning(
            "%s: the new-order message template is empty, so order %s sent nothing to you.",
            shop.domain,
            order.name,
        )
        return []

    queued: list[int] = []
    for number in recipients:
        # The subject is the order alone, whichever topic brought it here,
        # so the alert that went out on orders/create is the one that stops
        # a second going out on orders/paid.
        row = _insert(db, shop, events.ADMIN_EVENT, order.id, number, text, order.name)
        if row:
            queued.append(row.id)
    return queued


def _insert(
    db: Session,
    shop: Shop,
    event: str,
    subject: str,
    number: str,
    text: str,
    order_name: str,
    *,
    status: str = "queued",
) -> Delivery | None:
    """Write the once-per-order marker. None if it was already there."""
    row = Delivery(
        shop_domain=shop.domain,
        event=event,
        subject=subject,
        order_name=order_name,
        recipient_hash=_digest(number) if number else "",
        recipient_hint=f"...{number[-4:]}" if number else "",
        recipient=number,
        idempotency_key=idempotency_key(shop.domain, subject, event, number) if number else "",
        status=status,
        text=text,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # An order can reach the same moment twice: Shopify redelivering, or
        # orders/paid arriving for an order the shop was already told about.
        # The customer does not need telling twice.
        log.info(
            "%s: order %s already has a %s message for this recipient. Not sending another.",
            shop.domain,
            order_name,
            event,
        )
        return None
    return row


def idempotency_key(domain: str, subject: str, event: str, number: str) -> str:
    """One key per shop, order, event and recipient.

    Njiwa honours it for 24 hours, so a send that runs twice, or a process
    that was restarted mid-way, replays the first answer instead of messaging
    the customer again. The recipient is part of the key because one alert
    can go to several of your own numbers, and they must not collapse into
    one another.
    """
    return f"shopify-{_digest(domain)[:8]}-{subject}-{event}-{_digest(number)[:6]}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def deliver(delivery_id: int) -> None:
    """The worker. Runs after Shopify has been answered."""
    with session_scope() as db:
        row = db.get(Delivery, delivery_id)
        if row is None or row.status != "queued":
            return
        shop = db.get(Shop, row.shop_domain)
        settings = ShopSettings.from_json(shop.settings_json) if shop else ShopSettings()
        to, text, key = row.recipient, row.text, row.idempotency_key
        order_name, event, domain = row.order_name, row.event, row.shop_domain

    if shop is None or not settings.is_on():
        # The settings changed between planning and sending. Terminal, and
        # said: a row that stayed queued for ever would be a message the
        # merchant thinks is still coming.
        _finish(
            delivery_id,
            "failed",
            detail="WhatsApp messages were switched off, or the API key removed, "
            "before this could be sent.",
        )
        log.warning("%s: order %s, %s: not sent, sending is switched off.", domain, order_name, event)
        return

    answer = None
    failure: NjiwaError | None = None
    for attempt in range(1, NETWORK_ATTEMPTS + 1):
        try:
            answer = await client.send_text(settings, to, text, key)
            break
        except NjiwaError as exc:
            failure = exc
            if exc.code != "connection_failed":
                break
            # Never accepted, so safe to try again. A short wait, because the
            # likeliest reason is a blip and not an outage.
            if attempt < NETWORK_ATTEMPTS:
                await asyncio.sleep(2 * attempt)

    if answer is not None:
        message_id = str(answer.get("id") or "?")
        note = "Test key, so nothing reached WhatsApp." if answer.get("sandbox") else ""
        _finish(delivery_id, "sent", message_id=message_id, detail=note)
        log.info("%s: order %s, %s: WhatsApp sent to +%s (%s).", domain, order_name, event, to, message_id)
    else:
        assert failure is not None
        _finish(delivery_id, "failed", detail=f"{failure} ({failure.code})")
        log.error(
            "%s: order %s, %s: could not WhatsApp +%s. %s (%s)",
            domain,
            order_name,
            event,
            to,
            failure,
            failure.code,
        )


def _finish(delivery_id: int, status: str, *, message_id: str = "", detail: str = "") -> None:
    with session_scope() as db:
        row = db.get(Delivery, delivery_id)
        if row is None:
            return
        row.status = status
        row.message_id = message_id
        row.detail = detail
        row.finished_at = utc_now()
        # Njiwa has the message now, or has refused it. Either way this copy
        # has done its job.
        row.text = ""
        row.recipient = ""


async def process_refund(shop_domain: str, payload: dict) -> None:
    """A refund webhook names its order and says nothing about the customer,
    so the order is fetched first. That is a call to Shopify, which is why
    this runs after Shopify has been answered rather than before."""
    refund = Refund.from_webhook(payload)

    with session_scope() as db:
        shop = db.get(Shop, shop_domain)
        if shop is None or not shop.is_installed:
            return
        settings = ShopSettings.from_json(shop.settings_json)
        if not settings.enabled or not settings.is_configured():
            log.warning(
                "%s: WhatsApp messages are switched off or have no API key, so the refund "
                "on order %s sent nothing.",
                shop_domain,
                refund.order_id,
            )
            return
        if not settings.event_on("refunded"):
            return
        try:
            token = await shopify.fresh_access_token(shop)
        except ShopifyError as exc:
            log.error("%s: refund on order %s: %s", shop_domain, refund.order_id, exc)
            return

    try:
        order = await shopify.fetch_order(shop_domain, token, refund.order_id)
    except ShopifyError as exc:
        log.error(
            "%s: could not fetch order %s for its refund, so no message was sent. %s",
            shop_domain,
            refund.order_id,
            exc,
        )
        return
    if order is None:
        log.warning(
            "%s: Shopify has no order %s for this app, so the refund message was not sent. "
            "Orders older than 60 days need the read_all_orders scope.",
            shop_domain,
            refund.order_id,
        )
        return

    with session_scope() as db:
        shop = db.get(Shop, shop_domain)
        if shop is None:
            return
        queued = plan(db, shop, "refunds/create", order, refund)

    for delivery_id in queued:
        await deliver(delivery_id)


async def resend_stranded() -> int:
    """Anything still queued when this process last stopped.

    A row is written before the send starts and finished after it ends, so a
    process killed in between leaves the row queued with its text. Sending
    it again is safe: the idempotency key means Njiwa replays an answer it
    already gave rather than sending twice.
    """
    with session_scope() as db:
        ids = list(db.scalars(select(Delivery.id).where(Delivery.status == "queued")))
    if ids:
        log.warning(
            "%d message(s) were still queued when this process last stopped. Sending them now.",
            len(ids),
        )
    for delivery_id in ids:
        await deliver(delivery_id)
    return len(ids)
