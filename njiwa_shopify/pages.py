"""The pages this app serves. Plain HTML, built by hand.

No template engine and no script. There are four pages and they are forms; a
dependency for that would be a dependency for its own sake. Every field on
the settings page carries its own description, because a setting whose
meaning has to be looked up somewhere else is a setting people get wrong.
"""

from __future__ import annotations

from datetime import datetime
from html import escape as e
from typing import Any

from njiwa_shopify import events, templates
from njiwa_shopify.db import Delivery, Shop
from njiwa_shopify.shop_settings import ShopSettings

STYLE = """
body { font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #222;
       background: #f6f6f4; margin: 0; }
main { max-width: 760px; margin: 0 auto; padding: 24px 20px 60px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 36px 0 8px; padding-top: 16px; border-top: 1px solid #ddd; }
p, li { margin: 6px 0; }
.muted { color: #666; font-size: 14px; }
label { display: block; font-weight: 600; margin-top: 18px; }
label.check { font-weight: 600; }
label.check input { margin-right: 6px; }
.desc { color: #555; font-size: 13.5px; margin: 3px 0 6px; }
input[type=text], input[type=password], textarea { width: 100%; box-sizing: border-box;
       font: inherit; padding: 7px 9px; border: 1px solid #bbb; border-radius: 4px; background: #fff; }
textarea { min-height: 96px; }
code { background: #eceae4; padding: 1px 4px; border-radius: 3px; font-size: 13px; }
button { font: inherit; padding: 7px 14px; border: 1px solid #555; border-radius: 4px;
         background: #fff; cursor: pointer; margin-right: 8px; }
button.primary { background: #222; color: #fff; border-color: #222; }
.notice { padding: 10px 14px; border-radius: 4px; margin: 16px 0; border: 1px solid; }
.notice.ok { background: #eef7ee; border-color: #9fcf9f; }
.notice.error { background: #fbeeee; border-color: #e0a0a0; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e3e3e3; vertical-align: top; }
th { color: #555; font-weight: 600; }
.inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
.inline input[type=text] { width: 220px; }
footer { margin-top: 48px; color: #777; font-size: 13px; }
"""


def layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{e(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}"
        "<footer>Njiwa for Shopify · <a href=\"https://docs.njiwa.upeo.ai\">Docs</a> · "
        "<a href=\"https://console.upeo.ai\">Console</a> · UPEO.AI · hello@upeo.ai</footer>"
        "</main></body></html>"
    )


def notice_html(notice: dict[str, Any] | None) -> str:
    if not notice:
        return ""
    kind = "ok" if notice.get("kind") == "ok" else "error"
    return f"<div class=\"notice {kind}\">{notice.get('html', '')}</div>"


def install_page(app_url: str, note: str = "") -> str:
    body = (
        "<h1>Njiwa for Shopify</h1>"
        "<p class=\"muted\">WhatsApp your customers when their order is paid, sent, "
        "cancelled or refunded, and get a message yourself when one comes in.</p>"
        + (f"<div class=\"notice error\">{e(note)}</div>" if note else "")
        + f"<form method=\"get\" action=\"{e(app_url)}/auth\">"
        "<label for=\"shop\">Your shop's myshopify.com address</label>"
        "<p class=\"desc\">The one Shopify gave you, such as <code>example.myshopify.com</code>, "
        "not the domain your customers see. You will be sent to Shopify to approve the "
        "install, then brought back here to the settings.</p>"
        "<input type=\"text\" id=\"shop\" name=\"shop\" placeholder=\"example.myshopify.com\" required>"
        "<p><button class=\"primary\" type=\"submit\">Install</button></p>"
        "</form>"
        "<p class=\"muted\">Already installed? Open the app from your Shopify admin, under "
        "Apps, and you will land on the settings page signed in.</p>"
    )
    return layout("Njiwa for Shopify", body)


def message_page(title: str, text: str, *, back_url: str | None = None) -> str:
    body = f"<h1>{e(title)}</h1><p>{e(text)}</p>"
    if back_url:
        body += f"<p><a href=\"{e(back_url)}\">Back</a></p>"
    return layout(title, body)


