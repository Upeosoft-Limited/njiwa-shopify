"""Signed cookies: who is looking at the settings page, and the OAuth nonce.

A non-embedded app has no Shopify session to lean on once the merchant is
past the OAuth redirect. What it has is one moment of proof, a query string
Shopify signed, and this turns that moment into a cookie the settings page
can trust for a while.
"""

from __future__ import annotations

from typing import Any

from fastapi import Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from njiwa_shopify.config import get_settings

SESSION_COOKIE = "njiwa_shopify_session"
STATE_COOKIE = "njiwa_shopify_state"
FLASH_COOKIE = "njiwa_shopify_flash"

# Twelve hours is a working day. After that the merchant clicks the app in
# the admin again, which is one click, and gets a fresh one.
SESSION_MAX_AGE = 12 * 60 * 60
# An install that takes longer than this is not one install.
STATE_MAX_AGE = 10 * 60
FLASH_MAX_AGE = 120


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=f"njiwa-shopify-{salt}")


def sign(salt: str, payload: dict[str, Any]) -> str:
    return _serializer(salt).dumps(payload)


def load(salt: str, token: str | None, max_age: int) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        data = _serializer(salt).loads(token, max_age=max_age)
    except BadSignature:
        return None
    return data if isinstance(data, dict) else None


def set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        # Secure means https only, which is every deployment of this except a
        # developer's own machine.
        secure=get_settings().is_production,
        samesite="lax",
        path="/",
    )


def clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/")


def csrf_token(shop: str) -> str:
    """A token the settings form carries back, proving the POST came from a
    page this app served to this shop's session and not from a page
    somebody else wrote."""
    return sign("csrf", {"shop": shop})


def csrf_ok(shop: str, token: str | None) -> bool:
    data = load("csrf", token, SESSION_MAX_AGE)
    return bool(data) and data.get("shop") == shop
