"""Talking to Shopify. OAuth, signatures, and the Admin API.

Everything Shopify-specific that touches the network or a secret is here, so
the rest of the app can be read without knowing how Shopify signs things.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from njiwa_shopify import __version__
from njiwa_shopify.config import SHOPIFY_API_VERSION, SHOPIFY_SCOPES, get_settings
from njiwa_shopify.db import Shop, utc_now
from njiwa_shopify.events import TOPICS
from njiwa_shopify.orders import Order

log = logging.getLogger("njiwa_shopify")

TIMEOUT_SECONDS = 20

# Anchored at both ends. Without the anchors, evil.example.com/x.myshopify.com
# would pass and the access token would be sent to whoever owns it.
SHOP_DOMAIN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")

# How long the OAuth callback and the admin's app link are believed for.
# Shopify stamps both with the time it made them, and one that is older than
# this is a link somebody kept rather than one the merchant just clicked.
MAX_QUERY_AGE_SECONDS = 600


class ShopifyError(Exception):
    def __init__(self, message: str, code: str = "unknown", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def valid_shop_domain(shop: str | None) -> bool:
    return bool(shop) and bool(SHOP_DOMAIN.match(str(shop)))


# --- signatures --------------------------------------------------------------


def verify_query_hmac(params: Mapping[str, str], secret: str) -> bool:
    """The hmac Shopify puts on an OAuth callback and on the admin's app link.

    Everything but `hmac` itself, sorted, joined as key=value with &, signed
    with the client secret, as a hex digest. The escaping of %, = and & is
    what Shopify's own libraries do, copied exactly so an unusual value
    verifies here the way it does there.
    """
    given = params.get("hmac")
    if not given:
        return False

    pairs = []
    for key, value in params.items():
        if key == "hmac":
            continue
        k = str(key).replace("%", "%25").replace("=", "%3D")
        v = str(value).replace("%", "%25")
        pairs.append(f"{k}={v}".replace("&", "%26"))
    message = "&".join(sorted(pairs))

    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(given))


def query_is_fresh(params: Mapping[str, str]) -> bool:
    """Whether the timestamp Shopify stamped on the request is recent."""
    try:
        stamped = int(str(params.get("timestamp", "")))
    except ValueError:
        return False
    age = utc_now().timestamp() - stamped
    return -60 <= age <= MAX_QUERY_AGE_SECONDS


def verify_webhook_hmac(body: bytes, header: str | None, secret: str) -> bool:
    """The X-Shopify-Hmac-Sha256 header on every webhook.

    HMAC-SHA256 of the raw body, keyed with the client secret, base64. It has
    to be the bytes as they arrived: a body that has been parsed and
    re-serialised signs differently.
    """
    if not header:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header.strip())


# --- OAuth -------------------------------------------------------------------


def authorize_url(shop: str, state: str) -> str:
    """Where the merchant is sent to approve the install.

    No grant_options: the token asked for is an offline one, which belongs to
    the shop rather than to the person who clicked, and keeps working after
    they have logged out.
    """
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.shopify_api_key,
            "scope": SHOPIFY_SCOPES,
            "redirect_uri": settings.redirect_uri,
            "state": state,
        }
    )
    return f"https://{shop}/admin/oauth/authorize?{query}"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    scope: str
    expires_in: int | None
    refresh_expires_in: int | None


async def exchange_code(shop: str, code: str) -> TokenSet:
    """The authorisation code, once, for the shop's offline access token."""
    settings = get_settings()
    return await _token_request(
        shop,
        {
            "client_id": settings.shopify_api_key,
            "client_secret": settings.shopify_api_secret,
            "code": code,
            # Ask for a token that expires and comes with a refresh token.
            # Shopify requires this of new apps; an older app that is still
            # issued a permanent token simply gets no refresh_token back, and
            # apply_tokens copes with either.
            "expiring": "1",
        },
    )


