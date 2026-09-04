"""Test fixtures.

Every test runs against its own SQLite file and nothing here touches the
network: Njiwa is a recorder, and the two Shopify calls a refund needs are
stubbed. Everything in between is real, and it is the part that decides who
gets messaged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest

SECRET = "test-client-secret"
SHOP = "example.myshopify.com"

import os  # noqa: E402

os.environ["SHOPIFY_API_KEY"] = "test-client-id"
os.environ["SHOPIFY_API_SECRET"] = SECRET
os.environ["NJIWA_SHOPIFY_APP_URL"] = "https://njiwa-shopify.example.test"
os.environ["NJIWA_SHOPIFY_ENV"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from njiwa_shopify import client as njiwa_client  # noqa: E402
from njiwa_shopify import db, events, sessions  # noqa: E402
from njiwa_shopify.config import get_settings  # noqa: E402
from njiwa_shopify.db import Shop, session_scope  # noqa: E402
from njiwa_shopify.main import create_app  # noqa: E402
from njiwa_shopify.shop_settings import ShopSettings  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("NJIWA_SHOPIFY_DATABASE_URL", f"sqlite:///{tmp_path}/njiwa-shopify.db")
    get_settings.cache_clear()
    db.reset_engine()
    yield create_app()
    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sent(monkeypatch) -> list[dict[str, Any]]:
    """Every message the app tried to send, instead of Njiwa."""
    calls: list[dict[str, Any]] = []

    async def fake_send_text(settings, to, text, idempotency_key="", wait=False):
        calls.append({"to": to, "text": text, "key": idempotency_key, "wait": wait})
        return {"id": f"msg_{len(calls)}", "status": "sent", "sandbox": True, "to": to}

    monkeypatch.setattr(njiwa_client, "send_text", fake_send_text)
    return calls


def all_events_on() -> dict[str, bool]:
    return {event: True for event in [*events.CUSTOMER_EVENTS, events.ADMIN_EVENT]}


def install_shop(**overrides: Any) -> ShopSettings:
    """A shop that has finished OAuth, with every event switched on unless a
    test says otherwise."""
    settings = ShopSettings(
        enabled=True,
        api_key="sk_test_abc123",
        admin_numbers="254700000001, 254700000002",
        events=all_events_on(),
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    with session_scope() as session:
        session.merge(
            Shop(
                domain=SHOP,
                name="Example Shop",
                currency="KES",
                access_token="shpat_test",
                settings_json=settings.to_json(),
            )
        )
    return settings


@pytest.fixture
def shop(client) -> ShopSettings:
    return install_shop()


def sign_body(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def sign_query(params: dict[str, str], secret: str = SECRET) -> str:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def post_webhook(
    client: TestClient,
    topic: str,
    payload: dict[str, Any],
    *,
    webhook_id: str | None = None,
    shop: str = SHOP,
    secret: str = SECRET,
    signature: str | None = None,
):
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": shop,
        "X-Shopify-Webhook-Id": webhook_id or str(uuid.uuid4()),
        "X-Shopify-Hmac-Sha256": signature if signature is not None else sign_body(body, secret),
    }
    return client.post("/webhooks/shopify", content=body, headers=headers)


def order_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 450789469,
        "name": "#1001",
        "order_number": 1001,
        "email": "jane@example.com",
        "phone": "+254712345678",
        "financial_status": "pending",
        "fulfillment_status": None,
        "currency": "KES",
        "total_price": "1500.00",
        "current_total_price": "1500.00",
        "created_at": "2026-09-03T10:15:00+03:00",
        "cancelled_at": None,
        "test": False,
        "order_status_url": "https://example.myshopify.com/12345/orders/abc/authenticate?key=1",
        "payment_gateway_names": ["manual"],
        "customer": {"first_name": "Jane", "last_name": "Wanjiru", "phone": "+254712345678"},
        "billing_address": {
            "first_name": "Jane",
            "last_name": "Wanjiru",
            "phone": "0712 345 678",
            "country_code": "KE",
        },
        "shipping_address": {"phone": None, "country_code": "KE"},
        "line_items": [
            {"title": "Blue shirt", "name": "Blue shirt - Large", "quantity": 2, "variant_title": "Large"},
            {"title": "Socks", "name": "Socks", "quantity": 1, "variant_title": None},
        ],
    }
    payload.update(overrides)
    return payload


def signed_in(client: TestClient, shop: str = SHOP) -> None:
    client.cookies.set(sessions.SESSION_COOKIE, sessions.sign("session", {"shop": shop}))


def csrf(shop: str = SHOP) -> str:
    return sessions.csrf_token(shop)
