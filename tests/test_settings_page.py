"""The settings page: who gets in, what it saves, and what it refuses."""

from __future__ import annotations

from njiwa_shopify.db import Shop, session_scope
from njiwa_shopify.shop_settings import ShopSettings
from tests.conftest import SHOP, csrf, signed_in


def saved() -> ShopSettings:
    with session_scope() as db:
        return ShopSettings.from_json(db.get(Shop, SHOP).settings_json)


def form(**fields):
    base = {
        "csrf": csrf(),
        "enabled": "yes",
        "base_url": "https://njiwa.upeo.ai",
        "send_from": "",
        "admin_numbers": "254700000001",
        "event_paid": "yes",
        "template_paid": "Paid, {first_name}.",
    }
    base.update(fields)
    return base


class TestAccess:
    def test_without_a_session_you_get_the_install_page(self, client, shop):
        response = client.get("/settings")
        assert response.status_code == 401
        assert "not signed in" in response.text

    def test_with_a_session_you_get_the_page(self, client, shop):
        signed_in(client)
        response = client.get("/settings")
        assert response.status_code == 200
        assert "Send WhatsApp messages" in response.text
        assert "Tell me about new orders" in response.text
        assert "{first_name}" in response.text
        assert "sk_test_abc123" not in response.text  # the key is never echoed
        assert "A key ending <code>c123</code>" in response.text

    def test_a_session_for_an_uninstalled_shop_does_not_work(self, client):
        signed_in(client)
        assert client.get("/settings").status_code == 401

    def test_a_post_without_csrf_is_refused(self, client, shop):
        signed_in(client)
        response = client.post("/settings", data=form(csrf="nope"), follow_redirects=False)
        assert response.status_code == 403


class TestSave:
    def test_a_save_round_trips(self, client, shop):
        signed_in(client)
        response = client.post("/settings", data=form(), follow_redirects=False)
        assert response.status_code == 303, response.text
        after = saved()
        assert after.enabled
        assert after.api_key == "sk_test_abc123"  # blank field keeps the saved key
        assert after.event_on("paid") and not after.event_on("fulfilled")
        assert after.template_for("paid") == "Paid, {first_name}."
        assert after.template_for("fulfilled") == ""  # a box left empty is a decision
        assert after.admin_number_list() == ["254700000001"]

    def test_the_switch_can_be_turned_off_and_everything_stays(self, client, shop):
        signed_in(client)
        data = form()
        del data["enabled"]
        client.post("/settings", data=data, follow_redirects=False)
        after = saved()
        assert not after.enabled
        assert after.api_key == "sk_test_abc123"
        signed_in(client)
        assert "Sending is switched off" in client.get("/settings").text

    def test_a_new_key_is_saved_and_a_wrong_one_is_not(self, client, shop):
        signed_in(client)
        client.post("/settings", data=form(api_key="sk_live_new"), follow_redirects=False)
        assert saved().api_key == "sk_live_new"
        response = client.post("/settings", data=form(api_key="whsec_oops"), follow_redirects=False)
        assert response.status_code == 400
        assert "starts with sk_live_ or sk_test_" in response.text
        assert saved().api_key == "sk_live_new"

    def test_the_key_can_be_forgotten(self, client, shop):
        signed_in(client)
        client.post("/settings", data=form(forget_api_key="yes"), follow_redirects=False)
        assert saved().api_key == ""

    def test_a_group_address_is_refused(self, client, shop):
        signed_in(client)
        response = client.post(
            "/settings", data=form(admin_numbers="120363028712345678@g.us"), follow_redirects=False
        )
        assert response.status_code == 400
        assert "is not a phone number" in response.text
        assert saved().admin_number_list() == ["254700000001", "254700000002"]

    def test_send_from_must_be_international(self, client, shop):
        signed_in(client)
        response = client.post("/settings", data=form(send_from="0712345678"), follow_redirects=False)
        assert response.status_code == 400
        assert "full international form" in response.text
        client.post("/settings", data=form(send_from="+254 712 345 678"), follow_redirects=False)
        assert saved().send_from == "254712345678"

    def test_the_address_must_be_https(self, client, shop):
        signed_in(client)
        response = client.post("/settings", data=form(base_url="http://njiwa.local"), follow_redirects=False)
        assert response.status_code == 400
        assert "must start with https://" in response.text


class TestButtons:
    def test_send_test_refuses_a_group_and_needs_a_number(self, client, shop, sent):
        signed_in(client)
        response = client.post(
            "/settings/send-test", data={"csrf": csrf(), "to": "120363028712345678@g.us"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert sent == []
        page = client.get("/settings")
        assert "is not a phone number" in page.text

    def test_send_test_sends_one_fixed_message_and_reports(self, client, shop, sent):
        signed_in(client)
        client.post("/settings/send-test", data={"csrf": csrf(), "to": "+254 712 345 678"}, follow_redirects=False)
        assert len(sent) == 1
        assert sent[0]["to"] == "254712345678"
        assert sent[0]["key"] == ""  # pressing twice sends twice
        assert sent[0]["wait"] is True
        assert "Test message from Example Shop" in sent[0]["text"]
        page = client.get("/settings")
        assert "Sent to +254712345678 (msg_1)" in page.text
        assert "test key" in page.text

    def test_send_test_with_the_switch_off_fails_loudly(self, client, sent):
        from tests.conftest import install_shop

        install_shop(enabled=False)
        signed_in(client)
        client.post("/settings/send-test", data={"csrf": csrf(), "to": "254712345678"}, follow_redirects=False)
        assert sent == []
        assert "switched off" in client.get("/settings").text

    def test_test_connection_lists_numbers(self, client, shop, monkeypatch):
        from njiwa_shopify import client as njiwa_client

        async def fake_numbers(settings):
            return [{"id": "inst_1", "label": "Shop line", "msisdn": "254700000009", "status": "connected", "is_default": True}]

        monkeypatch.setattr(njiwa_client, "numbers", fake_numbers)
        signed_in(client)
        client.post("/settings/test-connection", data={"csrf": csrf()}, follow_redirects=False)
        page = client.get("/settings")
        assert "This is a test key" in page.text
        assert "+254700000009" in page.text
        assert "default" in page.text
