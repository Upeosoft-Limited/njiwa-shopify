"""Signatures. Every request Shopify makes carries one, and the app must
refuse anything that does not check out before it reads a byte of it."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from njiwa_shopify import shopify
from njiwa_shopify.db import Delivery, Shop, WebhookReceipt, session_scope
from tests.conftest import SECRET, SHOP, order_payload, post_webhook, sign_body, sign_query


class TestQueryHmac:
    def test_a_signed_query_verifies(self):
        params = {"shop": SHOP, "timestamp": "1756880000", "code": "abc", "state": "xyz"}
        params["hmac"] = sign_query(params)
        assert shopify.verify_query_hmac(params, SECRET)

    def test_a_changed_parameter_does_not(self):
        params = {"shop": SHOP, "timestamp": "1756880000", "code": "abc"}
        params["hmac"] = sign_query(params)
        params["shop"] = "evil.myshopify.com"
        assert not shopify.verify_query_hmac(params, SECRET)

    def test_the_wrong_secret_does_not(self):
        params = {"shop": SHOP, "timestamp": "1756880000"}
        params["hmac"] = sign_query(params, "another-secret")
        assert not shopify.verify_query_hmac(params, SECRET)

    def test_no_hmac_at_all(self):
        assert not shopify.verify_query_hmac({"shop": SHOP}, SECRET)

    def test_shop_domain_is_anchored(self):
        assert shopify.valid_shop_domain("example.myshopify.com")
        assert shopify.valid_shop_domain("my-shop-2.myshopify.com")
        assert not shopify.valid_shop_domain("example.myshopify.com.evil.example")
        assert not shopify.valid_shop_domain("evil.example/example.myshopify.com")
        assert not shopify.valid_shop_domain("-bad.myshopify.com")
        assert not shopify.valid_shop_domain("")
        assert not shopify.valid_shop_domain(None)


class TestWebhookHmac:
    def test_matches_base64_of_hmac_sha256_over_raw_body(self):
        body = b'{"id": 1}'
        assert shopify.verify_webhook_hmac(body, sign_body(body), SECRET)

    def test_a_reserialised_body_does_not_match(self):
        body = b'{"id": 1}'
        assert not shopify.verify_webhook_hmac(b'{"id":1}', sign_body(body), SECRET)

    def test_missing_header(self):
        assert not shopify.verify_webhook_hmac(b"{}", None, SECRET)
        assert not shopify.verify_webhook_hmac(b"{}", "", SECRET)

    def test_wrong_secret(self):
        body = b"{}"
        assert not shopify.verify_webhook_hmac(body, sign_body(body, "other"), SECRET)


class TestWebhookEndpoint:
    def test_bad_signature_is_401_and_nothing_is_recorded(self, client, shop, sent):
        webhook_id = str(uuid.uuid4())
        response = post_webhook(
            client, "orders/paid", order_payload(), webhook_id=webhook_id, signature="nope"
        )
        assert response.status_code == 401
        assert sent == []
        with session_scope() as db:
            assert db.get(WebhookReceipt, webhook_id) is None
            assert db.scalars(select(Delivery)).first() is None

    def test_missing_signature_is_401(self, client, shop, sent):
        body = json.dumps(order_payload()).encode()
        response = client.post(
            "/webhooks/shopify",
            content=body,
            headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Shop-Domain": SHOP},
        )
        assert response.status_code == 401
        assert sent == []

    def test_good_signature_is_200(self, client, shop, sent):
        response = post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        assert response.status_code == 200
        assert len(sent) == 3  # the customer, and two admin numbers

    def test_compliance_topics_are_acknowledged_and_refused_alike(self, client, shop):
        payload = {"shop_id": 1, "shop_domain": SHOP, "customer": {"id": 5}, "orders_requested": []}
        assert post_webhook(client, "customers/data_request", payload).status_code == 200
        assert post_webhook(client, "customers/data_request", payload, signature="x").status_code == 401
        assert post_webhook(client, "customers/redact", {"shop_domain": SHOP, "orders_to_redact": []}).status_code == 200
        assert post_webhook(client, "customers/redact", {}, signature="x").status_code == 401
        assert post_webhook(client, "shop/redact", {"shop_domain": SHOP}, signature="x").status_code == 401

    def test_shop_redact_removes_the_shop(self, client, shop, sent):
        post_webhook(client, "orders/paid", order_payload(financial_status="paid"))
        with session_scope() as db:
            assert db.get(Shop, SHOP) is not None
            assert db.scalars(select(Delivery)).first() is not None
        assert post_webhook(client, "shop/redact", {"shop_id": 1, "shop_domain": SHOP}).status_code == 200
        with session_scope() as db:
            assert db.get(Shop, SHOP) is None
            assert db.scalars(select(Delivery)).first() is None

    def test_customer_redact_removes_that_orders_rows(self, client, shop, sent):
        post_webhook(client, "orders/paid", order_payload(id=1, financial_status="paid"))
        post_webhook(client, "orders/paid", order_payload(id=2, name="#1002", financial_status="paid"))
        post_webhook(client, "customers/redact", {"shop_domain": SHOP, "orders_to_redact": [1]})
        with session_scope() as db:
            subjects = set(db.scalars(select(Delivery.subject)))
        assert subjects == {"2"}


class TestOAuthCallback:
    def test_bad_signature_is_403(self, client):
        params = {"shop": SHOP, "code": "abc", "state": "xyz", "timestamp": "1"}
        params["hmac"] = sign_query(params, "wrong")
        response = client.get("/auth/callback", params=params)
        assert response.status_code == 403

    def test_no_state_cookie_is_403(self, client):
        params = {"shop": SHOP, "code": "abc", "state": "xyz", "timestamp": "1"}
        params["hmac"] = sign_query(params)
        response = client.get("/auth/callback", params=params)
        assert response.status_code == 403

    def test_a_full_install(self, client, monkeypatch):
        from njiwa_shopify import shopify as shopify_module

        exchanged: list[str] = []

        async def fake_exchange(shop, code):
            exchanged.append(code)
            return shopify_module.TokenSet("shpat_new", "refresh_1", "read_orders", 3600, 86400 * 90)

        async def fake_fetch_shop(shop, token):
            assert token == "shpat_new"
            return {"name": "Example Shop", "domain": shop, "currency": "KES"}

        async def fake_ensure(shop, token, uri):
            assert uri == "https://njiwa-shopify.example.test/webhooks/shopify"
            return ["orders/create", "orders/paid"]

        monkeypatch.setattr(shopify_module, "exchange_code", fake_exchange)
        monkeypatch.setattr(shopify_module, "fetch_shop", fake_fetch_shop)
        monkeypatch.setattr(shopify_module, "ensure_webhooks", fake_ensure)

        begin = client.get("/auth", params={"shop": "example"}, follow_redirects=False)
        assert begin.status_code == 303
        location = begin.headers["location"]
        assert location.startswith(f"https://{SHOP}/admin/oauth/authorize?")
        assert "client_id=test-client-id" in location
        assert "scope=read_orders" in location
        state = location.split("state=")[1].split("&")[0]

        params = {"shop": SHOP, "code": "the-code", "state": state, "timestamp": "1"}
        params["hmac"] = sign_query(params)
        done = client.get("/auth/callback", params=params, follow_redirects=False)
        assert done.status_code == 303, done.text
        assert done.headers["location"].endswith("/settings")
        assert exchanged == ["the-code"]

        with session_scope() as db:
            row = db.get(Shop, SHOP)
            assert row is not None and row.is_installed
            assert row.access_token == "shpat_new"
            assert row.refresh_token == "refresh_1"
            assert row.token_expires_at is not None
            assert "orders/create" in row.webhooks_note

        # And the session cookie it set gets the merchant onto the settings page.
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Example Shop" in page.text

    def test_a_signed_app_link_signs_an_installed_shop_in(self, client, shop):
        from njiwa_shopify.db import utc_now

        params = {"shop": SHOP, "timestamp": str(int(utc_now().timestamp()))}
        params["hmac"] = sign_query(params)
        response = client.get("/", params=params, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].endswith("/settings")
        assert "njiwa_shopify_session" in response.headers.get("set-cookie", "")

    def test_a_stale_app_link_is_refused(self, client, shop):
        params = {"shop": SHOP, "timestamp": "1700000000"}
        params["hmac"] = sign_query(params)
        assert client.get("/", params=params, follow_redirects=False).status_code == 403

    def test_an_app_link_for_an_uninstalled_shop_starts_oauth(self, client):
        from njiwa_shopify.db import utc_now

        params = {"shop": SHOP, "timestamp": str(int(utc_now().timestamp()))}
        params["hmac"] = sign_query(params)
        response = client.get("/", params=params, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].endswith(f"/auth?shop={SHOP}")
