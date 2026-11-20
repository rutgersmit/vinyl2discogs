"""Read record photos with a vision model: Claude (Anthropic API) or a local model via Ollama."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image, ImageOps

try:  # iPhone photos (HEIC) dropped in the inbox folder
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover
    pass

from .config import Settings

FIELDS = (
    "artist", "title", "label", "catno", "country", "year",
    "matrix_a", "matrix_b", "barcode", "other_identifiers", "notes",
)

PROMPT = """You are an expert in identifying vinyl records (LPs, 7", 12") for cataloguing on Discogs.
You get one or more photos of the SAME record: typically the front cover, the centre label, and/or the
runout groove (matrix area). Photos may be unlabeled; figure out what each shows.

Extract exactly what is printed or etched. Do not guess pressings. Use null for anything not visible.

Return ONLY a JSON object with these keys:
{
  "artist": string|null,              // as printed; "Various" for compilations
  "title": string|null,               // album/release title
  "label": string|null,               // record label name on the centre label (not the distributor if both clear)
  "catno": string|null,               // catalogue number exactly as printed, e.g. "6399 016", "SHVL 804", "LSP-4445"
  "country": string|null,             // pressing country if indicated ("Made in Holland" -> "Netherlands", "Made in England" -> "UK")
  "year": integer|null,               // ONLY if a year is printed (℗/© year). Otherwise null
  "matrix_a": string|null,            // side A/1 runout text, transcribed exactly incl. stamps/initials, separate parts with spaces
  "matrix_b": string|null,            // side B/2 runout text if visible
  "barcode": string|null,
  "other_identifiers": string|null,   // rights society (BIEM/STEMRA/GEMA...), label code (LC xxxx), price codes etc.
  "notes": string|null,               // anything useful to tell pressings apart: label colour/design variant, "Stereo", club edition, promo stamp
  "confidence": "high"|"medium"|"low"
}
"""


def _image_to_b64_jpeg(path: Path, max_px: int) -> str:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Tolerate code fences or prose around the JSON
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in model response: {text[:200]!r}")
    data = json.loads(m.group(0))
    return normalize_extracted(data)


def normalize_extracted(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in FIELDS:
        v = data.get(k)
        if isinstance(v, str):
            v = v.strip() or None
            if v and v.lower() in ("null", "none", "unknown", "n/a"):
                v = None
        out[k] = v
    y = out.get("year")
    try:
        out["year"] = int(y) if y not in (None, "") else None
    except (TypeError, ValueError):
        out["year"] = None
    out["confidence"] = data.get("confidence") if data.get("confidence") in ("high", "medium", "low") else "low"
    return out


def _user_text(hint: str, roles: list[str]) -> str:
    parts = [PROMPT]
    if any(roles):
        parts.append("Photo roles as given by the user (in order): " + ", ".join(r or "unknown" for r in roles))
    if hint:
        parts.append(
            "User-provided hints (trust these over unclear photo text, but still transcribe what you see):\n" + hint
        )
    return "\n\n".join(parts)


class VisionProvider(Protocol):
    name: str

    async def extract(self, images: list[Path], roles: list[str], hint: str) -> dict[str, Any]: ...


class ClaudeVision:
    name = "claude"

    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.claude_model
        self._max_px = settings.max_image_px

    async def extract(self, images: list[Path], roles: list[str], hint: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for p in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": _image_to_b64_jpeg(p, self._max_px)},
            })
        content.append({"type": "text", "text": _user_text(hint, roles)})
        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _parse_json(text)


class OllamaVision:
    name = "ollama"

    def __init__(self, settings: Settings):
        if not settings.ollama_url:
            raise RuntimeError("OLLAMA_URL is not set")
        self._url = settings.ollama_url.rstrip("/")
        self._model = settings.ollama_model
        self._max_px = settings.max_image_px

    async def extract(self, images: list[Path], roles: list[str], hint: str) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [{
                "role": "user",
                "content": _user_text(hint, roles),
                "images": [_image_to_b64_jpeg(p, self._max_px) for p in images],
            }],
        }
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{self._url}/api/chat", json=payload)
            r.raise_for_status()
            return _parse_json(r.json()["message"]["content"])


def get_provider(name: str, settings: Settings) -> VisionProvider:
    name = (name or settings.vision_provider).lower()
    if name == "claude":
        return ClaudeVision(settings)
    if name == "ollama":
        return OllamaVision(settings)
    raise ValueError(f"Unknown vision provider: {name}")


def available_providers(settings: Settings) -> list[str]:
    out = []
    if settings.anthropic_api_key:
        out.append("claude")
    if settings.ollama_url:
        out.append("ollama")
    return out
