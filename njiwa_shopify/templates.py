"""The message itself.

A template is plain text with placeholders in braces. Every placeholder the
shop can use is listed in placeholders() below, and that same list is what the
settings page prints, so the documentation cannot drift from the code.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from njiwa_shopify.orders import Order, Refund

log = logging.getLogger("njiwa_shopify")

# WhatsApp takes 4096 characters. Stopping short leaves room for a footer.
MAX_LENGTH = 4000

# How many order lines {items} prints before it starts counting instead.
MAX_ITEMS = 10

_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def placeholders() -> dict[str, str]:
    """Placeholder => what it is replaced with, in the shop's own words."""
    return {
        "{first_name}": 'The billing first name, or "there" if the order has none.',
        "{last_name}": "The billing last name.",
        "{customer_name}": "Both names together.",
        "{order_number}": "The order number as the customer sees it, such as #1001.",
        "{order_total}": "The total, with your currency.",
        "{order_date}": "The date the order was placed.",
        "{order_status}": "The status the order has just moved to.",
        "{payment_method}": "How they paid, as Shopify names the gateway.",
        "{items}": 'One line per item, as "2 x Blue shirt - Large".',
        "{item_count}": "How many items in total.",
        "{shop_name}": "Your shop name.",
        "{order_url}": "A link the customer can open to see their own order.",
        "{admin_url}": (
            "A link that opens the order in your Shopify admin. "
            "Only put this in the message to yourself."
        ),
        "{refund_total}": (
            "The amount of the refund that was just made, with your currency. "
            "Filled in only for the refund message; empty everywhere else."
        ),
    }


def default_for(event: str) -> str:
    """What each message says before anybody edits it.

    They are deliberately short. A WhatsApp message that reads like an email
    gets read like an email, which is to say not at all.
    """
    defaults = {
        "placed": (
            "Hi {first_name}, we have your order {order_number} for {order_total}. "
            "We will let you know the moment your payment comes through.\n\n{shop_name}"
        ),
        "paid": (
            "Hi {first_name}, thank you. Your payment for order {order_number} came "
            "through and we are getting it ready.\n\n{items}\n\nTotal {order_total}\n{shop_name}"
        ),
        "fulfilled": (
            "Hi {first_name}, order {order_number} is on its way to you. "
            "Thank you for shopping with {shop_name}."
        ),
        "cancelled": (
            "Hi {first_name}, order {order_number} has been cancelled. If you had "
            "already paid, the money is on its way back to you. If that was not you, "
            "reply to this message and we will look into it.\n\n{shop_name}"
        ),
        "refunded": (
            "Hi {first_name}, we have refunded {refund_total} for order {order_number}. "
            "Banks take a few days to show it.\n\n{shop_name}"
        ),
        "admin": (
            "New order {order_number} on {shop_name}.\n\n{customer_name}\n"
            "{item_count} item(s), {order_total}\nPaid by {payment_method}\n\n{admin_url}"
        ),
    }
    return defaults.get(event, "")


def render(
    template: str,
    order: Order,
    event: str,
    *,
    shop_name: str,
    shop_domain: str,
    refund: Refund | None = None,
) -> str:
    """The message, or "" if the template is empty."""
    template = (template or "").strip()
    if not template:
        return ""

    values = _values(order, event, shop_name=shop_name, shop_domain=shop_domain, refund=refund)
    message = template
    for token, value in values.items():
        message = message.replace(token, value)

    # Anything still in braces is a placeholder that does not exist, usually
    # a typo. Sending "{order_no}" to a customer looks broken, so it comes out
    # and the shop is told where to look.
    unknown = sorted(set(_PLACEHOLDER.findall(message)))
    if unknown:
        log.warning(
            "Unknown placeholder %s in the %s message template for %s. "
            "It was removed before sending.",
            ", ".join(unknown),
            event,
            shop_domain,
        )
        message = _PLACEHOLDER.sub("", message)

    message = re.sub(r"\n{3,}", "\n\n", message).strip()

    if len(message) > MAX_LENGTH:
        message = message[: MAX_LENGTH - 1] + "…"

    return message


def _values(
    order: Order,
    event: str,
    *,
    shop_name: str,
    shop_domain: str,
    refund: Refund | None,
) -> dict[str, str]:
    from njiwa_shopify.events import status_label

    first = order.first_name or "there"
    full_name = " ".join(part for part in (order.first_name, order.last_name) if part)

    return {
        "{first_name}": first,
        "{last_name}": order.last_name,
        "{customer_name}": full_name,
        "{order_number}": order.name,
        "{order_total}": format_money(order.total, order.currency),
        "{order_date}": format_date(order.created_at),
        "{order_status}": status_label(event),
        "{payment_method}": ", ".join(_humanise(g) for g in order.payment_gateways),
        "{items}": _items(order),
        "{item_count}": str(order.item_count),
        "{shop_name}": shop_name or shop_domain,
        "{order_url}": order.status_url,
        # The old-style admin address, because Shopify still redirects it to
        # wherever the admin lives this year and the store handle need not
        # be known here.
        "{admin_url}": f"https://{shop_domain}/admin/orders/{order.id}" if order.id else "",
        "{refund_total}": format_money(refund.amount, refund.currency) if refund else "",
    }


def _items(order: Order) -> str:
    lines: list[str] = []
    more = 0
    for quantity, name in order.items:
        if len(lines) >= MAX_ITEMS:
            more += 1
            continue
        lines.append(f"{quantity} x {name}")
    if more == 1:
        lines.append("and 1 more item")
    elif more > 1:
        lines.append(f"and {more} more items")
    return "\n".join(lines)


def format_money(amount: str, currency: str) -> str:
    """"KES 1,500.00", keeping however many decimals Shopify sent, so a
    currency without minor units is not given two of them."""
    if not amount:
        return ""
    try:
        value = Decimal(amount)
    except InvalidOperation:
        return f"{currency} {amount}".strip()
    decimals = max(0, -value.as_tuple().exponent) if value == value.to_integral() else 2
    text = f"{value:,.{decimals}f}"
    return f"{currency} {text}".strip()


def format_date(iso: str) -> str:
    """"3 September 2026". Shopify's timestamps carry the shop's own offset,
    so the day is the day the shop saw."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{when.day} {when:%B %Y}"


def _humanise(gateway: str) -> str:
    """shopify_payments -> Shopify payments. Anything already written for
    people, like "Cash on Delivery (COD)", is left alone."""
    text = gateway.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else ""
