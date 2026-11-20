# CLAUDE.md

Guidance for working on vinyl2discogs.

## What it is
A FastAPI app that identifies vinyl records from photos (cover / label / runout) and adds them to a Discogs collection. The UI is a single mobile-first page; a background worker processes a SQLite-backed queue. All user-facing text, code and docs are in English.

## Layout
- `app/main.py`: `create_app()` factory. It wires the API routes, the worker loop, inbox import, CSV export and optional basic auth. Dependencies (Discogs client, vision provider) are injectable via factory arguments; the tests rely on this.
- `app/vision.py`: the extraction prompt, `ClaudeVision` (Anthropic SDK), `OpenAIVision` (Responses API over httpx, strict `json_schema` output from `EXTRACT_SCHEMA`) and `OllamaVision` (`/api/chat` with `format: json`). `normalize_extracted()` defines the JSON shape that every other module depends on; keep `EXTRACT_SCHEMA` in sync with `FIELDS`.
- `app/errors.py`: `describe()` maps exceptions to `{title, hint, detail, scope}` for the UI. When adding a provider or failure mode, add a mapping and a test; the UI never shows raw exceptions except under "Technical details".
- `app/discogs.py`: `DiscogsClient`, an async httpx client with throttling and 429 backoff. `find_candidates()` holds the search strategy and scoring; `matrix_overlap()` compares runouts. `rarity()` derives rare-find signals from the release's `community`/`lowest_price` (data already fetched for matrix comparison, so no extra calls).
- `app/db.py`: a single `items` table with JSON columns; the statuses are documented at the top of the file.
- `app/config.py`: settings from env vars. `vision_provider` auto-selects claude → openai → ollama when `VISION_PROVIDER` is empty.
- `app/static/index.html`: vanilla JS, hash routing (`#new`, `#queue`, `#item/<id>`), no build step.

## Commands
```bash
pip install -r requirements-dev.txt
pytest -q                                              # fake Discogs + stub vision, no keys needed
DATA_DIR=./data uvicorn --factory app.main:create_app --reload
docker compose up -d --build                           # after uncommenting `build: .`
```

## Conventions
- Keep the tests offline: mock Discogs with `httpx.MockTransport` (see `FakeDiscogs` in `tests/test_app.py`) and stub vision providers.
- Respect Discogs rate limits. Any new per-candidate API call multiplies requests per record.
- Change scoring weights only together with a test that shows the ranking you want.
- The UI must keep working on a phone viewport (~390px) with no JS errors in the console.

## Not yet verified against live services
Development used mocked services only, so these still need checking against the real ones:
- the real Discogs responses for `/users/{u}/collection/fields` and the instance field `POST` (JSON body `{"value": ...}`),
- Claude, OpenAI and Ollama extraction quality on real labels and runouts; prompt tuning is likely,
- the OpenAI Responses request against the live API (default model `gpt-6.1-terra`),
- camera capture on iOS and Android behind HTTPS,
- `community.have/want`, `lowest_price` and `curr_abbr` on live `/releases/{id}`, and sensible `RARE_*` defaults.

## Ideas / backlog
- Show "already in collection" on candidates before adding.
- Show master-release grouping in the candidate list.
- Run extraction once per side for double LPs.
