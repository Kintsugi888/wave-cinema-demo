"""Wave Cinema Cloud lane for Nano Banana image generation.

Mirrors the Higgsfield Cloud lane pattern (see providers.py): the
server-side key comes from the WAVECINEMA_GEMINI_API_KEY env var and is
only active when WAVECINEMA_GEMINI_CLOUD_ENABLED=1. The key is never read
from any user-supplied store — that path is the BYOK lane (geminikey.py).

Cost to the service is ~$0.034/image; users pay IMAGE_GEN_CREDITS
(quote.py) from their Wave Cinema credit balance. The margin is the
business.
"""
from __future__ import annotations

import os


def cloud_available() -> bool:
    """True when the Cloud image lane is unlocked and has a key."""
    if os.environ.get("WAVECINEMA_GEMINI_CLOUD_ENABLED") != "1":
        return False
    return bool((os.environ.get("WAVECINEMA_GEMINI_API_KEY") or "").strip())


def get_cloud_key() -> str | None:
    """Return the server-side Gemini key, or None when unavailable."""
    if not cloud_available():
        return None
    return (os.environ.get("WAVECINEMA_GEMINI_API_KEY") or "").strip()


def cloud_status() -> dict:
    """Display-safe status for the UI. Never exposes the key."""
    return {"available": cloud_available()}
