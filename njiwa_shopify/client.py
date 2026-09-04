"""Talking to Njiwa. Transport only.

Nothing in here decides when to message anybody. It takes a shop's settings,
makes the call, and turns a refusal into an exception the rest of the app can
read.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from njiwa_shopify import __version__
from njiwa_shopify.shop_settings import DEFAULT_BASE_URL, ShopSettings

log = logging.getLogger("njiwa_shopify")

# Long enough for a slow line, short enough that nothing holds a worker.
TIMEOUT_SECONDS = 20


class NjiwaError(Exception):
    """Anything Njiwa refused, or could not be asked.

    `code` is the stable, machine readable reason and is the thing to branch
    on. The wording of the message can change; the code does not. `docs` is a
    page explaining that exact code.
    """

    def __init__(
        self, message: str, code: str = "unknown", status: int = 0, docs: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.docs = docs


async def send_text(
    settings: ShopSettings, to: str, text: str, idempotency_key: str = "", wait: bool = False
) -> dict[str, Any]:
    """Send one text message. Returns Njiwa's answer, including the message id.

    `idempotency_key` is for anything that must not go twice. Njiwa honours
    it for 24 hours, so a retried job replays the first answer instead of
    messaging the customer again.

    `wait` asks Njiwa to hold the call for up to fifteen seconds until the
    message has actually been sent or has failed, and is for the settings
    page's test button, where somebody is watching and wants the real
    outcome. It is left off for an order, which is answered as soon as Njiwa
    has the message, because nothing there is waiting on the reply. An answer
    carrying `wait_timed_out` is one Njiwa was still sending when the wait ran
    out; the message is not lost.
    """
    headers: dict[str, str] = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    body: dict[str, Any] = {"to": to, "text": text}

    # Only when the shop named a number. Left out, Njiwa uses the account's
    # default, which is the right answer for the shops that have one number
    # and never think about this again.
    sender = re.sub(r"\D", "", settings.send_from or "")
    if sender:
        body["from"] = sender

    return await _request(
        settings,
        "POST",
        "/v1/messages",
        body=body,
        headers=headers,
        params={"wait": "true"} if wait else None,
    )


async def numbers(settings: ShopSettings) -> list[dict[str, Any]]:
    """The WhatsApp numbers on this account, linked or not."""
    answer = await _request(settings, "GET", "/v1/instances")
    data = answer.get("data")
    return data if isinstance(data, list) else []


async def _request(
    settings: ShopSettings,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    key = settings.api_key.strip()
    if not key:
        raise NjiwaError(
            "There is no Njiwa API key saved for this shop, so nothing can be sent.",
            "not_configured",
        )

    address = (settings.base_url or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    sent_headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": f"njiwa-shopify/{__version__}",
        **(headers or {}),
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.request(
                method, f"{address}{path}", json=body, headers=sent_headers, params=params
            )
    except httpx.HTTPError as exc:
        # A network failure is not a send failure: the message was never
        # accepted, so trying again later is safe.
        raise NjiwaError(
            f"Could not reach Njiwa at {address}. {exc.__class__.__name__}: {exc}",
            "connection_failed",
        ) from exc

    try:
        decoded = response.json()
    except ValueError:
        decoded = {}
    if not isinstance(decoded, dict):
        decoded = {}

    if response.status_code >= 400:
        error = decoded.get("error") if isinstance(decoded.get("error"), dict) else {}
        raise NjiwaError(
            str(error.get("message") or f"Njiwa answered with HTTP {response.status_code}."),
            str(error.get("code") or "unknown"),
            response.status_code,
            error.get("docs"),
        )

    return decoded
