"""The settings page, and the two buttons under it.

Plain forms and a redirect after every save, so a refresh never saves twice.
Who is allowed here is whoever holds the cookie the OAuth callback or the
admin's app link set, which is to say somebody Shopify has just vouched for.
"""

from __future__ import annotations

import logging
import re
import time
from html import escape as e

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from njiwa_shopify import client, events, numbers, pages, sessions
from njiwa_shopify.client import NjiwaError
from njiwa_shopify.config import get_settings
from njiwa_shopify.db import Delivery, Shop, session_scope
from njiwa_shopify.numbers import MAX_MSISDN_DIGITS, MIN_MSISDN_DIGITS
from njiwa_shopify.shop_settings import DEFAULT_BASE_URL, VALID_KEY_PREFIXES, ShopSettings

log = logging.getLogger("njiwa_shopify")

router = APIRouter()

# Ten test messages an hour from one shop is far more than a person pressing
# a button needs, and far less than a lure left running could use.
TEST_SEND_LIMIT = 10
TEST_SEND_WINDOW_SECONDS = 60 * 60
_test_sends: dict[str, list[float]] = {}


class NotSignedIn(Exception):
    """No usable session cookie. main.py turns this into the install page."""


def current_shop(request: Request) -> str:
    data = sessions.load(
        "session", request.cookies.get(sessions.SESSION_COOKIE), sessions.SESSION_MAX_AGE
    )
    if not data or not data.get("shop"):
        raise NotSignedIn()
    return str(data["shop"])


def _installed(db, domain: str) -> Shop:  # type: ignore[no-untyped-def]
    shop = db.get(Shop, domain)
    if shop is None or not shop.is_installed:
        raise NotSignedIn()
    return shop


def _recent(db, domain: str) -> list[Delivery]:  # type: ignore[no-untyped-def]
    return list(
        db.scalars(
            select(Delivery)
            .where(Delivery.shop_domain == domain)
            .order_by(Delivery.id.desc())
            .limit(50)
        )
    )


def _flash_and_redirect(kind: str, html: str) -> Response:
    response = RedirectResponse(url=f"{get_settings().app_url}/settings", status_code=303)
    sessions.set_cookie(
        response,
        sessions.FLASH_COOKIE,
        sessions.sign("flash", {"kind": kind, "html": html}),
        sessions.FLASH_MAX_AGE,
    )
    return response


@router.get("/settings", response_class=HTMLResponse)
async def show(request: Request, domain: str = Depends(current_shop)) -> Response:
    flash = sessions.load("flash", request.cookies.get(sessions.FLASH_COOKIE), sessions.FLASH_MAX_AGE)
    with session_scope() as db:
        shop = _installed(db, domain)
        settings = ShopSettings.from_json(shop.settings_json)
        recent = _recent(db, domain)
    response = HTMLResponse(
        pages.settings_page(
            shop,
            settings,
            recent,
            app_url=get_settings().app_url,
            csrf=sessions.csrf_token(domain),
            notice=flash,
        )
    )
    if flash:
        sessions.clear_cookie(response, sessions.FLASH_COOKIE)
    return response


@router.post("/settings")
async def save(request: Request, domain: str = Depends(current_shop)) -> Response:
    form = await request.form()
    if not sessions.csrf_ok(domain, str(form.get("csrf") or "")):
        return _forbidden()

    with session_scope() as db:
        shop = _installed(db, domain)
        current = ShopSettings.from_json(shop.settings_json)
        attempt, problems = _read_form(form, current)
        if problems:
            # Nothing is saved, and nothing typed is lost: the page comes back
            # with the values as entered and the reasons at the top.
            html = "Not saved. " + " ".join(e(p) for p in problems)
            return HTMLResponse(
                pages.settings_page(
                    shop,
                    attempt,
                    _recent(db, domain),
                    app_url=get_settings().app_url,
                    csrf=sessions.csrf_token(domain),
                    notice={"kind": "error", "html": html},
                ),
                status_code=400,
            )
        shop.settings_json = attempt.to_json()

    log.info("%s: settings saved.", domain)
    return _flash_and_redirect("ok", "Saved.")


