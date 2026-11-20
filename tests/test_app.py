import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import discogs as dg
from app.config import Settings
from app.main import create_app

# ---- fake Discogs ---------------------------------------------------------------
SEARCH = [
    {"id": 222, "title": "Golden Earring - Moontan", "year": "1973", "country": "Germany",
     "label": ["Track Records", "Polydor"], "catno": "2406 112", "format": ["Vinyl", "LP", "Album"], "thumb": ""},
    {"id": 111, "title": "Golden Earring - Moontan", "year": "1973", "country": "Netherlands",
     "label": ["Polydor"], "catno": "2430 210", "format": ["Vinyl", "LP", "Album"], "thumb": ""},
    {"id": 112, "title": "Golden Earring - Moontan", "year": "1973", "country": "Netherlands",
     "label": ["Polydor"], "catno": "2430 210", "format": ["Vinyl", "LP", "Album", "Club Edition"], "thumb": ""},
    {"id": 333, "title": "Golden Earring - Cut", "year": "1982", "country": "Netherlands",
     "label": ["Polydor"], "catno": "2311 175", "format": ["Vinyl", "LP"], "thumb": ""},
]
RELEASES = {
    111: {"id": 111, "identifiers": [{"type": "Matrix / Runout", "value": "2430 210 1Y 1 670 A1"},
                                     {"type": "Matrix / Runout", "value": "2430 210 2Y 1 670 B1"}]},
    112: {"id": 112, "identifiers": [{"type": "Matrix / Runout", "value": "2430 210 1Y 2 380 CLUB"}]},
    222: {"id": 222, "identifiers": [{"type": "Matrix / Runout", "value": "2406 112 A//1 420"}]},
    333: {"id": 333, "identifiers": []},
}


class FakeDiscogs:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.collection: dict[int, list[int]] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, params = req.url.path, dict(req.url.params)
        body = json.loads(req.content) if req.content else {}
        self.calls.append((req.method, path, params or body))
        assert req.headers["Authorization"] == "Discogs token=tok"
        if path == "/oauth/identity":
            return httpx.Response(200, json={"username": "rutger"})
        if path == "/database/search":
            res = SEARCH
            if "catno" in params:
                res = [r for r in SEARCH if dg.norm(r["catno"]) == dg.norm(params["catno"])]
            return httpx.Response(200, json={"results": res})
        if path.startswith("/releases/"):
            return httpx.Response(200, json=RELEASES[int(path.rsplit("/", 1)[1])])
        if path == "/users/rutger/collection/folders":
            return httpx.Response(200, json={"folders": [{"id": 0, "name": "All", "count": 0},
                                                         {"id": 1, "name": "Uncategorized", "count": 0}]})
        if path == "/users/rutger/collection/fields":
            return httpx.Response(200, json={"fields": [
                {"id": 1, "name": "Media Condition", "type": "dropdown", "options": ["Mint (M)", "Very Good Plus (VG+)"]},
                {"id": 2, "name": "Sleeve Condition", "type": "dropdown", "options": ["Mint (M)", "Very Good (VG)"]},
                {"id": 3, "name": "Notes", "type": "textarea"},
            ]})
        if path.startswith("/users/rutger/collection/releases/"):
            rid = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"releases": [{"instance_id": i} for i in self.collection.get(rid, [])]})
        parts = path.split("/")
        if req.method == "POST" and len(parts) == 8 and parts[6] == "releases":  # add to folder
            rid = int(parts[7])
            iid = 9000 + rid
            self.collection.setdefault(rid, []).append(iid)
            return httpx.Response(201, json={"instance_id": iid, "resource_url": "x"})
        if req.method == "POST" and "/fields/" in path:
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unmocked {req.method} {path}"})


class FakeVision:
    name = "fake"

    def __init__(self, result):
        self.result = result
        self.seen = None

    async def extract(self, images, roles, hint):
        self.seen = (images, roles, hint)
        return dict(self.result)


