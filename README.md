# Comic / Ebook Converter Telegram Bot

Send `.cbz` `.zip` `.cbr` `.rar` `.pdf` archives, or loose images, and
get back PDF / CBZ / CBR / EPUB / raw images — with live progress bars,
a full inline-button settings menu, and an admin panel. Everything is
driven by buttons; typing is only needed for free-text values (a new
filename, a batch pattern, a title, a user id to ban).

## Feature list

**Conversion core**
- CBZ/ZIP ↔ CBR/RAR ↔ PDF ↔ EPUB, in any direction, plus raw-image
  extraction, all through one settings-driven pipeline.
- Multi-volume RAR/CBR sets (`Name.part1.rar` + `Name.part2.rar`, or the
  older `.rar`/`.r00`/`.r01` scheme) auto-collected across messages.
- Multiple loose images sent one-by-one are auto-bundled into a single
  CBZ/PDF/EPUB.
- **Merge** several PDFs into one (`/merge`), or **split** a big PDF
  back into N parts or a custom part count (`/splitpdf`).
- PDF metadata (title/author) embedded on output.
- Pages are re-encoded through Pillow before packaging, so odd color
  modes or a few corrupt pages don't sink the whole book.

**Page tools** (toggle in ⚙️ Settings)
- Split two-page spreads into individual pages.
- Webtoon slicer — cuts very tall strip images into fixed-height pages.
- Reading-direction toggle (LTR / RTL-manga), written into CBZ's
  `ComicInfo.xml` and EPUB's page-progression direction.
- Cover-page detection (skip it in ebook output).
- Kindle / e-ink optimization (grayscale + resize to a Paperwhite-class
  panel resolution).
- Output quality presets (Low / Medium / High — max dimension + JPEG
  quality).
- Thumbnail generation, sent as a preview and attached to the PDF/CBZ.

**Extraction extras**
- Any video files bundled inside a `.cbr`/`.cbz`/`.rar`/`.zip` are
  detected and sent as native Telegram videos instead of being dropped.
- "Images" output format sends the pages themselves (as photo albums),
  instead of packaging them.

**Operations**
- `/cancel` stops a running download/convert job (not just the queue).
- File-size warning + confirm/cancel buttons before downloading
  anything above `FILE_SIZE_WARNING_MB`.
- Auto-cleanup: every file this bot touches — on disk *and* the
  message it delivered to you — is deleted after `FILE_TTL_HOURS`
  (default 2h).
- Per-user rate limiting and a daily data quota, both admin-adjustable
  at runtime via `/admin` (0 = unlimited, the default).
- Allowlist / banlist, toggled via `/admin`.
- `/stats` (admin-only): users seen, conversions done, data processed,
  current limits.
- `GET /health` endpoint (see "Health check & webhook mode" below).
- Logging to console and, optionally, a file (`LOG_FILE`).
- Language/locale toggle (English / Español / हिन्दी) for bot replies.
- Persistent queue and settings: a container restart doesn't lose a
  user's queue, in-progress volume/image batches, or preferences.

## Project layout

```
comic-bot/
├── bot.py                  # Pyrogram client, handlers, all inline-button UI
├── config.py                # env-var driven configuration
├── utils/
│   ├── converter.py         # extraction + the options-driven conversion pipeline
│   ├── archives.py           # CBZ/CBR packaging, PDF->images, video extraction
│   ├── ebook.py               # page tools: spreads, webtoon, kindle, thumbnails, EPUB
│   ├── pdfutil.py              # PDF merge / split / metadata
│   ├── session.py               # persistent per-user queue, settings, batch state
│   ├── admin_store.py            # persisted banlist/allowlist/limits/stats
│   ├── ratelimit.py                # per-user rate limit + daily quota checks
│   ├── cleanup.py                    # background TTL sweep of disk files
│   ├── webhook_server.py              # aiohttp health-check + optional /notify
│   ├── i18n.py                         # bot-reply translations
│   └── progress.py                      # progress-bar / speed / ETA math
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── .gitignore
```

## 1. Get credentials

1. **API_ID / API_HASH** — log in at <https://my.telegram.org> →
   *API development tools* → create an app.
