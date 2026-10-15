"""Nano Banana image generation via the Google Gemini API.

Uses the Gemini 2.5 Flash Image model (model id "gemini-2.5-flash-image")
through the public generativelanguage REST endpoint. Plain urllib, no
extra dependencies.

The model returns generated images as base64 inlineData inside the
candidate content parts. We extract the first image part we find.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from .errors import WaveCinemaError

MODEL_ID = "gemini-2.5-flash-image"
_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL_ID}:generateContent"
)
TIMEOUT_S = 180


class NanoBananaError(WaveCinemaError):
    """Image generation failed (network, API error, or no image returned)."""


def generate_image(prompt: str, api_key: str,
                   timeout: int = TIMEOUT_S) -> dict:
    """Generate one image from a text prompt.

    Returns {"image_bytes": bytes, "mime_type": str}.
    Raises NanoBananaError with a human-readable message on failure.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise NanoBananaError("prompt is empty")
    if not api_key:
        raise NanoBananaError("no Gemini API key — add one in Settings")

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    data = json.dumps(body).encode("utf-8")
    url = f"{_ENDPOINT}?key={api_key}"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        try:
            msg = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            msg = detail
        raise NanoBananaError(f"Gemini API error (HTTP {e.code}): {msg}")
    except urllib.error.URLError as e:
        raise NanoBananaError(f"could not reach the Gemini API: {e.reason}")
    except TimeoutError:
        raise NanoBananaError(
            f"generation timed out after {timeout}s — try again")
    except Exception as e:
        raise NanoBananaError(f"generation failed: {e}")

    # Walk candidates -> content -> parts, take the first inlineData image.
    for cand in payload.get("candidates", []) or []:
        content = cand.get("content") or {}
        for part in content.get("parts", []) or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    raw = base64.b64decode(inline["data"])
                except Exception:
                    continue
                mime = inline.get("mimeType", "image/png")
                if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
                    return {"image_bytes": raw, "mime_type": mime}
    # No image found — surface any text the model returned for debugging.
    texts = []
    for cand in payload.get("candidates", []) or []:
        for part in (cand.get("content") or {}).get("parts", []) or []:
            if part.get("text"):
                texts.append(part["text"][:200])
    hint = f" model said: {' '.join(texts)}"[:300] if texts else ""
    raise NanoBananaError("the model returned no image." + hint)


ART_STYLES = {
    "anime": "anime style, clean cel shading",
    "photorealistic": "photorealistic, cinematic photography",
    "3d": "3D render, stylized Pixar-like quality",
    "comic": "comic book style, bold inks",
    "pixel": "pixel art style",
}


def character_sheet_prompt(name: str, description: str,
                           style: str = "anime") -> str:
    """Build the Nano Banana prompt for a character reference sheet."""
    style_phrase = ART_STYLES.get(style, ART_STYLES["anime"])
    desc = (description or "").strip()
    return (
        f"Character reference model sheet, {style_phrase}, for a character "
        f"named {name}. {desc}\n\n"
        "Layout: ONE single square image on a plain light-grey background. "
        "TOP ROW: three full-body turnaround views side by side — FRONT VIEW, "
        "SIDE VIEW, BACK VIEW — same outfit, same proportions, neutral pose, "
        "neutral expression. BOTTOM ROW: four head-and-shoulders close-ups "
        "showing expressions: neutral, happy, angry, sad. "
        "The character design must be perfectly consistent across every view. "
        "Clean model-sheet presentation, no watermark, no text labels."
    )


def background_prompt(description: str) -> str:
    """Build the Nano Banana prompt for an environment backplate."""
    desc = (description or "").strip()
    return (
        f"Environment concept art, cinematic backplate, no characters, no "
        f"people, no text, no watermark: {desc}\n\n"
        "Wide establishing shot, rich detail, atmospheric cinematic lighting, "
        "16:9 composition."
    )
