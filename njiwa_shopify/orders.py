"""An order, as this app needs to see it.

Shopify hands over an order two ways: as the JSON body of an order webhook,
and as the answer to a GraphQL query when a refund arrives carrying only the
order's id. The two spell every field differently. Everything downstream reads
this one shape instead, built from whichever arrived.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Order:
    id: str
    name: str  # As the customer sees it, with its prefix: #1001.
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    country: str = ""  # ISO code of the address the phone came from.
    currency: str = ""
    total: str = ""
    created_at: str = ""
    financial_status: str = ""
    payment_gateways: list[str] = field(default_factory=list)
    items: list[tuple[int, str]] = field(default_factory=list)
    status_url: str = ""
    test: bool = False

    @property
    def item_count(self) -> int:
        return sum(quantity for quantity, _ in self.items)

    @classmethod
    def from_webhook(cls, payload: dict[str, Any]) -> Order:
        customer = payload.get("customer") or {}
        billing = payload.get("billing_address") or {}
        shipping = payload.get("shipping_address") or {}

        phone, country = _pick_phone(
            # The checkout phone first: Shopify has already put it in
            # international form. The addresses are as typed, and the customer
            # record is whatever was on file, which may be years old.
            (payload.get("phone"), ""),
            (shipping.get("phone"), shipping.get("country_code")),
            (billing.get("phone"), billing.get("country_code")),
            (customer.get("phone"), billing.get("country_code") or shipping.get("country_code")),
        )
        first, last = _pick_name(
            (billing.get("first_name"), billing.get("last_name")),
            (customer.get("first_name"), customer.get("last_name")),
            (shipping.get("first_name"), shipping.get("last_name")),
        )

        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            first_name=first,
            last_name=last,
            phone=phone,
            country=country,
            currency=str(payload.get("currency") or ""),
            total=str(payload.get("current_total_price") or payload.get("total_price") or ""),
            created_at=str(payload.get("created_at") or ""),
            financial_status=str(payload.get("financial_status") or ""),
            payment_gateways=[str(g) for g in (payload.get("payment_gateway_names") or [])],
            items=[
                (int(line.get("quantity") or 0), str(line.get("name") or line.get("title") or ""))
                for line in (payload.get("line_items") or [])
            ],
            status_url=str(payload.get("order_status_url") or ""),
            test=bool(payload.get("test", False)),
        )

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> Order:
        customer = node.get("customer") or {}
        billing = node.get("billingAddress") or {}
        shipping = node.get("shippingAddress") or {}

        phone, country = _pick_phone(
            (node.get("phone"), ""),
            (shipping.get("phone"), shipping.get("countryCodeV2")),
            (billing.get("phone"), billing.get("countryCodeV2")),
            (customer.get("phone"), billing.get("countryCodeV2") or shipping.get("countryCodeV2")),
        )
        first, last = _pick_name(
            (billing.get("firstName"), billing.get("lastName")),
            (customer.get("firstName"), customer.get("lastName")),
            (shipping.get("firstName"), shipping.get("lastName")),
        )

        money = (node.get("currentTotalPriceSet") or node.get("totalPriceSet") or {}).get(
            "shopMoney"
        ) or {}
        edges = ((node.get("lineItems") or {}).get("edges")) or []

        return cls(
            id=str(node.get("legacyResourceId") or ""),
            name=str(node.get("name") or ""),
            first_name=first,
            last_name=last,
            phone=phone,
            country=country,
            currency=str(money.get("currencyCode") or ""),
            total=str(money.get("amount") or ""),
            created_at=str(node.get("createdAt") or ""),
            financial_status=str(node.get("displayFinancialStatus") or "").lower(),
            payment_gateways=[str(g) for g in (node.get("paymentGatewayNames") or [])],
            items=[
                (int((e.get("node") or {}).get("quantity") or 0), str((e.get("node") or {}).get("name") or ""))
                for e in edges
            ],
            status_url=str(node.get("statusPageUrl") or ""),
            test=bool(node.get("test", False)),
        )


@dataclass
class Refund:
    id: str
    order_id: str
    amount: str
    currency: str

    @classmethod
    def from_webhook(cls, payload: dict[str, Any]) -> Refund:
        # A refund's money is in its transactions, one per gateway movement.
        # Only the ones that succeeded count; a failed attempt is not money
        # anybody is getting back.
        total = 0.0
        currency = ""
        for tx in payload.get("transactions") or []:
            if tx.get("kind") != "refund" or tx.get("status") != "success":
                continue
            try:
                total += float(tx.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            currency = currency or str(tx.get("currency") or "")
        return cls(
            id=str(payload.get("id") or ""),
            order_id=str(payload.get("order_id") or ""),
            amount=f"{total:.2f}" if total else "",
            currency=currency,
        )


def _pick_phone(*candidates: tuple[Any, Any]) -> tuple[str, str]:
    for phone, country in candidates:
        if phone and str(phone).strip():
            return str(phone).strip(), str(country or "").strip()
    return "", ""


def _pick_name(*candidates: tuple[Any, Any]) -> tuple[str, str]:
    for first, last in candidates:
        if (first and str(first).strip()) or (last and str(last).strip()):
            return str(first or "").strip(), str(last or "").strip()
    return "", ""
