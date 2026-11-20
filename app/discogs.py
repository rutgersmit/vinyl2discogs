"""Discogs API client plus the search and scoring logic used to find a pressing."""
from __future__ import annotations

import asyncio
import re
import time
from difflib import SequenceMatcher
from typing import Any

import httpx

from .config import Settings

API = "https://api.discogs.com"


class DiscogsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DiscogsClient:
    """Async client with simple throttling (Discogs allows 60 req/min with a token)."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 min_interval: float = 1.05):
        if not settings.discogs_token:
            raise DiscogsError("DISCOGS_TOKEN is not set")
        self._s = settings
        self._client = httpx.AsyncClient(
            base_url=API,
            timeout=30,
            transport=transport,
            headers={
                "Authorization": f"Discogs token={settings.discogs_token}",
                "User-Agent": settings.user_agent,
                "Accept": "application/vnd.discogs.v2.discogs+json",
            },
        )
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._base_interval = min_interval
        self._min_interval = min_interval
        self._username: str | None = settings.discogs_username or None
        self._release_cache: dict[int, dict] = {}
        self._fields_cache: list[dict] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, url: str, **kw: Any) -> Any:
        for attempt in range(4):
            async with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                r = await self._client.request(method, url, **kw)
                self._last = time.monotonic()
                remaining = r.headers.get("X-Discogs-Ratelimit-Remaining")
                if remaining is not None and remaining.isdigit() and int(remaining) < 5:
                    self._min_interval = max(3.0, self._base_interval)  # back off until the window resets
                else:
                    self._min_interval = self._base_interval
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("Retry-After", 30)) * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise DiscogsError(f"{method} {url} -> {r.status_code}: {r.text[:300]}", r.status_code)
            return r.json() if r.content else {}
        raise DiscogsError(f"{method} {url}: rate limited, gave up after retries", 429)

    # ---- account / collection -------------------------------------------------
    async def username(self) -> str:
        if not self._username:
            self._username = (await self._req("GET", "/oauth/identity"))["username"]
        return self._username

    async def folders(self) -> list[dict]:
        u = await self.username()
        data = await self._req("GET", f"/users/{u}/collection/folders")
        return [{"id": f["id"], "name": f["name"], "count": f["count"]} for f in data["folders"] if f["id"] != 0]

    async def fields(self) -> list[dict]:
        """Collection fields (Media Condition, Sleeve Condition, Notes, custom fields)."""
        if self._fields_cache is None:
            u = await self.username()
            data = await self._req("GET", f"/users/{u}/collection/fields")
            self._fields_cache = data.get("fields", [])
        return self._fields_cache

    async def instances_of(self, release_id: int) -> list[dict]:
        u = await self.username()
        data = await self._req("GET", f"/users/{u}/collection/releases/{release_id}")
        return data.get("releases", [])

    async def add_to_collection(self, release_id: int, folder_id: int) -> int:
        u = await self.username()
        data = await self._req("POST", f"/users/{u}/collection/folders/{folder_id}/releases/{release_id}")
        return int(data["instance_id"])

    async def set_field(self, folder_id: int, release_id: int, instance_id: int, field_id: int, value: str) -> None:
        u = await self.username()
        await self._req(
            "POST",
            f"/users/{u}/collection/folders/{folder_id}/releases/{release_id}/instances/{instance_id}/fields/{field_id}",
            json={"value": value},
        )

    # ---- database ------------------------------------------------------------
    async def search(self, **params: Any) -> list[dict]:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params.setdefault("type", "release")
        params.setdefault("per_page", 25)
        data = await self._req("GET", "/database/search", params=params)
        return data.get("results", [])

    async def release(self, release_id: int) -> dict:
        if release_id not in self._release_cache:
            self._release_cache[release_id] = await self._req("GET", f"/releases/{release_id}")
        return self._release_cache[release_id]


# ---- normalisation & scoring --------------------------------------------------

def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def words(s: str | None) -> str:
    s = re.sub(r"\(\d+\)", "", s or "")  # Discogs disambiguation suffix "Artist (2)"
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def sim(a: str | None, b: str | None) -> float:
    a, b = words(a), words(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def matrix_tokens(s: str | None) -> set[str]:
    return {norm(t) for t in re.split(r"[\s/|,;:·•]+", s or "") if len(norm(t)) >= 2}


def matrix_overlap(extracted: list[str], release_matrices: list[str]) -> float:
    """Best overlap (0..1) between an extracted runout and any of a release's matrix strings."""
    best = 0.0
    for ex in extracted:
        et = matrix_tokens(ex)
        if not et:
            continue
        for rm in release_matrices:
            rt = matrix_tokens(rm)
            if not rt:
                continue
            exact = len(et & rt)
            # partial token matches ("YAX1234-2" vs "YAX1234") count for half
            partial = sum(0.5 for t in et - rt if any(len(t) >= 4 and (t in r or r in t) for r in rt))
            best = max(best, min(1.0, (exact + partial) / len(et)))
    return best


def split_title(t: str) -> tuple[str, str]:
    if " - " in t:
        a, b = t.split(" - ", 1)
        return a.strip(), b.strip()
    return "", t.strip()


