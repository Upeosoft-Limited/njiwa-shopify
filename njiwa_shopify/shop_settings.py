"""What a shop chose on its settings page, and what it gets before choosing.

One JSON document on the shop row rather than a column per setting. The shape
is the settings table in PLUGIN-SPEC.md, nothing joins on it, and adding an
event is a line here rather than a migration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from njiwa_shopify import numbers, templates

DEFAULT_BASE_URL = "https://njiwa.upeo.ai"

VALID_KEY_PREFIXES = ("sk_live_", "sk_test_")


@dataclass
class ShopSettings:
    # The master switch. Off keeps everything and sends nothing.
    enabled: bool = True
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    # Which linked number sends. Empty means the account's default number.
    send_from: str = ""
    # Where the new-order alert goes. Comma separated, digits only.
    admin_numbers: str = ""
    # Whether an order Shopify marked as a test is messaged like a real one.
    # Off, because the Bogus gateway and a developer's run through the
    # checkout carry whatever number was typed into them, and that number
    # belongs to somebody who did not order anything.
    send_test_orders: bool = False
    # Every event is off until somebody turns it on. Installing this app must
    # never cause a message to be sent.
    events: dict[str, bool] = field(default_factory=dict)
    # Wording the merchant has saved. An event with no entry here uses the
    # default wording; an entry that is an empty string means the merchant
    # cleared the box, and that sends nothing.
    templates: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | None) -> ShopSettings:
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            api_key=str(data.get("api_key") or ""),
            base_url=str(data.get("base_url") or DEFAULT_BASE_URL),
            send_from=str(data.get("send_from") or ""),
            admin_numbers=str(data.get("admin_numbers") or ""),
            send_test_orders=bool(data.get("send_test_orders", False)),
            events={str(k): bool(v) for k, v in (data.get("events") or {}).items()},
            templates={str(k): str(v) for k, v in (data.get("templates") or {}).items()},
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "enabled": self.enabled,
                "api_key": self.api_key,
                "base_url": self.base_url,
                "send_from": self.send_from,
                "admin_numbers": self.admin_numbers,
                "send_test_orders": self.send_test_orders,
                "events": self.events,
                "templates": self.templates,
            }
        )

    def is_configured(self) -> bool:
        return self.api_key.strip() != ""

    def is_test_key(self) -> bool:
        return self.api_key.startswith("sk_test_")

    def is_on(self) -> bool:
        return self.enabled and self.is_configured()

    def event_on(self, event: str) -> bool:
        return bool(self.events.get(event, False))

    def template_for(self, event: str) -> str:
        if event in self.templates:
            return self.templates[event]
        return templates.default_for(event)

    def admin_number_list(self) -> list[str]:
        return numbers.parse_list(self.admin_numbers)

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.to_json())