def _read_form(form, current: ShopSettings) -> tuple[ShopSettings, list[str]]:  # type: ignore[no-untyped-def]
    """Everything checked here is checked without a network call, so a Njiwa
    outage can never stop somebody saving their own settings. Whether the key
    actually works is what Test connection is for."""
    problems: list[str] = []
    attempt = ShopSettings(enabled=form.get("enabled") == "yes")

    # A password field is never echoed back, so an empty one means "keep".
    key = str(form.get("api_key") or "").strip()
    if form.get("forget_api_key") == "yes":
        attempt.api_key = ""
    elif key:
        if key.startswith(VALID_KEY_PREFIXES):
            attempt.api_key = key
        else:
            attempt.api_key = current.api_key
            problems.append(
                f"A Njiwa API key starts with sk_live_ or sk_test_. The one pasted starts "
                f"with {key[:8]}, which usually means a webhook secret or a console password "
                "was pasted by mistake."
            )
    else:
        attempt.api_key = current.api_key

    # https only, because the API key rides on every request. Over http it is
    # readable by anything on the path, and that key is the whole authority
    # over the shop's WhatsApp account.
    base_url = str(form.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE_URL
    attempt.base_url = base_url
    if not base_url.startswith("https://"):
        problems.append(
            f"The Njiwa address must start with https://. It is currently {base_url}."
        )

    send_from = str(form.get("send_from") or "").strip()
    digits = re.sub(r"\D", "", send_from)
    attempt.send_from = digits
    if send_from and not digits:
        problems.append(f"Send from: {send_from} does not contain a phone number.")
    elif digits.startswith("0"):
        problems.append(
            "Send from: write the sending number in full international form, digits only, "
            "like 254712345678. A number beginning 0 is a local number, and which country it "
            "belongs to depends on who is reading it."
        )

    attempt.send_test_orders = form.get("send_test_orders") == "yes"

    attempt.admin_numbers = str(form.get("admin_numbers") or "").strip()
    bad = numbers.rejected_from_list(attempt.admin_numbers)
    if bad:
        problems.append(
            f"Your WhatsApp numbers: {', '.join(bad)} is not a phone number. Write each one "
            "as digits in full international form, like 254712345678, separated by commas."
        )

    for event in [*events.CUSTOMER_EVENTS, events.ADMIN_EVENT]:
        attempt.events[event] = form.get(f"event_{event}") == "yes"
        # Saved even when empty. Empty is a decision: it is how a merchant
        # turns one message off without turning the event off.
        attempt.templates[event] = str(form.get(f"template_{event}") or "").strip()

    return attempt, problems


@router.post("/settings/test-connection")
async def test_connection(request: Request, domain: str = Depends(current_shop)) -> Response:
    """Who this key belongs to, and what it can send from. Sends nothing."""
    form = await request.form()
    if not sessions.csrf_ok(domain, str(form.get("csrf") or "")):
        return _forbidden()

    with session_scope() as db:
        settings = ShopSettings.from_json(_installed(db, domain).settings_json)

    if not settings.is_configured():
        return _flash_and_redirect("error", "Save an API key first, then test.")

    try:
        found = await client.numbers(settings)
    except NjiwaError as exc:
        return _flash_and_redirect("error", _refusal(exc))

    lines: list[str] = []
    if settings.is_test_key():
        lines.append(
            "<strong>This is a test key.</strong> Every message is checked and stored, and "
            "nothing reaches WhatsApp. Swap it for a key beginning sk_live_ when you are ready."
        )
    if not found:
        lines.append(
            "The key works, but this account has no numbers yet. Add one in the Njiwa "
            "console under Numbers and link it."
        )
    else:
        listed = []
        for number in found:
            msisdn = number.get("msisdn")
            listed.append(
                f"{e(str(number.get('label') or number.get('id') or ''))} — "
                f"{e('+' + str(msisdn)) if msisdn else 'not linked yet'} "
                f"({e(str(number.get('status') or ''))})"
                + (" — default" if number.get("is_default") else "")
            )
        lines.append("Connected. This key can send from:<br>" + "<br>".join(listed))

    if settings.send_from:
        known = {str(n.get("msisdn")) for n in found if n.get("msisdn")}
        if settings.send_from not in known:
            lines.append(
                "<strong>Send from does not match any number on this account, so every "
                "message will be refused.</strong> Correct it, or clear it to use the "
                "default number."
            )

    return _flash_and_redirect("ok" if found else "error", "<br><br>".join(lines))


@router.post("/settings/send-test")
async def send_test(request: Request, domain: str = Depends(current_shop)) -> Response:
    """One fixed message to a number you name, and the real outcome.

    Test connection proves the key; this proves the rest of the path, all the
    way to a phone in somebody's hand. The wording is written here and the
    caller cannot touch it: the recipient is the only thing they supply.
    """
    form = await request.form()
    if not sessions.csrf_ok(domain, str(form.get("csrf") or "")):
        return _forbidden()

    with session_scope() as db:
        shop = _installed(db, domain)
        settings = ShopSettings.from_json(shop.settings_json)
        shop_name = shop.name or shop.domain

    if not settings.enabled:
        # Loudly. The master switch off is an error somebody can find, not
        # a no-op.
        return _flash_and_redirect(
            "error",
            "Nothing was sent: WhatsApp messages are switched off. Turn the switch on, "
            "save, and try again.",
        )
    if not settings.is_configured():
        return _flash_and_redirect("error", "Save an API key first, then send a test.")

    raw = str(form.get("to") or "").strip()
    if not raw:
        return _flash_and_redirect("error", "Type the number to send the test message to.")

    # A plus, spaces, dashes and brackets are how a number is written down,
    # so take them off rather than refuse a number that is perfectly correct.
    # Then digits and nothing else: an address ending @g.us is a group, and
    # one press meant for one person would post to hundreds. A leading zero
    # is fine here; a recipient is read against the sending number's country.
    number = re.sub(r"[\s+()\-]", "", raw)
    if not re.fullmatch(r"[0-9]+", number) or not (
        MIN_MSISDN_DIGITS <= len(number) <= MAX_MSISDN_DIGITS
    ):
        return _flash_and_redirect(
            "error",
            f"{e(raw)} is not a phone number. Write it as digits, {MIN_MSISDN_DIGITS} to "
            f"{MAX_MSISDN_DIGITS} of them, like 254712345678 or 0712345678.",
        )

    if not _within_limit(domain):
        return _flash_and_redirect(
            "error",
            f"That is more than {TEST_SEND_LIMIT} test messages in an hour from this shop. "
            "Wait a while.",
        )

    try:
        # No idempotency key. Pressing the button twice should send twice; a
        # key would replay the first answer and prove nothing.
        answer = await client.send_text(
            settings,
            number,
            f"Test message from {shop_name}. If you can read this, Shopify can reach your "
            "customers on WhatsApp.",
            wait=True,
        )
    except NjiwaError as exc:
        return _flash_and_redirect("error", _refusal(exc))

    status = str(answer.get("status") or "queued")
    message = f"Sent to +{e(number)} ({e(str(answer.get('id') or '?'))}), status {e(status)}."
    if answer.get("error"):
        message += f" Njiwa reports: {e(str(answer.get('error')))}"
    if answer.get("sandbox"):
        message += " <strong>This is a test key, so nothing actually reached the phone.</strong>"
    elif answer.get("wait_timed_out"):
        message += " Njiwa is still sending it; the console under Messages will show the outcome."
    return _flash_and_redirect("ok", message)


def _within_limit(domain: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _test_sends.get(domain, []) if now - t < TEST_SEND_WINDOW_SECONDS]
    if len(recent) >= TEST_SEND_LIMIT:
        _test_sends[domain] = recent
        return False
    recent.append(now)
    _test_sends[domain] = recent
    return True


def _refusal(exc: NjiwaError) -> str:
    """Njiwa's own sentence leads. The code and its page follow, because the
    wording can change and the code does not."""
    parts = [e(str(exc))]
    if exc.code and exc.code != "unknown":
        parts.append(f"The reason code is {e(exc.code)}.")
    if exc.docs:
        parts.append(f"That code is explained at <a href=\"{e(exc.docs)}\">{e(exc.docs)}</a>")
    return " ".join(parts)


def _forbidden() -> Response:
    return HTMLResponse(
        pages.message_page(
            "That form has expired",
            "Open the settings page again and retry.",
            back_url=f"{get_settings().app_url}/settings",
        ),
        status_code=403,
    )