async def refresh_access_token(shop: str, refresh_token: str) -> TokenSet:
    settings = get_settings()
    return await _token_request(
        shop,
        {
            "client_id": settings.shopify_api_key,
            "client_secret": settings.shopify_api_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


async def _token_request(shop: str, form: dict[str, str]) -> TokenSet:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(
                f"https://{shop}/admin/oauth/access_token",
                data=form,
                headers={"Accept": "application/json", "User-Agent": f"njiwa-shopify/{__version__}"},
            )
    except httpx.HTTPError as exc:
        raise ShopifyError(f"Could not reach {shop}. {exc}", "connection_failed") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        raise ShopifyError(
            f"Shopify would not issue a token for {shop} (HTTP {response.status_code}): "
            f"{response.text[:200]}",
            "token_refused",
            response.status_code,
        )

    return TokenSet(
        access_token=str(body["access_token"]),
        refresh_token=str(body["refresh_token"]) if body.get("refresh_token") else None,
        scope=str(body.get("scope") or ""),
        expires_in=int(body["expires_in"]) if body.get("expires_in") else None,
        refresh_expires_in=(
            int(body["refresh_token_expires_in"]) if body.get("refresh_token_expires_in") else None
        ),
    )


def apply_tokens(shop: Shop, tokens: TokenSet) -> None:
    now = utc_now()
    shop.access_token = tokens.access_token
    shop.refresh_token = tokens.refresh_token
    shop.scopes = tokens.scope or shop.scopes
    shop.token_expires_at = (
        now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None
    )
    shop.refresh_token_expires_at = (
        now + timedelta(seconds=tokens.refresh_expires_in) if tokens.refresh_expires_in else None
    )


async def fresh_access_token(shop: Shop) -> str:
    """The shop's access token, refreshed first if it is about to expire.

    Mutates the shop row; the caller commits it. A token with no expiry is a
    permanent one and is handed back as it is.
    """
    if not shop.access_token:
        raise ShopifyError(
            f"{shop.domain} has no access token. Open the app from the Shopify admin "
            "to connect it again.",
            "not_installed",
        )
    expires = shop.token_expires_at
    if expires is None or expires > utc_now() + timedelta(minutes=2):
        return shop.access_token
    if not shop.can_refresh:
        raise ShopifyError(
            f"The Shopify token for {shop.domain} has expired and cannot be refreshed. "
            "Open the app from the Shopify admin to connect it again.",
            "reconnect",
        )
    assert shop.refresh_token is not None
    apply_tokens(shop, await refresh_access_token(shop.domain, shop.refresh_token))
    return shop.access_token or ""


# --- Admin API ---------------------------------------------------------------


async def graphql(
    shop: str, token: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One GraphQL call. Returns `data`; anything Shopify called an error is
    raised."""
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(
                url,
                json={"query": query, "variables": variables or {}},
                headers={
                    "X-Shopify-Access-Token": token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"njiwa-shopify/{__version__}",
                },
            )
    except httpx.HTTPError as exc:
        raise ShopifyError(f"Could not reach {shop}. {exc}", "connection_failed") from exc

    if response.status_code == 401:
        raise ShopifyError(
            f"Shopify no longer accepts the access token for {shop}. Open the app from "
            "the Shopify admin to connect it again.",
            "unauthorized",
            401,
        )
    if response.status_code == 429:
        raise ShopifyError(f"Shopify is rate limiting {shop}. Try again shortly.", "throttled", 429)
    if response.status_code >= 400:
        raise ShopifyError(
            f"Shopify answered HTTP {response.status_code} for {shop}: {response.text[:200]}",
            "http_error",
            response.status_code,
        )

    try:
        body = response.json()
    except ValueError:
        raise ShopifyError(f"Shopify sent something that is not JSON for {shop}.", "bad_json") from None

    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        message = first.get("message") if isinstance(first, dict) else str(first)
        raise ShopifyError(f"Shopify refused the query for {shop}: {message}", "graphql_error")

    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


async def fetch_shop(shop: str, token: str) -> dict[str, str]:
    data = await graphql(shop, token, "{ shop { name myshopifyDomain currencyCode } }")
    node = data.get("shop") or {}
    return {
        "name": str(node.get("name") or ""),
        "domain": str(node.get("myshopifyDomain") or shop),
        "currency": str(node.get("currencyCode") or ""),
    }


_LIST_SUBSCRIPTIONS = """
query njiwaSubscriptions {
  webhookSubscriptions(first: 100) {
    edges { node { id topic uri } }
  }
}
"""

_CREATE_SUBSCRIPTION = """
mutation njiwaSubscribe($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}
"""


async def ensure_webhooks(shop: str, token: str, uri: str) -> list[str]:
    """Subscribe this shop to every topic in events.TOPICS at `uri`.

    Idempotent: a subscription that already points at this exact address is
    left alone. One pointing somewhere else, from an older deployment of this
    app say, is also left alone; Shopify deletes a subscription on its own
    once it has failed eight times in a row, and a fresh one is made here
    beside it. Returns the topics that were newly created.
    """
    data = await graphql(shop, token, _LIST_SUBSCRIPTIONS)
    existing: set[tuple[str, str]] = set()
    for edge in ((data.get("webhookSubscriptions") or {}).get("edges")) or []:
        node = edge.get("node") or {}
        existing.add((str(node.get("topic") or ""), str(node.get("uri") or "")))

    created: list[str] = []
    for topic, enum_name in TOPICS.items():
        if (enum_name, uri) in existing:
            continue
        result = await graphql(
            shop,
            token,
            _CREATE_SUBSCRIPTION,
            {"topic": enum_name, "sub": {"uri": uri, "format": "JSON"}},
        )
        payload = result.get("webhookSubscriptionCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            message = "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict))
            raise ShopifyError(
                f"Shopify would not subscribe {shop} to {topic}: {message}", "subscribe_failed"
            )
        created.append(topic)
    return created


_ORDER_QUERY = """
query njiwaOrder($id: ID!) {
  order(id: $id) {
    legacyResourceId
    name
    phone
    test
    createdAt
    displayFinancialStatus
    statusPageUrl
    paymentGatewayNames
    customer { firstName lastName phone }
    billingAddress { firstName lastName phone countryCodeV2 }
    shippingAddress { firstName lastName phone countryCodeV2 }
    totalPriceSet { shopMoney { amount currencyCode } }
    currentTotalPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 50) { edges { node { name quantity } } }
  }
}
"""


async def fetch_order(shop: str, token: str, order_id: str) -> Order | None:
    """The order a refund belongs to. A refund webhook carries the order's id
    and nothing about its customer, so the order has to be asked for.

    None when Shopify has no such order for this app, which is what it says
    for an order more than 60 days old on an app without read_all_orders.
    """
    data = await graphql(shop, token, _ORDER_QUERY, {"id": f"gid://shopify/Order/{order_id}"})
    node = data.get("order")
    if not node:
        return None
    return Order.from_graphql(node)