EXTRACTED = {"artist": "Golden Earring", "title": "Moontan", "label": "Polydor", "catno": "2430 210",
             "country": "Netherlands", "year": 1973, "matrix_a": "2430 210 1Y 1 670 A1", "matrix_b": None,
             "barcode": None, "other_identifiers": None, "notes": None, "confidence": "high"}


@pytest.fixture
def env(tmp_path):
    fake = FakeDiscogs()
    vis = FakeVision(EXTRACTED)
    settings = Settings(discogs_token="tok", data_dir=tmp_path, anthropic_api_key="", vision_provider_env="claude")
    app = create_app(
        settings,
        discogs_factory=lambda s: dg.DiscogsClient(s, transport=httpx.MockTransport(fake.handler), min_interval=0),
        vision_factory=lambda name, s: vis,
        run_worker=False,
    )
    with TestClient(app) as client:
        yield client, app, fake, vis, tmp_path


def jpg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "orange").save(b, "JPEG")
    return b.getvalue()


# ---- unit ------------------------------------------------------------------------
def test_matrix_overlap():
    assert dg.matrix_overlap(["2430 210 1Y 1 670 A1"], ["2430 210 1Y 1 670 A1"]) == 1.0
    assert dg.matrix_overlap(["2430210 1Y 1 670"], ["2406 112 A//1 420"]) < 0.3
    assert dg.matrix_overlap([], ["x"]) == 0.0


def test_parse_release_ref():
    assert dg.parse_release_ref("https://www.discogs.com/release/1234567-Golden-Earring-Moontan") == 1234567
    assert dg.parse_release_ref("[r42]") == 42
    assert dg.parse_release_ref("Golden Earring") is None


# ---- end-to-end -----------------------------------------------------------------
def run_worker_once(app):
    import asyncio
    item = app.state.db.claim_next()
    asyncio.run(app.state.process(item))
    return app.state.db.get(item["id"])


