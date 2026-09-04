"""Which Shopify moment is which message.

Shopify fires a webhook once, at the moment a thing becomes true, which is
exactly what the spec asks for. There is no "the order was saved" topic in
this list on purpose: orders/updated fires when somebody corrects an address,
and a customer would be messaged for it.
"""

from __future__ import annotations

from njiwa_shopify.orders import Order

# The Shopify topics this app subscribes to, and their names in the GraphQL
# enum the subscription is created with.
TOPICS: dict[str, str] = {
    "orders/create": "ORDERS_CREATE",
    "orders/paid": "ORDERS_PAID",
    "orders/fulfilled": "ORDERS_FULFILLED",
    "orders/cancelled": "ORDERS_CANCELLED",
    "refunds/create": "REFUNDS_CREATE",
    "app/uninstalled": "APP_UNINSTALLED",
}

# Shopify requires every app to answer these. They are not subscribed to
# through the API; their address is set in the Partner Dashboard, and they
# arrive at the same endpoint as everything else.
COMPLIANCE_TOPICS = ("customers/data_request", "customers/redact", "shop/redact")

# The moments worth telling a customer about, and what each is called on the
# settings page.
CUSTOMER_EVENTS: dict[str, str] = {
    "placed": "Order placed, payment not in yet",
    "paid": "Payment received",
    "fulfilled": "Order fulfilled",
    "cancelled": "Order cancelled",
    "refunded": "Order refunded",
}

ADMIN_EVENT = "admin"

# When you get told about a new order.
#
# On Shopify an order exists only once a checkout has completed: an abandoned
# cart is a checkout, not an order, and never reaches orders/create. So
# orders/create is the first moment the order is real, and the alert goes out
# on it. orders/paid is there as the second chance for an order that was
# created while the app was not listening; the once-per-order marker stops it
# going twice.
ADMIN_ALERT_TOPICS = ("orders/create", "orders/paid")


def customer_event_for(topic: str, order: Order) -> str | None:
    """The customer message a topic maps to, or None if there is not one."""
    if topic == "orders/create":
        # Only when the order came in waiting for money: bank deposit, cash on
        # delivery, any manual method. An order paid by card at the checkout
        # arrives with orders/paid a moment later and hears about that instead;
        # telling them "we are waiting for your payment" first would be wrong.
        # An authorised card that has not been captured is not "waiting for
        # payment" from the customer's side either, so it hears at capture.
        return "placed" if order.financial_status == "pending" else None
    if topic == "orders/paid":
        return "paid"
    if topic == "orders/fulfilled":
        return "fulfilled"
    if topic == "orders/cancelled":
        return "cancelled"
    if topic == "refunds/create":
        return "refunded"
    return None


def status_label(event: str) -> str:
    """What {order_status} says for each message."""
    return {
        "placed": "Awaiting payment",
        "paid": "Paid",
        "fulfilled": "Fulfilled",
        "cancelled": "Cancelled",
        "refunded": "Refunded",
        ADMIN_EVENT: "New",
    }.get(event, "")


def event_help(event: str) -> str:
    return {
        "placed": (
            "For bank deposit, cash on delivery and anything else where the order is "
            "placed before the money arrives. Tell them you have it and that you are "
            "waiting. An order paid by card at the checkout skips this and gets "
            "Payment received instead."
        ),
        "paid": "The one most shops want. Payment has landed and you are getting the order ready.",
        "fulfilled": (
            "Sent when the whole order is marked fulfilled. Shopify sends its own email "
            "at the same moment; this arrives where people actually look."
        ),
        "cancelled": "Worth sending. A cancellation nobody explained is what turns into a phone call.",
        "refunded": (
            "Money is on its way back. Sent for every refund, partial ones included, "
            "with the amount of that refund. Saying so stops the \"where is my refund\" "
            "message before it is sent."
        ),
    }.get(event, "")
