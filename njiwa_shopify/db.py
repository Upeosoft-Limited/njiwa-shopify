"""Storage. SQLite through SQLAlchemy, and three small tables.

SQLite because a first version of this runs as one process on one host, and a
file it can open is one fewer service to keep alive. The URL is a setting, so
the same models run on Postgres the day that stops being true.

What is kept is deliberately little. A shop, its Shopify token and its
settings. The id of every webhook seen, so a redelivery is recognised. And one
row per message this app decided to send, which is both the once-per-order
marker the spec asks for and the log the merchant reads. The message text is
held on that row only until it has gone: Njiwa keeps every message and its
fate, and a second copy here would be a second thing to keep in step.

Every timestamp is naive UTC. SQLite has no timezone type, and a column that
hands back naive datetimes to code holding aware ones raises the moment the
two are compared, so this app never holds an aware one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from njiwa_shopify.config import get_settings


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Shop(Base):
    __tablename__ = "shops"

    # The myshopify domain, which Shopify puts on every webhook and every
    # OAuth callback. The one identifier a shop never changes.
    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    currency: Mapped[str] = mapped_column(String(8), default="")

    # The offline access token, and what refreshes it. All of it is cleared
    # the moment the shop uninstalls; Shopify has revoked it by then anyway.
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str] = mapped_column(String(255), default="")

    # Everything the merchant chose on the settings page, as one JSON
    # document. shop_settings.py owns its shape.
    settings_json: Mapped[str] = mapped_column(Text, default="{}")

    # What happened the last time webhooks were registered, in a sentence the
    # settings page can show.
    webhooks_note: Mapped[str] = mapped_column(Text, default="")

    installed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def is_installed(self) -> bool:
        return bool(self.access_token) and self.uninstalled_at is None

    @property
    def can_refresh(self) -> bool:
        """Whether the Admin API can still be reached without the merchant.

        The refresh token lasts 90 days from the last refresh. Past that the
        merchant has to open the app again, which runs OAuth again.
        """
        if not self.refresh_token:
            return False
        if self.refresh_token_expires_at is None:
            return True
        return self.refresh_token_expires_at > utc_now()


class WebhookReceipt(Base):
    """Every webhook id Shopify has delivered here.

    Shopify retries anything not answered 200 within five seconds and can
    deliver the same webhook more than once. The id is the same each time, so
    a second arrival is recognised before anything is decided.
    """

    __tablename__ = "webhook_receipts"

    webhook_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    shop_domain: Mapped[str] = mapped_column(String(255), index=True)
    topic: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Delivery(Base):
    """One message this app decided to send, and what became of it.

    The unique constraint is the once-per-order marker: one row per shop,
    event, subject and recipient, and an attempt to insert a second is how
    "already sent" is discovered. The subject is the order id, or for a refund
    the order and refund ids together, because a second partial refund is a
    second real event.

    Once sent, the recipient survives only as a hash and its last digits. The
    row has to be unique per recipient, and the merchant wants to see roughly
    where a message went, and neither needs the whole number sitting in this
    file for ever.
    """

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint(
            "shop_domain", "event", "subject", "recipient_hash", name="uq_delivery_once"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_domain: Mapped[str] = mapped_column(String(255), index=True)
    event: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(64))
    order_name: Mapped[str] = mapped_column(String(32), default="")
    # sha256, hex, so 64 characters. Declared at its real width because
    # SQLite ignores the length and Postgres refuses the insert.
    recipient_hash: Mapped[str] = mapped_column(String(64))
    recipient_hint: Mapped[str] = mapped_column(String(16), default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")

    # queued, sent, failed, or no_number. The full recipient number and the
    # text are set while the row is queued and cleared the moment it leaves
    # that state; after that the hash and the hint above are all that is
    # left of where it went.
    status: Mapped[str] = mapped_column(String(16), default="queued")
    recipient: Mapped[str] = mapped_column(String(32), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[str] = mapped_column(String(64), default="")
    # Why it failed, or a note about how it was sent, in a sentence the
    # merchant reads on the settings page.
    detail: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict = {}
        if url.startswith("sqlite"):
            # One process, several threads: FastAPI runs a sync endpoint on a
            # threadpool, and SQLite objects to a connection crossing threads
            # unless told not to. The timeout is how long a writer waits for
            # another writer before giving up.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(connection, _record) -> None:  # type: ignore[no-untyped-def]
                # Write-ahead logging lets a webhook be recorded while the
                # settings page is being read, instead of one waiting on the
                # other.
                connection.execute("PRAGMA journal_mode=WAL")

        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """Forget the engine, so the next call builds one from current settings.
    Tests use it to point each run at its own file."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