2. **BOT_TOKEN** — message [@BotFather](https://t.me/BotFather) →
   `/newbot`.
3. **Your numeric user id**, for `ADMIN_IDS` — message
   [@userinfobot](https://t.me/userinfobot).

Copy `.env.example` to `.env` and fill these in (see it for every
other tunable: TTL hours, rate limit, quota, health port, etc.).

## 2. Run directly on a VPS with Docker (no venv)

```bash
git clone <your-repo-url> comic-bot
cd comic-bot
cp .env.example .env
nano .env                     # API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS

docker build -t comic-bot .
docker run -d \
  --name comic-bot \
  --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/data:/app/data \
  comic-bot
```

Or with Docker Compose (equivalent, easier to manage):

```bash
docker compose up -d --build
```

```bash
docker logs -f comic-bot
docker compose pull && docker compose up -d --build   # after a git pull
```

## 3. Deploy to Koyeb (or any Dockerfile-based PaaS)

1. Push this repo to GitHub.
2. In Koyeb: **Create App → GitHub → select repo** → it auto-detects the
   `Dockerfile`.
3. Add the environment variables from `.env.example` in the platform's
   dashboard instead of shipping a `.env` file.
4. If your platform requires a health check for a "web service" type,
   point it at `GET /health` on the exposed port (`HEALTH_CHECK_PORT`,
   default 8080) — the bot's own Telegram connection is separate from
   this and doesn't need a port at all.

The same Dockerfile works unmodified on Railway, Render, Fly.io, or a
plain `docker run` on any VPS.

## 4. Using the bot

- **Send an archive/PDF/image** — you'll see a live download bar, then
  "Converting...", then buttons for what to do with the result.
- **⚙️ Settings** (`/settings`) — output format, quality preset, reading
  direction, language, and every page-tool toggle. Changes apply to the
  *next* thing you send.
- **Rename this file** / **Skip** / **Batch rename all queued** — same
  flow as before, extension-aware (works for `.pdf`/`.cbz`/`.cbr`/`.epub`).
- **Multi-volume archives** — send each part as its own message, in any
  order; auto-converts after `MULTIVOLUME_AUTO_FINALIZE_SECONDS` of
  silence, or `/convert` now.
- **Loose images** — send several photos/image files; they're bundled
  into one output after `IMAGE_BATCH_AUTO_FINALIZE_SECONDS`, or `/convert`.
- `/merge` — send several PDFs, then tap **Merge now**.
- `/splitpdf` — send a PDF, then pick a part count (or "Custom...").
- `/queue`, `/cancel`, `/help` — as before; `/cancel` now also kills a
  running job, not just the queue.
- `/admin`, `/stats` — admin-only (must be in `ADMIN_IDS`): toggle the
  allowlist, ban/unban/allow/disallow a user id, set the rate limit and
  daily quota, view usage stats.

## Honest limitations (things that look like a feature but need a caveat)

- **CBR/RAR *output*** needs the proprietary `rar` command-line tool,
  which isn't bundled in the Docker image (only free `unrar`, used for
  *reading* CBR/RAR, is). Without it, choosing CBR output automatically
  falls back to a CBZ (ZIP) with the same pages and you're told so —
  every comic reader opens either one fine. To get genuine `.rar`
  output, install RARLab's `rar` binary into the image yourself; it's
  auto-detected via `shutil.which("rar")`.
- **"Webhook mode"** does *not* mean Telegram pushes updates over HTTP
  here. Pyrogram talks MTProto over a persistent socket, which is a
  different transport from the classic Bot-API HTTP/webhook model —
  there's nothing to "switch to webhooks" at that layer. What
  `WEBHOOK_MODE=true` actually turns on is a `POST /notify` endpoint an
  external system can call to make the bot push a message into a chat,
  alongside the always-on `GET /health`. See `utils/webhook_server.py`.
- **MOBI export** isn't included — Amazon's own tooling for it
  (`kindlegen`) has been discontinued, and the common alternative
  (Calibre's `ebook-convert`) is a large system dependency to bundle
  just for this. EPUB output works today and is what current Kindles
  (and every other e-reader) accept directly.
- **Job cancellation** (`/cancel` / the "Cancel this job" button) is
  clean for the download step (it's a cancellable async task) and for
  anything still queued. For the CPU-bound conversion step itself
  (running in a thread-pool executor), cancellation takes effect at the
  next check point rather than truly interrupting page-by-page work
  mid-flight — in practice this means cancellation is near-instant
  while downloading, and finishes the current file before stopping for
  a very large batch mid-conversion.
- **Two hard ceilings remain**, from Telegram's platform itself, not
  this code: **2GB per file** and **4096 characters per message**.
  Getting past either means running your own
  [local Bot API server](https://github.com/tdlib/telegram-bot-api) in
  `--local` mode against a *user* session instead of a bot token — a
  materially different setup this project intentionally doesn't do.

For very high concurrent load, move the conversion step from the
in-process thread pool used here to a real task queue (Celery/RQ) so
multiple worker processes can convert in parallel.
