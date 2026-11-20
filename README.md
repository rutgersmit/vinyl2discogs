# vinyl2discogs

Catalogue old vinyl into your Discogs collection by taking photos. vinyl2discogs:

1. reads the cover, centre label and runout with a vision model (Claude, OpenAI or a local Ollama model),
2. finds the matching **pressing** on Discogs,
3. adds it to your collection with media/sleeve condition once you confirm.

It's built for records without a barcode. The catalogue number and label details find the release, and the matrix/runout etching tells pressings apart.

It's a mobile-first web app: run it on a home server and use it from your phone at the record shelf.

## Quick start

**One-liner**

```bash
docker run -d --name vinyl2discogs --restart unless-stopped -p 8765:8000 \
  -v "$PWD/data:/data" \
  -e DISCOGS_TOKEN=your-discogs-token \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/rutgersmit/vinyl2discogs:latest
```

Using OpenAI or a local model instead? Replace the `ANTHROPIC_API_KEY` line with `-e OPENAI_API_KEY=sk-...` or `-e OLLAMA_URL=http://<ollama-host>:11434`.

**Docker Compose**

```bash
mkdir vinyl2discogs && cd vinyl2discogs
curl -O https://raw.githubusercontent.com/rutgersmit/vinyl2discogs/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/rutgersmit/vinyl2discogs/main/.env.example
# edit .env: DISCOGS_TOKEN + at least one of ANTHROPIC_API_KEY, OPENAI_API_KEY, OLLAMA_URL
docker compose up -d
```

Open `http://<your-server>:8765` on your phone and add it to your home screen.

| Setting | Where to get it |
|---|---|
| `DISCOGS_TOKEN` | https://www.discogs.com/settings/developers → *Generate new token* |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| `OLLAMA_URL` | your Ollama server, e.g. `http://192.168.1.10:11434`, with a vision model pulled (`ollama pull qwen2.5vl:7b`) |

You need at least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `OLLAMA_URL`. With several configured, the default is the first one in that order (override with `VISION_PROVIDER`), and you can switch per record in the UI. Models are configurable with `CLAUDE_MODEL`, `OPENAI_MODEL` and `OLLAMA_MODEL`. `OPENAI_BASE_URL` accepts any endpoint that speaks the OpenAI Responses API. All other settings are optional; see [.env.example](.env.example).

Photos and the queue database live in `./data`. The container fixes the folder's ownership on start and runs as UID/GID 1000. Set `PUID`/`PGID` if your host user has different IDs and you want to manage `data/inbox` from the host.

Images are published for `linux/amd64` and `linux/arm64`, so a Raspberry Pi works too. To build the image yourself, uncomment `build: .` in `docker-compose.yml`.

> **HTTPS:** some mobile browsers only open the camera directly on a secure origin, and otherwise fall back to the file picker. Put the app behind your reverse proxy with TLS. For basic protection without a proxy, set `APP_PASSWORD`.

## Usage

**Live, one record at a time**
1. On *New*, take photos of the cover, the label and the runout.
2. Tap **Identify now**.
3. Pick the right candidate, set the condition, and tap **Add to collection**.

After that you jump to the next item waiting for review.

**Batch from your phone**
Keep tapping **Queue & next** while you shoot. A background worker identifies the records, and you go through *Queue → Review* later.

**Batch from a folder**
1. Create one subfolder per record in `data/inbox/`, e.g. `data/inbox/box1-03/`, and put that record's photos in it.
2. Optionally add a `hint.txt` with clues for the model.
3. In the UI, go to *Queue → Import inbox*.

File names containing `cover`, `label` or `runout` get that role automatically; other names are fine too. HEIC photos from iPhones are supported.

## How identification works

1. **Extraction.** The vision model returns JSON with:
   - artist, title, label and catalogue number
   - country and year (only if printed)
   - runout text per side
   - rights society and other identifiers
   - notes on label variants

   Your hints are included in the prompt.
2. **Search.** Discogs is searched with these strategies, in order:
   - `catno` + `label`
   - `catno`
   - `artist` + `release_title`
   - barcode
   - free text

   The search stops early once there are three strong hits, to save rate limit.
3. **Scoring.** Each candidate gets points for:

   | Signal | Points |
   |---|---|
   | exact catno | 40 |
   | label | 15 |
   | artist | up to 15 (fuzzy) |
   | title | up to 15 (fuzzy) |
   | country | 8 |
   | year | 5 |

4. **Matrix refinement.** For the top 8 candidates, the full release is fetched and its *Matrix / Runout* identifiers are compared with the runout read from your photo, for up to 40 extra points. This separates, say, a first pressing from a club edition with the same catalogue number.
5. **Fixing a miss.** If the right release isn't on top, you can:
   - edit the extracted details and search again,
   - search yourself, or paste a Discogs URL or release ID,
   - re-read the photos with the other provider or an extra hint.

When adding a record, vinyl2discogs:
- checks whether the release is already in your collection, and asks before adding a duplicate,
- fills *Media Condition*, *Sleeve Condition* and *Notes*, with the options taken from your own collection fields,
- remembers your last folder and condition choices for the next record.

## Limitations

- The Discogs API can't create new releases. Mark such records as **Not in Discogs** and export them as CSV from the *Queue* view. The export includes everything that was extracted, so you can submit those releases on the website.
- Discogs allows 60 requests/minute with a token, and one record takes roughly 4–12 requests. The client throttles itself and backs off on HTTP 429.
- Small local vision models usually read covers and labels fine, but struggle with hand-etched runouts. The hosted models (Claude, OpenAI) are noticeably better there.
- When a provider fails (no credit, bad key, unreachable), the record shows what went wrong and how to fix it, with buttons to retry or read it with another configured provider. *Queue → Errors* can retry all failed records at once.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
DATA_DIR=./data uvicorn --factory app.main:create_app --reload
```

The tests run against a fake Discogs API and a stub vision provider, so they need no keys. See [CLAUDE.md](CLAUDE.md) for an architecture overview.

## License

MIT