def score_search_result(r: dict, ex: dict) -> tuple[float, list[str]]:
    score, why = 0.0, []
    artist, title = split_title(r.get("title", ""))
    cat_ex = norm(ex.get("catno"))
    cats = [norm(c) for c in re.split(r",", r.get("catno") or "") if norm(c)]
    if cat_ex and cats:
        if cat_ex in cats:
            score += 40; why.append("catno exact")
        elif any(len(cat_ex) >= 4 and (cat_ex in c or c in cat_ex) for c in cats):
            score += 25; why.append("catno partial")
    if ex.get("label"):
        ls = max((sim(ex["label"], l) for l in r.get("label", [])), default=0)
        if ls >= 0.8:
            score += 15; why.append("label")
    if ex.get("artist"):
        s = sim(ex["artist"], artist)
        score += 15 * s
        if s >= 0.85:
            why.append("artist")
    if ex.get("title"):
        s = sim(ex["title"], title)
        score += 15 * s
        if s >= 0.85:
            why.append("title")
    if ex.get("country") and sim(ex["country"], r.get("country")) >= 0.9:
        score += 8; why.append("country")
    if ex.get("year") and str(ex["year"]) == str(r.get("year")):
        score += 5; why.append("year")
    if "Vinyl" in (r.get("format") or []):
        score += 2
    return score, why


def release_matrices(rel: dict) -> list[str]:
    return [
        i.get("value", "") for i in rel.get("identifiers", [])
        if i.get("type", "").lower().startswith("matrix")
    ]


def to_candidate(r: dict, score: float, why: list[str], rel: dict | None = None) -> dict:
    artist, title = split_title(r.get("title", ""))
    cand = {
        "id": r["id"],
        "artist": artist,
        "title": title,
        "year": r.get("year"),
        "country": r.get("country"),
        "labels": r.get("label", [])[:3],
        "catno": r.get("catno"),
        "formats": ", ".join(dict.fromkeys(r.get("format") or [])),
        "thumb": r.get("thumb") or r.get("cover_image"),
        "url": f"https://www.discogs.com/release/{r['id']}",
        "score": round(score, 1),
        "reasons": why,
        "matrix": [],
    }
    if rel:
        cand["matrix"] = release_matrices(rel)[:6]
        if rel.get("released"):
            cand["year"] = rel.get("released")
        img = (rel.get("images") or [{}])[0]
        cand["thumb"] = cand["thumb"] or img.get("uri150")
    return cand


async def find_candidates(client: DiscogsClient, ex: dict, *, detail_lookups: int = 8, limit: int = 12) -> list[dict]:
    """Search with a few strategies, dedupe, score, then refine the top results by matrix comparison."""
    artist, title, label, catno = ex.get("artist"), ex.get("title"), ex.get("label"), ex.get("catno")
    if artist and artist.lower() in ("various", "various artists"):
        artist = "Various"

    strategies: list[dict] = []
    if catno and label:
        strategies.append({"catno": catno, "label": label, "format": "Vinyl"})
    if catno:
        strategies.append({"catno": catno, "format": "Vinyl"})
    if artist and title:
        strategies.append({"artist": artist, "release_title": title, "format": "Vinyl"})
    if ex.get("barcode"):
        strategies.append({"barcode": ex["barcode"]})
    q = " ".join(x for x in (artist, title) if x)
    if q:
        strategies.append({"q": q, "format": "Vinyl"})

    seen: dict[int, dict] = {}
    for params in strategies:
        for r in await client.search(**params):
            seen.setdefault(r["id"], r)
        # Enough strong hits? Stop searching (saves rate limit).
        strong = sum(1 for r in seen.values() if score_search_result(r, ex)[0] >= 60)
        if strong >= 3 or len(seen) >= 60:
            break

    scored = sorted(
        ((r, *score_search_result(r, ex)) for r in seen.values()), key=lambda t: t[1], reverse=True
    )[: max(limit, detail_lookups)]

    extracted_matrix = [m for m in (ex.get("matrix_a"), ex.get("matrix_b")) if m]
    out: list[dict] = []
    for i, (r, score, why) in enumerate(scored):
        rel = None
        if i < detail_lookups:
            try:
                rel = await client.release(r["id"])
            except DiscogsError:
                rel = None
        if rel and extracted_matrix:
            mo = matrix_overlap(extracted_matrix, release_matrices(rel))
            if mo > 0:
                score += 40 * mo
                why = [*why, f"matrix {int(mo * 100)}%"]
        out.append(to_candidate(r, score, why, rel))

    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:limit]


async def candidate_from_release_id(client: DiscogsClient, release_id: int) -> dict:
    rel = await client.release(release_id)
    fake_search = {
        "id": rel["id"],
        "title": f"{', '.join(a.get('name', '') for a in rel.get('artists', []))} - {rel.get('title', '')}",
        "year": rel.get("year"),
        "country": rel.get("country"),
        "label": [l.get("name") for l in rel.get("labels", [])],
        "catno": ", ".join(l.get("catno", "") for l in rel.get("labels", [])),
        "format": [f.get("name") for f in rel.get("formats", [])]
                  + [d for f in rel.get("formats", []) for d in f.get("descriptions", [])],
        "thumb": rel.get("thumb"),
    }
    return to_candidate(fake_search, 0, ["manual"], rel)


def parse_release_ref(ref: str) -> int | None:
    """Accept a release ID, '[r123]' or a Discogs URL."""
    ref = ref.strip()
    m = re.search(r"/release/(\d+)", ref) or re.fullmatch(r"\[?r?(\d+)\]?", ref)
    return int(m.group(1)) if m else None