def test_flow_identify_and_add(env):
    client, app, fake, vis, tmp = env
    r = client.post("/api/items", files={"cover": ("c.jpg", jpg(), "image/jpeg"), "label": ("l.jpg", jpg(), "image/jpeg")},
                    data={"hint": "Dutch pressing"})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    item = run_worker_once(app)
    assert item["status"] == "review"
    assert vis.seen[1] == ["cover", "label"] and vis.seen[2] == "Dutch pressing"
    cands = item["candidates"]
    # The Dutch pressing with the matching runout must rank first, above the club edition with the same catno.
    assert cands[0]["id"] == 111, [(c["id"], c["score"]) for c in cands]
    assert "matrix 100%" in cands[0]["reasons"]
    assert cands[1]["id"] == 112

    st = client.get("/api/status").json()
    assert st["discogs_ok"] and st["username"] == "rutger"
    assert st["fields"]["Media Condition"] == ["Mint (M)", "Very Good Plus (VG+)"]
    assert [f["id"] for f in st["folders"]] == [1]

    body = {"release_id": 111, "folder_id": 1, "media_condition": "Very Good Plus (VG+)",
            "sleeve_condition": "Very Good (VG)", "notes": "inherited"}
    r = client.post(f"/api/items/{item_id}/add", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["item"]["status"] == "added" and r.json()["item"]["instance_id"] == 9111
    field_posts = [(p, b) for m, p, b in fake.calls if "/fields/" in p]
    assert [b["value"] for _, b in field_posts] == ["Very Good Plus (VG+)", "Very Good (VG)", "inherited"]

    # Second time: duplicate check
    r = client.post(f"/api/items/{item_id}/add", json=body)
    assert r.status_code == 409 and r.json()["duplicate"] == 1
    r = client.post(f"/api/items/{item_id}/add", json={**body, "force": True})
    assert r.status_code == 200


def test_research_with_edited_fields_and_manual(env):
    client, app, fake, vis, tmp = env
    item_id = client.post("/api/items", files={"label": ("l.jpg", jpg(), "image/jpeg")}).json()["id"]
    vis.result = {**EXTRACTED, "catno": None, "matrix_a": None, "title": "Moontn"}
    run_worker_once(app)
    r = client.post(f"/api/items/{item_id}/reprocess", json={"extracted": {"catno": "2311 175", "title": "Cut"}})
    assert r.status_code == 200
    assert r.json()["candidates"][0]["id"] == 333
    r = client.post(f"/api/items/{item_id}/search", json={"q": "https://www.discogs.com/release/111-x"})
    assert r.json()["candidates"][0]["id"] == 111


def test_inbox_import_and_status(env):
    client, app, fake, vis, tmp = env
    d = tmp / "inbox" / "doos1-plaat3"
    d.mkdir(parents=True)
    (d / "IMG_label.jpg").write_bytes(jpg())
    (d / "IMG_runout.jpg").write_bytes(jpg())
    (d / "hint.txt").write_text("Golden Earring", encoding="utf-8")
    r = client.post("/api/inbox/import").json()
    assert len(r["created"]) == 1
    item = app.state.db.get(r["created"][0])
    assert [p["role"] for p in item["photos"]] == ["label", "runout"]
    assert "Golden Earring" in item["hint"] and not d.exists()
    assert client.get(f"/photos/{item['photos'][0]['path']}").status_code == 200

    client.post(f"/api/items/{item['id']}/status", json={"status": "not_found"})
    csv = client.get("/api/export/not_found.csv").text
    import csv as csvmod
    rows = list(csvmod.reader(io.StringIO(csv)))
    assert rows[0][0] == "id" and len(rows) == 2 and "Golden Earring" in rows[1][-1]


def test_ollama_provider_and_json_parsing(tmp_path):
    import asyncio
    import respx
    from app.vision import OllamaVision, _parse_json

    assert _parse_json('```json\n{"artist": "X", "year": "1973", "catno": "null"}\n```') == {
        **{k: None for k in ("title", "label", "country", "matrix_a", "matrix_b", "barcode", "other_identifiers", "notes")},
        "artist": "X", "year": 1973, "catno": None, "confidence": "low"}

    img = tmp_path / "a.jpg"
    img.write_bytes(jpg())
    s = Settings(data_dir=tmp_path, ollama_url="http://ollama:11434", ollama_model="m")
    with respx.mock:
        route = respx.post("http://ollama:11434/api/chat").respond(
            json={"message": {"content": json.dumps({"artist": "Golden Earring", "confidence": "medium"})}})
        out = asyncio.run(OllamaVision(s).extract([img], ["label"], "hint!"))
        sent = json.loads(route.calls[0].request.content)
    assert out["artist"] == "Golden Earring" and out["confidence"] == "medium"
    assert sent["format"] == "json" and len(sent["messages"][0]["images"]) == 1
    assert "hint!" in sent["messages"][0]["content"]


def test_claude_provider_request_shape(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from app.vision import ClaudeVision

    img = tmp_path / "a.jpg"
    img.write_bytes(jpg())
    s = Settings(data_dir=tmp_path, anthropic_api_key="sk-test", claude_model="claude-sonnet-5-5")
    prov = ClaudeVision(s)
    sent = {}

    async def create(**kw):
        sent.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(
            type="text", text='{"artist": "Golden Earring", "catno": "2430 210", "confidence": "high"}')])

    prov._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    out = asyncio.run(prov.extract([img, img], ["cover", "label"], ""))
    assert out["catno"] == "2430 210"
    assert sent["model"] == "claude-sonnet-5-5"
    content = sent["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"


def test_provider_autodetect(tmp_path):
    assert Settings(data_dir=tmp_path, anthropic_api_key="k", ollama_url="").vision_provider == "claude"
    assert Settings(data_dir=tmp_path, anthropic_api_key="", ollama_url="http://x:11434").vision_provider == "ollama"
    assert Settings(data_dir=tmp_path, anthropic_api_key="k", ollama_url="http://x", vision_provider_env="ollama").vision_provider == "ollama"
