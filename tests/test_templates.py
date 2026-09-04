"""The wording, and what fills it in."""

from __future__ import annotations

import logging

from njiwa_shopify import templates
from njiwa_shopify.orders import Order, Refund
from tests.conftest import order_payload


def render(template: str, event: str = "paid", **kwargs) -> str:
    order = Order.from_webhook(order_payload(financial_status="paid"))
    return templates.render(
        template, order, event, shop_name="Example Shop", shop_domain="example.myshopify.com", **kwargs
    )


class TestRender:
    def test_every_placeholder_is_filled(self):
        text = render(" ".join(templates.placeholders()))
        assert "{" not in text
        assert "Jane" in text and "Wanjiru" in text and "#1001" in text
        assert "KES 1,500.00" in text
        assert "3 September 2026" in text
        assert "Paid" in text
        assert "Manual" in text
        assert "2 x Blue shirt - Large" in text
        assert "https://example.myshopify.com/admin/orders/450789469" in text

    def test_first_name_falls_back_to_there(self):
        payload = order_payload()
        payload["customer"] = None
        payload["billing_address"]["first_name"] = ""
        payload["billing_address"]["last_name"] = ""
        payload["shipping_address"] = {"country_code": "KE"}
        order = Order.from_webhook(payload)
        assert templates.render("Hi {first_name}", order, "paid", shop_name="S", shop_domain="d") == "Hi there"

    def test_an_unknown_placeholder_is_removed_and_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="njiwa_shopify"):
            text = render("Order {order_no} is ready")
        assert text == "Order  is ready"
        assert any("{order_no}" in r.getMessage() for r in caplog.records)

    def test_empty_template_sends_nothing(self):
        assert render("") == ""
        assert render("   \n ") == ""

    def test_refund_total_only_for_refunds(self):
        assert render("{refund_total}") == ""
        refund = Refund(id="1", order_id="450789469", amount="500.00", currency="KES")
        assert render("{refund_total}", "refunded", refund=refund) == "KES 500.00"

    def test_items_stop_at_ten_and_count_the_rest(self):
        payload = order_payload()
        payload["line_items"] = [{"name": f"Item {i}", "quantity": 1} for i in range(13)]
        order = Order.from_webhook(payload)
        text = templates.render("{items}", order, "paid", shop_name="S", shop_domain="d")
        assert text.count("\n") == 10
        assert text.endswith("and 3 more items")

    def test_blank_lines_collapse_and_long_messages_are_cut(self):
        assert render("a\n\n\n\nb") == "a\n\nb"
        long = render("x" * 5000)
        assert len(long) == templates.MAX_LENGTH
        assert long.endswith("…")

    def test_every_event_has_default_wording_that_renders(self):
        for event in ("placed", "paid", "fulfilled", "cancelled", "refunded", "admin"):
            refund = Refund(id="1", order_id="1", amount="10.00", currency="KES") if event == "refunded" else None
            text = render(templates.default_for(event), event, refund=refund)
            assert text and "{" not in text


class TestFormatting:
    def test_money_keeps_the_decimals_it_was_given(self):
        assert templates.format_money("1500.00", "KES") == "KES 1,500.00"
        assert templates.format_money("1500", "JPY") == "JPY 1,500"
        assert templates.format_money("12.5", "USD") == "USD 12.50"
        assert templates.format_money("", "KES") == ""

    def test_date(self):
        assert templates.format_date("2026-09-03T10:15:00+03:00") == "3 September 2026"
        assert templates.format_date("2026-01-10T23:00:00Z") == "10 January 2026"
        assert templates.format_date("") == ""


class TestRefundPayload:
    def test_only_successful_refund_transactions_count(self):
        refund = Refund.from_webhook(
            {
                "id": 1,
                "order_id": 2,
                "transactions": [
                    {"kind": "refund", "status": "success", "amount": "300.00", "currency": "KES"},
                    {"kind": "refund", "status": "failure", "amount": "999.00", "currency": "KES"},
                    {"kind": "refund", "status": "success", "amount": "200.00", "currency": "KES"},
                ],
            }
        )
        assert refund.amount == "500.00"
        assert refund.currency == "KES"


class TestOrderFromGraphql:
    def test_the_graphql_shape_reads_the_same(self):
        node = {
            "legacyResourceId": "450789469",
            "name": "#1001",
            "phone": None,
            "test": False,
            "createdAt": "2026-09-03T07:15:00Z",
            "displayFinancialStatus": "PARTIALLY_REFUNDED",
            "statusPageUrl": "https://example.myshopify.com/orders/x",
            "paymentGatewayNames": ["shopify_payments"],
            "customer": {"firstName": "Jane", "lastName": "Wanjiru", "phone": "+254712345678"},
            "billingAddress": {"firstName": "Jane", "lastName": "Wanjiru", "phone": "0712345678", "countryCodeV2": "KE"},
            "shippingAddress": None,
            "totalPriceSet": {"shopMoney": {"amount": "1500.0", "currencyCode": "KES"}},
            "currentTotalPriceSet": {"shopMoney": {"amount": "1000.0", "currencyCode": "KES"}},
            "lineItems": {"edges": [{"node": {"name": "Blue shirt - Large", "quantity": 2}}]},
        }
        order = Order.from_graphql(node)
        webhook = Order.from_webhook(order_payload())
        assert order.id == webhook.id and order.name == webhook.name
        assert (order.phone, order.country) == ("0712345678", "KE")
        assert order.first_name == "Jane"
        assert order.items == [(2, "Blue shirt - Large")]
        assert order.total == "1000.0"
        assert order.payment_gateways == ["shopify_payments"]
