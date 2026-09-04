from fastapi import APIRouter

from njiwa_shopify import __version__

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "service": "njiwa-shopify", "version": __version__}
