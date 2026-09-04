"""Settings for the service itself. Everything comes from the environment.

These are the operator's settings: who this app is to Shopify and where it can
be reached. What a shop wants sent, and with which Njiwa key, is the merchant's
business, lives per shop in the database, and is edited on the settings page.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The Admin API version named on every call. Shopify releases one a quarter and
# supports each for a year. A request naming a retired version is answered by
# the oldest version still supported, with a header saying so, rather than
# refused, so nothing breaks on the day this goes stale; it is pinned so that
# moving forward is a deliberate edit here and not a surprise.
SHOPIFY_API_VERSION = "2026-07"

# Everything this app asks a shop for. read_orders covers every order and
# refund webhook it subscribes to, and fetching an order when a refund comes in.
# It asks for nothing it does not use, because the scope list is the first
# thing a merchant reads on the install screen.
SHOPIFY_SCOPES = "read_orders"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    environment: str = Field(default="development", alias="NJIWA_SHOPIFY_ENV")

    # From the app's page in the Shopify Partner Dashboard. The key is the
    # client id and is public. The secret signs every OAuth callback and every
    # webhook Shopify sends, and it is the one value that must never leave
    # this server.
    shopify_api_key: str = Field(alias="SHOPIFY_API_KEY")
    shopify_api_secret: str = Field(alias="SHOPIFY_API_SECRET")

    # Where Shopify can reach this service: a public https address, including
    # any path prefix it is mounted under. The OAuth redirect and every webhook
    # subscription are built from it, so it must match what the Partner
    # Dashboard has to the character.
    app_url: str = Field(alias="NJIWA_SHOPIFY_APP_URL")

    database_url: str = Field(
        default="sqlite:///./njiwa-shopify.db", alias="NJIWA_SHOPIFY_DATABASE_URL"
    )

    # Signs the browser cookies. Left empty, the Shopify secret is used under
    # a separate salt, which is enough for one process on one host.
    session_secret_raw: str = Field(default="", alias="NJIWA_SHOPIFY_SESSION_SECRET")

    log_level: str = Field(default="INFO", alias="NJIWA_SHOPIFY_LOG_LEVEL")

    @field_validator("app_url")
    @classmethod
    def _tidy_app_url(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def session_secret(self) -> str:
        return self.session_secret_raw or self.shopify_api_secret

    @property
    def redirect_uri(self) -> str:
        return f"{self.app_url}/auth/callback"

    @property
    def webhook_uri(self) -> str:
        return f"{self.app_url}/webhooks/shopify"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
