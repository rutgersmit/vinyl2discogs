"""vinyl2discogs — photos of an LP -> identification -> Discogs collection."""
from __future__ import annotations

import asyncio
import base64
import errno
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import discogs as dg
from . import errors, vision
from .config import Settings, get_settings
from .db import DB

log = logging.getLogger("vinyl2discogs")
STATIC = Path(__file__).parent / "static"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ROLE_WORDS = {"cover": "cover", "front": "cover", "label": "label", "runout": "runout",
              "matrix": "runout", "back": "back", "rear": "back"}


# ---- request models -----------------------------------------------------------
class ReprocessIn(BaseModel):
    provider: str | None = None
    hint: str | None = None
    extracted: dict[str, Any] | None = None  # edited fields -> search again without re-reading photos


class SearchIn(BaseModel):
    q: str


class AddIn(BaseModel):
    release_id: int
    folder_id: int | None = None
    media_condition: str | None = None
    sleeve_condition: str | None = None
    notes: str | None = None
    force: bool = False


class StatusIn(BaseModel):
    status: str


# ---- startup checks -------------------------------------------------------------
def data_dir_error(settings: Settings, err: OSError) -> str:
    d = settings.data_dir
    existing = next((p for p in (d, *d.parents) if p.exists()), None)
    if existing is None:
        owner = "unknown"
    else:
        st = existing.stat()
        owner = f"{st.st_uid}:{st.st_gid}" + ("" if existing == d else f" (nearest existing folder: {existing})")
    return (
        f"Cannot write to the data folder {d} ({err.strerror}). "
        f"The app runs as {os.getuid()}:{os.getgid()}, the folder is owned by {owner}.\n"
        "  Docker fixes:\n"
        "  - make sure you run the latest image: docker compose pull && docker compose up -d --force-recreate\n"
        "  - don't set `user:` in docker-compose.yml; use PUID/PGID instead (the entrypoint needs to start as root)\n"
        "  - SELinux host (Fedora/RHEL): mount the volume as ./data:/data:z\n"
        "  - NFS share with root_squash: on the host, run chown -R 1000:1000 data (or your PUID:PGID)"
    )


def ensure_data_dir(settings: Settings) -> None:
    """Create the data folders and verify they are writable; exit with a clear message if not."""
    try:
        for d in (settings.photos_dir, settings.inbox_dir):
            d.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write-test"
        probe.touch()
        probe.unlink()
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        log.error(data_dir_error(settings, e))
        raise SystemExit(1) from None


