"""Every route, on one router, so another application can mount the lot."""

from fastapi import APIRouter

from njiwa_shopify.routers import auth, health, settings, webhooks

router = APIRouter()
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(settings.router)
router.include_router(webhooks.router)

__all__ = ["router"]