def settings_page(
    shop: Shop,
    settings: ShopSettings,
    recent: list[Delivery],
    *,
    app_url: str,
    csrf: str,
    notice: dict[str, Any] | None = None,
) -> str:
    action = f"{e(app_url)}/settings"

    parts: list[str] = [
        f"<h1>Njiwa for Shopify</h1>",
        f"<p class=\"muted\">{e(shop.name or shop.domain)} · {e(shop.domain)}</p>",
        notice_html(notice),
    ]

    if not settings.enabled:
        parts.append(
            "<div class=\"notice error\">Sending is switched off. Every setting is kept and "
            "nothing is sent until it is switched back on.</div>"
        )
    elif not settings.is_configured():
        parts.append(
            "<div class=\"notice error\">There is no Njiwa API key saved yet, so nothing can "
            "be sent. Paste one below and save.</div>"
        )

    # --- Connection ------------------------------------------------------
    parts.append(f"<form method=\"post\" action=\"{action}\">")
    parts.append(f"<input type=\"hidden\" name=\"csrf\" value=\"{e(csrf)}\">")
    parts.append("<h2>Connection</h2>")
    parts.append("<p class=\"desc\">Njiwa sends the WhatsApp messages. Your shop tells it when.</p>")

    parts.append(
        _check(
            "enabled",
            "Send WhatsApp messages",
            settings.enabled,
            "The master switch. Turn it off and this app stops sending anything at all, "
            "without losing your key, your numbers or your wording. Orders carry on exactly "
            "as before.",
        )
    )

    if settings.api_key:
        saved = (
            f"A key ending <code>{e(settings.api_key[-4:])}</code> is saved"
            + (" and it is a <strong>test key</strong>, so nothing reaches a phone." if settings.is_test_key() else " and it is a <strong>live key</strong>, so messages reach real phones and cost money.")
            + " Leave this empty to keep it."
        )
    else:
        saved = "No key is saved yet."
    parts.append(
        "<label for=\"api_key\">API key</label>"
        f"<p class=\"desc\">{saved} Create one in the Njiwa console under API keys. A key "
        "beginning <code>sk_test_</code> checks and stores every message and delivers "
        "nothing, which is what you want while you set this up. A key beginning "
        "<code>sk_live_</code> sends to real phones. The console shows a key once and keeps "
        "only its fingerprint, so a lost key is replaced rather than recovered.</p>"
        "<input type=\"password\" id=\"api_key\" name=\"api_key\" autocomplete=\"off\" value=\"\">"
    )
    if settings.api_key:
        parts.append(
            "<p class=\"desc\"><label class=\"check\" style=\"margin-top:6px;font-weight:normal\">"
            "<input type=\"checkbox\" name=\"forget_api_key\" value=\"yes\">"
            "Forget the saved key</label></p>"
        )

    parts.append(
        _text(
            "base_url",
            "Njiwa address",
            settings.base_url,
            "Leave this exactly as it is. It exists for shops that have been given their "
            "own Njiwa address, and changing it otherwise stops messages reaching anybody.",
        )
    )
    parts.append(
        _text(
            "send_from",
            "Send from",
            settings.send_from,
            "Which of your linked WhatsApp numbers these messages come from. Digits only, "
            "in full international form, such as 254712345678. Leave it empty to use the "
            "number marked default in the console, which is the right answer if you have "
            "one number.",
            placeholder="254712345678",
        )
    )
    parts.append(
        _check(
            "send_test_orders",
            "Message test orders too",
            settings.send_test_orders,
            "Off, and an order Shopify marks as a test sends nothing: the Bogus gateway and "
            "a payment provider's test mode both put a real phone number on an order nobody "
            "placed, and a live key would message that person and charge you for it. Each "
            "one that is skipped is written to the log. Turn this on only while you are "
            "watching what comes out.",
        )
    )

    # --- Customers -------------------------------------------------------
    parts.append("<h2>Messages to your customers</h2>")
    parts.append(
        "<p class=\"desc\">Each message is plain text. Anything in braces is filled in from "
        "the order:</p><p class=\"desc\">"
        + "<br>".join(
            f"<code>{e(token)}</code> — {e(meaning)}" for token, meaning in templates.placeholders().items()
        )
        + "</p>"
    )
    for event, label in events.CUSTOMER_EVENTS.items():
        parts.append(_check(f"event_{event}", label, settings.event_on(event), events.event_help(event)))
        parts.append(
            _area(
                f"template_{event}",
                settings.template_for(event),
                f"The message sent when an order reaches <strong>{e(label)}</strong>. Leave it "
                "empty and nothing is sent, whatever the tick box says.",
            )
        )

    # --- Admin -----------------------------------------------------------
    parts.append("<h2>The message to you</h2>")
    parts.append(
        "<p class=\"desc\">One message when an order comes in. On Shopify an order exists "
        "only once the checkout is complete, so an abandoned cart never wakes you up.</p>"
    )
    parts.append(
        _check(
            "event_admin",
            "Tell me about new orders",
            settings.event_on(events.ADMIN_EVENT),
            "Send me a WhatsApp message when an order comes in.",
        )
    )
    parts.append(
        _text(
            "admin_numbers",
            "Your WhatsApp numbers",
            settings.admin_numbers,
            "Where that message goes. Digits only, in full international form, separated by "
            "commas if there are several. Everybody listed gets their own copy.",
            placeholder="254712345678, 254733000111",
        )
    )
    parts.append(
        _area(
            "template_admin",
            settings.template_for(events.ADMIN_EVENT),
            "What that message says. <code>{admin_url}</code> is worth having here: it opens "
            "the order straight from your phone.",
        )
    )
    parts.append("<p style=\"margin-top:24px\"><button class=\"primary\" type=\"submit\">Save</button></p>")
    parts.append("</form>")

    # --- Check it works --------------------------------------------------
    parts.append("<h2>Check it works</h2>")
    parts.append(
        "<p class=\"desc\">Both buttons use the settings as they are saved, not as they are "
        "on screen. Save first, then check.</p>"
    )
    parts.append(
        f"<form method=\"post\" action=\"{action}/test-connection\" class=\"inline\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{e(csrf)}\">"
        "<button type=\"submit\">Test connection</button>"
        "<span class=\"desc\">Lists the numbers your Njiwa account really has. Sends nothing.</span>"
        "</form>"
    )
    parts.append(
        f"<form method=\"post\" action=\"{action}/send-test\" class=\"inline\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{e(csrf)}\">"
        "<input type=\"text\" name=\"to\" placeholder=\"Your own number\" required>"
        "<button type=\"submit\">Send me a test message</button>"
        "<span class=\"desc\">One fixed message to that number, with the saved key. A live key "
        "sends a real message.</span>"
        "</form>"
    )

    # --- Recent ----------------------------------------------------------
    parts.append("<h2>Recent messages</h2>")
    if shop.webhooks_note:
        parts.append(f"<p class=\"desc\">{e(shop.webhooks_note)}</p>")
    if not recent:
        parts.append(
            "<p class=\"muted\">Nothing yet. Once an order reaches one of the moments above, "
            "what was sent, or why it was not, is listed here.</p>"
        )
    else:
        rows = "".join(_delivery_row(d) for d in recent)
        parts.append(
            "<table><thead><tr><th>When (UTC)</th><th>Order</th><th>Message</th><th>To</th>"
            f"<th>Outcome</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    return layout("Njiwa for Shopify", "".join(parts))


def _check(name: str, label: str, on: bool, desc: str) -> str:
    checked = " checked" if on else ""
    return (
        f"<label class=\"check\"><input type=\"checkbox\" name=\"{e(name)}\" value=\"yes\"{checked}>"
        f"{e(label)}</label><p class=\"desc\">{desc}</p>"
    )


def _text(name: str, label: str, value: str, desc: str, *, placeholder: str = "") -> str:
    return (
        f"<label for=\"{e(name)}\">{e(label)}</label><p class=\"desc\">{desc}</p>"
        f"<input type=\"text\" id=\"{e(name)}\" name=\"{e(name)}\" value=\"{e(value)}\" "
        f"placeholder=\"{e(placeholder)}\">"
    )


def _area(name: str, value: str, desc: str) -> str:
    return (
        f"<p class=\"desc\">{desc}</p>"
        f"<textarea id=\"{e(name)}\" name=\"{e(name)}\">{e(value)}</textarea>"
    )


def _delivery_row(d: Delivery) -> str:
    label = events.CUSTOMER_EVENTS.get(d.event) or ("New order, to you" if d.event == events.ADMIN_EVENT else d.event)
    if d.status == "sent":
        outcome = f"Sent ({e(d.message_id)})" + (f". {e(d.detail)}" if d.detail else "")
    elif d.status == "failed":
        outcome = f"Not sent. {e(d.detail)}"
    elif d.status == "no_number":
        outcome = "No phone number on the order, so nothing was sent."
    else:
        outcome = "Queued"
    return (
        f"<tr><td>{e(_when(d.created_at))}</td><td>{e(d.order_name)}</td><td>{e(label)}</td>"
        f"<td>{e(d.recipient_hint)}</td><td>{outcome}</td></tr>"
    )


def _when(value: datetime | None) -> str:
    return value.strftime("%d %b %Y %H:%M") if value else ""