# ---- app factory --------------------------------------------------------------
def create_app(
    settings: Settings | None = None,
    discogs_factory: Callable[[Settings], dg.DiscogsClient] | None = None,
    vision_factory: Callable[[str, Settings], vision.VisionProvider] | None = None,
    run_worker: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    ensure_data_dir(settings)
    db = DB(settings.db_path)
    discogs_factory = discogs_factory or dg.DiscogsClient
    vision_factory = vision_factory or vision.get_provider
    state: dict[str, Any] = {"discogs": None, "wake": asyncio.Event()}

    def client() -> dg.DiscogsClient:
        if state["discogs"] is None:
            try:
                state["discogs"] = discogs_factory(settings)
            except dg.DiscogsError as e:
                raise HTTPException(503, str(e)) from e
        return state["discogs"]

    # ---- rare finds -------------------------------------------------------------
    def rare_info(cand: dict) -> dict:
        r = cand.get("rarity") or {}
        return {"release_id": cand["id"], "artist": cand.get("artist"), "title": cand.get("title"),
                "url": cand.get("url"), "reasons": r.get("reasons", []), "have": r.get("have"),
                "want": r.get("want"), "lowest_price": r.get("lowest_price"), "currency": settings.currency}

    async def notify_rare(item_id: int, info: dict) -> None:
        if not settings.rare_webhook_url:
            return
        msg = f"Rare find: {info['artist']} – {info['title']} ({'; '.join(info['reasons'])})"
        payload = {"event": "rare_find", "message": msg, "text": msg, "item_id": item_id, **info}
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                (await h.post(settings.rare_webhook_url, json=payload)).raise_for_status()
        except httpx.HTTPError as e:  # an alert must never fail the record itself
            log.warning("Rare-find webhook failed for item %s: %s", item_id, e)

    async def update_rare(item: dict, candidates: list[dict], release_id: int | None = None) -> dict | None:
        """Flag the item when its best match (or the added release) is rare; alert once per release."""
        if release_id:
            pick = next((c for c in candidates if c["id"] == release_id and c.get("rarity")), None)
            if pick is None:  # picked via manual search: one lookup, usually served from the release cache
                try:
                    pick = await dg.candidate_from_release_id(client(), release_id)
                except (dg.DiscogsError, httpx.HTTPError) as e:  # optional enrichment: never block the add
                    log.warning("Rarity lookup for release %s failed: %s", release_id, e)
                    prev = item.get("rare")
                    keep = prev if prev and prev.get("release_id") == release_id else None
                    db.update(item["id"], rare=keep)
                    return keep
        else:
            pick = candidates[0] if candidates else None
        info = rare_info(pick) if pick and (pick.get("rarity") or {}).get("rare") else None
        alerted = list(item.get("rare_alerted") or [])
        fields: dict[str, Any] = {"rare": info}
        if info and info["release_id"] not in alerted:
            fields["rare_alerted"] = [*alerted, info["release_id"]]
            db.update(item["id"], **fields)
            await notify_rare(item["id"], info)
        else:
            db.update(item["id"], **fields)
        return info

    # ---- worker ---------------------------------------------------------------
    async def process(item: dict) -> None:
        photos = [settings.photos_dir / p["path"] for p in item["photos"]]
        roles = [p.get("role", "") for p in item["photos"]]
        provider = vision_factory(item.get("provider") or settings.vision_provider, settings)
        extracted = await provider.extract(photos, roles, item.get("hint", ""))
        db.update(item["id"], extracted=extracted, provider=provider.name)
        cands = await dg.find_candidates(client(), extracted, detail_lookups=settings.detail_lookups)
        await update_rare(item, cands)
        db.update(item["id"], candidates=cands, status="review")

    async def run_item(item: dict) -> None:
        """Process one claimed item; on failure store a friendly error instead of raising."""
        try:
            await process(item)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, HTTPException) and isinstance(e.__cause__, Exception):
                e = e.__cause__
            info = errors.describe(e, settings, item.get("provider") or settings.vision_provider)
            log.warning("Item %s failed: %s | %s", item["id"], info["title"], info["detail"])
            db.update(item["id"], status="error", error=info["title"], error_info=info)

    async def worker_loop() -> None:
        while True:
            item = db.claim_next()
            if not item:
                state["wake"].clear()
                try:
                    await asyncio.wait_for(state["wake"].wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            await run_item(item)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(worker_loop()) for _ in range(max(1, settings.worker_concurrency))] if run_worker else []
        yield
        for t in tasks:
            t.cancel()
        if state["discogs"] is not None:
            await state["discogs"].aclose()

    app = FastAPI(title="vinyl2discogs", lifespan=lifespan)
    app.state.db = db
    app.state.process = process
    app.state.run_item = run_item

    @app.exception_handler(dg.DiscogsError)
    async def discogs_error(request: Request, exc: dg.DiscogsError):
        info = errors.describe(exc, settings)
        log.warning("Discogs error on %s: %s", request.url.path, info["detail"])
        return JSONResponse({"detail": f"{info['title']}. {info['hint']}", "error_info": info},
                            status_code=exc.status if exc.status in (401, 429) else 502)

    # ---- optional basic auth -------------------------------------------------
    if settings.app_password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            hdr = request.headers.get("authorization", "")
            ok = False
            if hdr.lower().startswith("basic "):
                try:
                    _, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
                    ok = secrets.compare_digest(pw, settings.app_password)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="vinyl2discogs"'})
            return await call_next(request)

    # ---- helpers --------------------------------------------------------------
    def save_upload(dest_dir: Path, up: UploadFile, role: str, idx: int) -> dict:
        ext = Path(up.filename or "").suffix.lower()
        ext = ext if ext in IMAGE_EXT else ".jpg"
        name = f"{idx:02d}_{role or 'foto'}{ext}"
        with open(dest_dir / name, "wb") as f:
            shutil.copyfileobj(up.file, f)
        return {"path": f"{dest_dir.name}/{name}", "role": role}

    def wake() -> None:
        state["wake"].set()

    def get_item(item_id: int) -> dict:
        item = db.get(item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        return item

    # ---- routes ---------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status():
        out: dict[str, Any] = {
            "providers": vision.available_providers(settings),
            "default_provider": settings.vision_provider,
            "default_folder_id": settings.discogs_default_folder_id,
            "currency": settings.currency,
            "counts": db.counts(),
            "discogs_ok": False,
        }
        try:
            c = client()
            out["username"] = await c.username()
            out["folders"] = await c.folders()
            fields = await c.fields()
            out["fields"] = {f["name"]: f.get("options", []) for f in fields if f.get("type") == "dropdown"}
            out["discogs_ok"] = True
        except HTTPException as e:
            out["discogs_error"] = e.detail
        except dg.DiscogsError as e:
            out["discogs_error"] = str(e)
        return out

    @app.post("/api/items")
    async def create_item(
        cover: UploadFile | None = File(None),
        label: UploadFile | None = File(None),
        runout: UploadFile | None = File(None),
        extra: list[UploadFile] | None = File(None),
        hint: str = Form(""),
        provider: str = Form(""),
    ):
        uploads = [(u, r) for u, r in ((cover, "cover"), (label, "label"), (runout, "runout")) if u and u.filename]
        uploads += [(u, "") for u in (extra or []) if u and u.filename]
        if not uploads:
            raise HTTPException(400, "At least one photo is required")
        d = settings.photos_dir / uuid.uuid4().hex[:12]
        d.mkdir(parents=True)
        photos = [save_upload(d, u, r, i) for i, (u, r) in enumerate(uploads)]
        item = db.create(photos=photos, hint=hint.strip(), provider=provider or None)
        wake()
        return item

    @app.post("/api/inbox/import")
    async def import_inbox():
        """Each subfolder of /data/inbox is one record. Optional hint.txt with hints."""
        created = []
        for sub in sorted(p for p in settings.inbox_dir.iterdir() if p.is_dir()):
            imgs = sorted(p for p in sub.iterdir() if p.suffix.lower() in IMAGE_EXT)
            if not imgs:
                continue
            hint_file = sub / "hint.txt"
            hint = hint_file.read_text(encoding="utf-8").strip() if hint_file.exists() else ""
            hint = f"(folder: {sub.name})\n{hint}".strip()
            d = settings.photos_dir / uuid.uuid4().hex[:12]
            d.mkdir(parents=True)
            photos = []
            for i, img in enumerate(imgs):
                role = next((v for k, v in ROLE_WORDS.items() if k in img.stem.lower()), "")
                name = f"{i:02d}_{role or 'foto'}{img.suffix.lower()}"
                shutil.move(str(img), d / name)
                photos.append({"path": f"{d.name}/{name}", "role": role})
            shutil.rmtree(sub, ignore_errors=True)
            created.append(db.create(photos=photos, hint=hint, source="inbox")["id"])
        wake()
        return {"created": created}

    @app.post("/api/items/retry-errors")
    async def retry_errors():
        n = db.requeue_errors()
        wake()
        return {"requeued": n}

    @app.get("/api/items")
    async def list_items(status: str = "", rare: bool = False):
        statuses = [s for s in status.split(",") if s] or None
        return {"items": db.list(statuses, rare_only=rare), "counts": db.counts()}

    @app.get("/api/items/{item_id}")
    async def item_detail(item_id: int):
        return get_item(item_id)

    @app.delete("/api/items/{item_id}")
    async def delete_item(item_id: int):
        item = get_item(item_id)
        dirs = {p["path"].split("/")[0] for p in item["photos"]}
        for d in dirs:
            shutil.rmtree(settings.photos_dir / d, ignore_errors=True)
        db.delete(item_id)
        return {"ok": True}

    @app.post("/api/items/{item_id}/reprocess")
    async def reprocess(item_id: int, body: ReprocessIn):
        item = get_item(item_id)
        if body.extracted is not None:
            ex = vision.normalize_extracted({**(item.get("extracted") or {}), **body.extracted})
            ex["confidence"] = "high"  # corrected by the user
            cands = await dg.find_candidates(client(), ex, detail_lookups=settings.detail_lookups)
            await update_rare(item, cands)
            return db.update(item_id, extracted=ex, candidates=cands, status="review", error=None, error_info=None)
        fields: dict[str, Any] = {"status": "pending", "error": None, "error_info": None}
        if body.provider:
            fields["provider"] = body.provider
        if body.hint is not None:
            fields["hint"] = body.hint
        item = db.update(item_id, **fields)
        wake()
        return item

    @app.post("/api/items/{item_id}/search")
    async def manual_search(item_id: int, body: SearchIn):
        item = get_item(item_id)
        c = client()
        ref = dg.parse_release_ref(body.q)
        if ref:
            cands = [await dg.candidate_from_release_id(c, ref)]
        else:
            ex = item.get("extracted") or {}
            results = await c.search(q=body.q, format="Vinyl")
            cands = [dg.to_candidate(r, *dg.score_search_result(r, ex)) for r in results]
            cands.sort(key=lambda x: x["score"], reverse=True)
        return {"candidates": cands}

    @app.get("/api/releases/{release_id}/owned")
    async def owned(release_id: int):
        return {"instances": len(await client().instances_of(release_id))}

    @app.post("/api/items/{item_id}/add")
    async def add(item_id: int, body: AddIn):
        item = get_item(item_id)
        c = client()
        folder = body.folder_id or settings.discogs_default_folder_id
        if not body.force:
            existing = await c.instances_of(body.release_id)
            if existing:
                return JSONResponse(
                    {"detail": f"This release is already in your collection ({len(existing)}x).", "duplicate": len(existing)},
                    status_code=409,
                )
        instance_id = await c.add_to_collection(body.release_id, folder)
        field_ids = {f["name"].lower(): f["id"] for f in await c.fields()}
        warnings = []
        for name, value in (("media condition", body.media_condition),
                            ("sleeve condition", body.sleeve_condition),
                            ("notes", body.notes)):
            if value and name in field_ids:
                try:
                    await c.set_field(folder, body.release_id, instance_id, field_ids[name], value)
                except dg.DiscogsError as e:
                    warnings.append(f"{name}: {e}")
        await update_rare(item, item.get("candidates") or [], body.release_id)
        item = db.update(
            item_id, status="added", release_id=body.release_id, instance_id=instance_id, folder_id=folder,
            media_condition=body.media_condition, sleeve_condition=body.sleeve_condition, notes=body.notes,
            error="; ".join(warnings) or None,
        )
        return {"item": item, "warnings": warnings}

    @app.post("/api/items/{item_id}/status")
    async def set_status(item_id: int, body: StatusIn):
        if body.status not in ("skipped", "not_found", "review", "pending"):
            raise HTTPException(400, "Invalid status")
        get_item(item_id)
        item = db.update(item_id, status=body.status)
        if body.status == "pending":
            wake()
        return item

    @app.get("/api/export/not_found.csv")
    async def export_not_found():
        """Records that need to be submitted to Discogs by hand."""
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "artist", "title", "label", "catno", "country", "year", "matrix_a", "matrix_b", "notes", "hint"])
        for it in db.list(["not_found"]):
            e = it.get("extracted") or {}
            w.writerow([it["id"], *(e.get(k) or "" for k in
                       ("artist", "title", "label", "catno", "country", "year", "matrix_a", "matrix_b", "notes")),
                        it.get("hint", "")])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=vinyl2discogs_not_found.csv"})

    app.mount("/photos", StaticFiles(directory=settings.photos_dir), name="photos")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


logging.basicConfig(level=logging.INFO)
# Run with: uvicorn --factory app.main:create_app
