"""Which moment sends what, and the two rules that are not negotiable: every
event is off until somebody turns it on, and the master switch off is loud."""

from __future__ import annotations

import logging

from sqlalchemy import select

from njiwa_shopify.db import Delivery, session_scope
from njiwa_shopify.shop_settings import ShopSettings
from tests.conftest import install_shop, order_payload, post_webhook


class TestDefaults:
    def test_a_fresh_install_sends_nothing(self, client, sent):
        install_shop(events={})
        for topic in ("orders/create", "orders/paid", "orders/fulfilled", "orders/cancelled"):
            assert post_webhook(client, topic, order_payload(financial_status="paid")).status_code == 200
        assert sent == []

    def test_default_settings_have_every_event_off(self):
        fresh = ShopSettings.from_json("{}")
        assert not any(fresh.event_on(e) for e in ("placed", "paid", "fulfilled", "cancelled", "refunded", "admin"))
        assert fresh.template_for("paid")  # but wording that works unedited


class TestMasterSwitch:
    def test_off_sends_nothing_and_says_so(self, client, sent, caplog):
        install_shop(enabled=False)
        with caplog.at_level(logging.WARNING, logger="njiwa_shopify"):
            post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert sent == []
        assert any("switched off" in r.getMessage() for r in caplog.records)

    def test_no_key_sends_nothing_and_says_so(self, client, sent, caplog):
        install_shop(api_key="")
        with caplog.at_level(logging.WARNING, logger="njiwa_shopify"):
            post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert sent == []
        assert any("no Njiwa API key" in r.getMessage() for r in caplog.records)


class TestMoments:
    def test_placed_only_when_payment_is_pending(self, client, shop, sent):
        install_shop(events={"placed": True})
        post_webhook(client, "orders/create", order_payload(id=1, financial_status="paid"))
        post_webhook(client, "orders/create", order_payload(id=2, financial_status="authorized"))
        post_webhook(client, "orders/create", order_payload(id=3, financial_status="pending"))
        assert len(sent) == 1
        assert "waiting" not in sent[0]["text"].lower() or "payment comes through" in sent[0]["text"]
        assert "#1001" in sent[0]["text"]

    def test_paid_message_uses_the_default_wording(self, client, sent):
        install_shop(events={"paid": True})
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert len(sent) == 1
        text = sent[0]["text"]
        assert text.startswith("Hi Jane, thank you.")
        assert "2 x Blue shirt - Large" in text
        assert "Total KES 1,500.00" in text
        assert text.endswith("Example Shop")
        assert sent[0]["to"] == "254712345678"

    def test_the_admin_alert_goes_to_every_listed_number(self, client, sent):
        install_shop(events={"admin": True})
        post_webhook(client, "orders/create", order_payload())
        assert sorted(c["to"] for c in sent) == ["254700000001", "254700000002"]
        assert "New order #1001 on Example Shop." in sent[0]["text"]
        assert "https://example.myshopify.com/admin/orders/450789469" in sent[0]["text"]

    def test_no_phone_number_sends_nothing_and_is_written_down(self, client, sent):
        install_shop(events={"paid": True})
        payload = order_payload(financial_status="paid", phone=None)
        payload["customer"]["phone"] = None
        payload["billing_address"]["phone"] = None
        post_webhook(client, "orders/paid", payload)
        assert sent == []
        with session_scope() as db:
            rows = list(db.scalars(select(Delivery)))
        assert len(rows) == 1 and rows[0].status == "no_number"

    def test_a_group_address_in_the_admin_numbers_is_never_used(self, client, sent):
        install_shop(events={"admin": True}, admin_numbers="120363028712345678@g.us, 254700000001")
        post_webhook(client, "orders/create", order_payload())
        assert [c["to"] for c in sent] == ["254700000001"]

    def test_empty_wording_sends_nothing(self, client, sent):
        install_shop(events={"paid": True}, templates={"paid": ""})
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert sent == []

    def test_a_shop_that_is_not_installed_is_ignored(self, client, sent):
        response = post_webhook(client, "orders/paid", order_payload(), shop="stranger.myshopify.com")
        assert response.status_code == 200
        assert sent == []

    def test_uninstall_clears_the_key_and_token(self, client, shop, sent):
        from njiwa_shopify.db import Shop
        from tests.conftest import SHOP

        post_webhook(client, "app/uninstalled", {"id": 1, "domain": SHOP})
        with session_scope() as db:
            row = db.get(Shop, SHOP)
            assert row is not None
            assert not row.is_installed
            assert row.access_token is None
            assert ShopSettings.from_json(row.settings_json).api_key == ""
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert sent == []


class TestRecovery:
    def test_a_message_stranded_by_a_restart_is_sent_on_startup(self, app, sent):
        from fastapi.testclient import TestClient

        from njiwa_shopify import notifier
        from njiwa_shopify.db import Shop

        with TestClient(app):
            install_shop(events={"paid": True})
            with session_scope() as db:
                shop = db.get(Shop, "example.myshopify.com")
                from njiwa_shopify.orders import Order

                queued = notifier.plan(db, shop, "orders/paid", Order.from_webhook(order_payload(financial_status="paid")))
            assert len(queued) == 1
        assert sent == []  # planned, never delivered: the process "died" here

        with TestClient(app):
            pass  # startup sweeps
        assert len(sent) == 1
        with session_scope() as db:
            row = db.get(Delivery, queued[0])
            assert row.status == "sent"


class TestOrdersMarkedTest:
    """Shopify's Bogus gateway and every payment provider's test mode put a
    real phone number on an order nobody placed. Messaging it costs money and
    reaches a stranger, so it does not happen unless the shop asks for it."""

    def test_a_test_order_sends_nothing_and_says_so(self, client, sent, caplog):
        install_shop(events={"paid": True, "admin": True})
        with caplog.at_level(logging.INFO, logger="njiwa_shopify"):
            response = post_webhook(
                client, "orders/paid", order_payload(financial_status="paid", test=True)
            )
        assert response.status_code == 200
        assert sent == []
        assert any("test order" in r.getMessage() for r in caplog.records)

    def test_it_is_off_on_a_fresh_install(self):
        assert not ShopSettings.from_json("{}").send_test_orders

    def test_the_shop_can_ask_for_them(self, client, sent):
        install_shop(events={"paid": True}, send_test_orders=True)
        post_webhook(client, "orders/paid", order_payload(financial_status="paid", test=True))
        assert len(sent) == 1

    def test_a_real_order_is_unaffected(self, client, sent):
        install_shop(events={"paid": True})
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert len(sent) == 1

    def test_nothing_is_written_down_either(self, client, sent):
        install_shop(events={"paid": True, "admin": True})
        post_webhook(client, "orders/paid", order_payload(financial_status="paid", test=True))
        with session_scope() as db:
            assert db.scalars(select(Delivery)).first() is None
