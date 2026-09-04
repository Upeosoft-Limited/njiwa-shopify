"""Njiwa for Shopify.

WhatsApp your customers when their order is paid, sent, cancelled or refunded,
and get a message yourself when one comes in.

Shopify has no plugin folder to drop a file into. An app is a web service the
merchant installs, and Shopify then POSTs order webhooks to it. So this package
is that service: a small FastAPI application that runs on its own, or can be
mounted inside another one.
"""

__version__ = "0.1.0"
