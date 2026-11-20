"""Turn exceptions into short, actionable messages for the UI."""
from __future__ import annotations

from typing import Any

import httpx

from . import discogs as dg
from . import vision
from .config import Settings


def _err(title: str, hint: str, detail: str, *, scope: str = "item") -> dict[str, Any]:
    # scope "provider"/"discogs": the problem affects every record, not just this one
    return {"title": title, "hint": hint, "detail": detail, "scope": scope}


def describe(exc: BaseException, settings: Settings, provider: str | None = None) -> dict[str, Any]:
    detail = f"{type(exc).__name__}: {exc}"[:2000]
    msg = str(exc).lower()

    # ---- Anthropic -----------------------------------------------------------
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        anthropic = None  # type: ignore[assignment]
    if anthropic is not None:
        if isinstance(exc, anthropic.APIConnectionError):
            return _err("Can't reach the Anthropic API",
                        "Check the server's internet connection, then retry.", detail, scope="provider")
        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            if "credit balance" in msg:
                return _err("Your Anthropic account is out of credit",
                            "Top up at console.anthropic.com → Plans & Billing, or switch this record to another provider.",
                            detail, scope="provider")
            if status == 401:
                return _err("The Anthropic API key was rejected",
                            "Check ANTHROPIC_API_KEY in your .env and restart the container.", detail, scope="provider")
            if status == 403:
                return _err("The Anthropic API key isn't allowed to do this",
                            "Check the key's permissions in the Anthropic console.", detail, scope="provider")
            if status == 404:
                return _err(f"Claude model not found: {settings.claude_model}",
                            "Set CLAUDE_MODEL in your .env to a model your key can use.", detail, scope="provider")
            if status == 413:
                return _err("The photos are too large for the API",
                            "Lower MAX_IMAGE_PX in your .env, or upload fewer photos for this record.", detail)
            if status == 429:
                return _err("Anthropic rate limit reached", "Wait a minute, then retry.", detail)
            if status >= 500:
                return _err("Anthropic is temporarily unavailable", "Retry in a moment.", detail)
            return _err(f"Anthropic API error ({status})", "Retry, or switch this record to another provider.", detail)

    # ---- OpenAI --------------------------------------------------------------
    if isinstance(exc, vision.OpenAIError):
        code = (exc.code or "").lower()
        if exc.status == 429 and "quota" in code + msg:
            return _err("Your OpenAI account is out of credit",
                        "Add credit at platform.openai.com → Billing, or switch this record to another provider.",
                        detail, scope="provider")
        if exc.status == 401:
            return _err("The OpenAI API key was rejected",
                        "Check OPENAI_API_KEY in your .env and restart the container.", detail, scope="provider")
        if exc.status == 403:
            return _err("The OpenAI API key isn't allowed to use this model",
                        "Check your project's model access, or set OPENAI_MODEL to another model.",
                        detail, scope="provider")
        if exc.status == 404 or "model" in code:
            return _err(f"OpenAI model not found: {settings.openai_model}",
                        "Set OPENAI_MODEL in your .env to a vision model your key can use.", detail, scope="provider")
        if exc.status == 429:
            return _err("OpenAI rate limit reached", "Wait a minute, then retry.", detail)
        if exc.status >= 500:
            return _err("OpenAI is temporarily unavailable", "Retry in a moment.", detail)
        return _err(f"OpenAI API error ({exc.status})", exc.message[:200], detail)
    if provider == "openai" and isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return _err("Can't reach the OpenAI API",
                    f"Check the server's internet connection and OPENAI_BASE_URL ({settings.openai_base_url}).",
                    detail, scope="provider")

    # ---- Ollama --------------------------------------------------------------
    if provider == "ollama" or (isinstance(exc, httpx.HTTPError) and settings.ollama_url
                                and settings.ollama_url in str(getattr(getattr(exc, "request", None), "url", ""))):
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return _err(f"Can't reach Ollama at {settings.ollama_url}",
                        "Check that Ollama is running and OLLAMA_URL is reachable from the container.",
                        detail, scope="provider")
        if isinstance(exc, httpx.ReadTimeout):
            return _err("Ollama took too long to answer",
                        "The model may still be loading. Retry, or use a smaller vision model.", detail)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return _err(f"Ollama model not found: {settings.ollama_model}",
                        f"Run `ollama pull {settings.ollama_model}` on the Ollama host, or change OLLAMA_MODEL.",
                        detail, scope="provider")

    # ---- Discogs -------------------------------------------------------------
    if isinstance(exc, dg.DiscogsError):
        if "not set" in msg:
            return _err("No Discogs token configured",
                        "Set DISCOGS_TOKEN in your .env (discogs.com/settings/developers).", detail, scope="discogs")
        if exc.status == 401:
            return _err("Discogs rejected the token",
                        "Generate a new token at discogs.com/settings/developers and update DISCOGS_TOKEN.",
                        detail, scope="discogs")
        if exc.status == 429:
            return _err("Discogs rate limit reached", "Wait a minute, then retry.", detail)
        return _err("Discogs request failed", "Retry in a moment.", detail)
    if isinstance(exc, httpx.HTTPError):
        return _err("Network error", "Check the server's internet connection, then retry.", detail)

    # ---- config / model output -----------------------------------------------
    if isinstance(exc, RuntimeError) and "is not set" in msg:
        return _err("Vision provider not configured",
                    f"{exc}. Add it to your .env, or pick the other provider for this record.", detail,
                    scope="provider")
    if isinstance(exc, ValueError) and ("json" in msg or "expecting" in msg):
        return _err("The model's answer couldn't be read",
                    "Retry, or read this record with the other provider.", detail)
    if isinstance(exc, OSError) and "cannot identify image" in msg:
        return _err("One of the photos couldn't be opened",
                    "Delete this record and upload the photos again (JPEG/PNG/HEIC).", detail)

    return _err("Something went wrong", "Retry. If it keeps happening, check the container logs.", detail)
