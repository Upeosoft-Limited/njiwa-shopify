"""Never twice. Shopify redelivers, orders reach the same moment from two
topics, and a refund is one refund however many times it is announced."""

from __future__ import annotations

from sqlalchemy import select

from njiwa_shopify.db import Delivery, session_scope
from tests.conftest import SHOP, order_payload, post_webhook


def deliveries() -> list[Delivery]:
    with session_scope() as db:
        return list(db.scalars(select(Delivery).order_by(Delivery.id)))


class TestWebhookId:
    def test_the_same_webhook_id_twice_sends_once(self, client, shop, sent):
        payload = order_payload(financial_status="paid")
        first = post_webhook(client, "orders/paid", payload, webhook_id="wh-1")
        again = post_webhook(client, "orders/paid", payload, webhook_id="wh-1")
        assert first.status_code == again.status_code == 200
        assert len(sent) == 3
        assert len(deliveries()) == 3

    def test_a_new_webhook_id_for_the_same_order_and_topic_sends_once(self, client, shop, sent):
        payload = order_payload(financial_status="paid")
        post_webhook(client, "orders/paid", payload, webhook_id="wh-1")
        post_webhook(client, "orders/paid", payload, webhook_id="wh-2")
        assert len(sent) == 3
        assert len(deliveries()) == 3


class TestOncePerOrder:
    def test_the_shop_owner_hears_once_across_create_and_paid(self, client, shop, sent):
        post_webhook(client, "orders/create", order_payload(financial_status="pending"))
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        admin = [d for d in deliveries() if d.event == "admin"]
        assert len(admin) == 2  # two numbers, one alert each
        assert {d.recipient_hint for d in admin} == {"...0001", "...0002"}
        customer = [d for d in deliveries() if d.event != "admin"]
        assert [d.event for d in customer] == ["placed", "paid"]
        assert len(sent) == 4

    def test_the_idempotency_key_is_stable_and_names_the_recipient(self, client, shop, sent):
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        keys = [call["key"] for call in sent]
        assert len(set(keys)) == 3
        assert all(k.startswith("shopify-") and "-450789469-" in k for k in keys)
        assert any("-paid-" in k for k in keys)
        assert sum("-admin-" in k for k in keys) == 2

    def test_a_delivery_row_is_the_marker(self, client, shop, sent):
        post_webhook(client, "orders/fulfilled", order_payload(financial_status="paid"))
        rows = deliveries()
        assert len(rows) == 1
        row = rows[0]
        assert row.event == "fulfilled"
        assert row.subject == "450789469"
        assert row.status == "sent"
        assert row.message_id == "msg_1"
        assert row.text == "" and row.recipient == ""  # nothing kept once sent
        assert row.recipient_hint == "...5678"
        assert "Test key" in row.detail


class TestRefunds:
    def test_two_refunds_on_one_order_are_two_messages(self, client, shop, sent, monkeypatch):
        from njiwa_shopify import shopify
        from njiwa_shopify.orders import Order

        async def fake_fetch_order(domain, token, order_id):
            return Order.from_webhook(order_payload(financial_status="partially_refunded"))

        monkeypatch.setattr(shopify, "fetch_order", fake_fetch_order)

        refund = {
            "id": 9001,
            "order_id": 450789469,
            "transactions": [{"kind": "refund", "status": "success", "amount": "500.00", "currency": "KES"}],
        }
        post_webhook(client, "refunds/create", refund, webhook_id="r-1")
        post_webhook(client, "refunds/create", refund, webhook_id="r-1")  # redelivered
        post_webhook(client, "refunds/create", {**refund, "id": 9002}, webhook_id="r-2")

        assert len(sent) == 2
        assert "refunded KES 500.00" in sent[0]["text"]
        subjects = [d.subject for d in deliveries()]
        assert subjects == ["450789469:refund:9001", "450789469:refund:9002"]

    def test_a_refund_whose_order_cannot_be_fetched_sends_nothing(self, client, shop, sent, monkeypatch):
        from njiwa_shopify import shopify

        async def fake_fetch_order(domain, token, order_id):
            return None

        monkeypatch.setattr(shopify, "fetch_order", fake_fetch_order)
        response = post_webhook(client, "refunds/create", {"id": 1, "order_id": 2, "transactions": []})
        assert response.status_code == 200
        assert sent == []
        assert deliveries() == []
