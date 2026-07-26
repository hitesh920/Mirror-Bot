<div align="center">

# Mirror-Bot

**A private, owner-controlled Telegram bot for downloading, processing, and
delivering files to Telegram or Cloudflare R2.**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Deployment-Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Telegram](https://img.shields.io/badge/Interface-Telegram-26A5E4?logo=telegram&logoColor=white)](https://telegram.org/)
[![Cloudflare R2](https://img.shields.io/badge/Storage-Cloudflare_R2-F38020?logo=cloudflare&logoColor=white)](https://developers.cloudflare.com/r2/)

[Features](#features) · [Quick start](#quick-start) ·
[Commands](#command-reference) · [Configuration](#configuration) ·
[Operations](#operations)

</div>

---

Mirror-Bot turns a private Telegram chat into a streamlined file-transfer
workflow. Submit a URL, magnet, torrent, or Telegram attachment; optionally
extract or archive it; then deliver the result to Telegram or a private
Cloudflare R2 bucket.

The application is asynchronous, containerized, owner-only, and designed for a
small VPS. qBittorrent, FFmpeg, 7-Zip, UnRAR, Deno, and the Python service run
inside one supervised Docker container.

> [!IMPORTANT]
> Mirror-Bot is intended for private, authorized transfers. You are responsible
> for the content you download, store, and share.

## Features

- **Flexible inputs** — Direct HTTP/HTTPS links, magnet links, `.torrent`
  files, replied Telegram media, yt-dlp-supported sites, and popular file hosts.
- **Two delivery options** — Upload to Telegram chats/channels or a private
  Cloudflare R2 bucket.
- **Built-in processing** — Create ZIP files, use password protection, extract
  archives, and apply custom output names.
- **Selective torrents** — Review torrent contents in a temporary browser page
  and download only the files you want.
- **R2 delivery pages** — Get single-file links or a polished folder page with
  individual downloads and a Copy all action.
- **Telegram-aware uploads** — Send media with metadata and thumbnails, split
  oversized files, and retry recoverable failures.
- **Useful controls** — Monitor live progress, limit concurrency, cancel tasks,
  test network speed, inspect sanitized logs, and restart gracefully.
- **Defensive defaults** — Owner-only access, isolated workspaces, validated
  paths, symlink rejection, disk/stall guards, and secret redaction.

Supported direct-host resolvers include MediaFire, GoFile, PixelDrain,
WeTransfer, OneDrive, 1fichier, DoodStream, Linkbox, KrakenFiles, Send.cm,
StreamTape, pCloud, Solidfiles, Upload.ee, Racaty, and compatible redirect
services.

## Quick start

### Requirements

- A Linux VPS with Docker Engine and Docker Compose
- A Telegram bot token from [BotFather](https://t.me/BotFather)
- A Telegram API ID and API hash from
  [my.telegram.org](https://my.telegram.org/)
- Your Telegram numeric user ID
- Optional: a private Cloudflare R2 bucket and S3 credentials
- Enough free disk for active downloads and processing

### Deploy

```bash
git clone https://github.com/hitesh920/Mirror-Bot.git
cd Mirror-Bot

cp .env.example .env
mkdir -p data/downloads data/logs
chmod 600 .env
```

Edit `.env` and add the required Telegram credentials:

```dotenv
BOT_TOKEN=123456789:replace_with_your_bot_token
OWNER_ID=123456789
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace_with_your_api_hash
```

Validate and start the service:

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

The container is ready when the logs show `BOT STARTED` and
`Starting Telegram UI`. Send `/help` to the bot from the configured owner
account.

## Command reference

All commands are restricted to `OWNER_ID`.

### Transfers

- `/add <link>` — Add a URL, magnet, or torrent link.
- Reply with `/add` — Add the replied Telegram file or link.
- `/status` — Show live progress, speed, phase, and ETA.
- `/cancel <task-id>` — Cancel one task.
- `/cancelall` — Cancel every active task.

### Cloudflare R2

- `/r2stats` — Show stored objects, usage, operations, and billing-period data.
- `/search <name>` — Search uploads and return their original stored links.
- `/search *` — List every current upload, newest first.
- `/delete <key-or-link>` — Permanently delete one file or folder after
  confirmation.
- `/delete all` — Permanently delete everything under `R2_PREFIX` after
  confirmation.

### Bot and server

- `/start` — Confirm that the bot is online.
- `/help` — Show the built-in command guide.
- `/stats` — Show uptime, CPU, RAM, disk, and task totals.
- `/speedtest` — Test VPS network speed.
- `/logs` — Send the latest sanitized application logs.
- `/restart` — Gracefully restart the containerized service.
- `/ping` — Check command responsiveness.

### `/add` options

```text
/add <link> [-z | -zp <password> | -e | -ep <password> | -n <name>]
```

- `-z` creates a ZIP archive.
- `-zp <password>` creates a password-protected ZIP archive.
- `-e` extracts a supported archive.
- `-ep <password>` extracts a password-protected archive.
- `-n <name>` applies a custom task and output name.

For yt-dlp sources, the bot presents video/audio and quality controls before
destination selection. Torrent sources open a temporary browser page where
individual files can be selected.

## Configuration

Copy [`.env.example`](.env.example) to `.env`. Never commit the populated file.

### Telegram

Required:

- `BOT_TOKEN` — Telegram bot token.
- `OWNER_ID` — Numeric ID of the only user allowed to control the bot.
- `TELEGRAM_API_ID` — Telegram application ID.
- `TELEGRAM_API_HASH` — Telegram application hash.

Optional:

- `TELEGRAM_DUMP_CHAT_ID` — Channel ID or `@username` used for Telegram
  uploads. Empty by default.
- `ENABLE_TELEGRAM_UI` — Enables the Telegram client and command handlers.
  Defaults to `true`.

If `TELEGRAM_DUMP_CHAT_ID` is configured, the bot must be allowed to post in
that chat. If it is unavailable, uploads fall back to the requesting private
chat when possible.

### Cloudflare R2

Required to enable R2:

- `R2_ENDPOINT_URL` — Account-specific S3 endpoint.
- `R2_BUCKET` — Private bucket name. The example configuration uses
  `mirror-bot`.
- `R2_ACCESS_KEY_ID` — Bucket-scoped S3 access key.
- `R2_SECRET_ACCESS_KEY` — Bucket-scoped S3 secret.

Optional:

- `R2_PREFIX` — Prefix managed by the bot. Defaults to `uploads/`.
- `R2_AUTO_DELETE_SECONDS` — Retention period. Defaults to `172800` seconds;
  use `0` to disable automatic deletion.
- `CLOUDFLARE_ACCOUNT_ID` — Enables account usage information in `/r2stats`.
- `CLOUDFLARE_API_TOKEN` — Read-only Billing and Account Analytics token used
  by `/r2stats`.

Use a private bucket and an R2 token with **Object Read & Write** access only to
the intended bucket. The optional Cloudflare API token should remain read-only.

### Runtime

- `TASK_LIMIT` — Maximum concurrently executing tasks. Defaults to `10`.
- `STATUS_UPDATE_INTERVAL` — Telegram status refresh interval. Defaults to
  `10` seconds.
- `TORRENT_SELECTION_PORT` — Temporary torrent-selector port. Defaults to
  `8001`.
- `TORRENT_SELECTION_TIMEOUT` — Selector lifetime. Defaults to `300` seconds.
- `PUBLIC_BASE_URL` — Optional public URL override. The VPS address is detected
  automatically when this is empty.
- `TZ` — Container timezone. Defaults to `Asia/Kolkata`.

Integer settings are validated during startup. Invalid ports, negative
retention, or non-numeric values stop the service with a clear configuration
error.

## Cloudflare R2 behavior

- Each task is stored under `R2_PREFIX/<task-id>/`.
- Large files use multipart uploads.
- A single-file upload returns one download button.
- A folder upload creates one private HTML landing page containing every file.
- Folder pages include a **Copy all** action that copies each basename and link.
- `/search` returns the link stored during upload; it does not generate a new
  link.
- Links are signed for seven days, while objects are deleted after the
  configured retention—two days by default—making deletion the effective
  access limit.
- The expiry sweeper runs every 15 minutes and only manages objects under
  `R2_PREFIX`.
- `/delete all` removes bot-managed objects but preserves the bucket itself.

You may also configure a matching R2 lifecycle rule as an additional
server-side safeguard.

## Networking

Docker publishes only one application port:

- `8001/tcp` is the temporary torrent file selector. Expose it publicly only
  when torrent selection is required.
- `8080/tcp` is the qBittorrent Web UI/API. It remains internal to the
  container and must never be published.

The torrent selector is created only while a selection is active. A connection
refusal while idle can therefore be expected.

## Operations

### Update

Allow active transfers to finish before recreating the container.

```bash
git status --short
git pull --ff-only
docker compose build --pull bot
docker compose up -d --no-deps --force-recreate bot
docker compose ps
docker compose logs --tail=100 bot
```

### Logs and diagnostics

```bash
docker compose logs -f bot
tail -n 200 data/logs/bot.log
grep 'event=task.failed' data/logs/bot.log
```

Application logs are rotated and sanitized. `/logs` exports the latest 2,000
sanitized lines, but logs should still be reviewed before sharing publicly.

### Stop or restart

```bash
docker compose restart bot
docker compose down
```

The shutdown path stops accepting work, cancels active tasks, closes temporary
pages, cleans qBittorrent state, and allows local cleanup to finish before the
container exits.

## Development

Mirror-Bot targets Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

python -m pytest -q
python -m ruff check .
python -m ruff format --check .
docker compose config
docker compose build bot
```

The test suite covers configuration boundaries, command output, path safety,
R2 behavior, torrent selection, cleanup, Telegram delivery, cancellation, and
runtime shutdown.

## Project structure

```text
mirrorbot/
├── commands/      Telegram command handlers
├── core/          Configuration, models, parsing, errors, and logging
├── downloaders/   Direct, Telegram, torrent, and yt-dlp engines
├── resolvers/     File-host and redirect resolution
├── services/      Processing, delivery, task management, and runtime services
└── telegram/      Messages, keyboards, state, and live status UI

scripts/           qBittorrent launcher
tests/             Unit and boundary tests
docs/              Operations and maintenance documentation
```

For architecture details, deployment operations, troubleshooting, and the
maintenance roadmap, see the
[Mirror-Bot Technical Guide](docs/MIRROR_BOT_TECHNICAL_GUIDE.md).

## Security notes

- Keep `.env`, Telegram sessions, logs, cookies, and credentials out of Git.
- Restrict `.env` permissions with `chmod 600`.
- Keep the R2 bucket private and scope credentials to the required bucket.
- Do not expose qBittorrent port `8080`.
- Treat temporary selector and R2 links as secrets.
- Review staged changes and logs before publishing them.
- Rotate any credential immediately if it is exposed.

---

<div align="center">

Built for fast, temporary, owner-controlled file delivery.

</div>
