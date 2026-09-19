<div align="center">

# Mirror-Bot

### Private file transfers, controlled from Telegram

Download from the web, torrents, or Telegram; process the result; then deliver
it to Telegram or a private Cloudflare R2 bucket.

[![CI](https://github.com/hitesh920/Mirror-Bot/actions/workflows/ci.yml/badge.svg)](https://github.com/hitesh920/Mirror-Bot/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Run_with-Docker-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![Telegram](https://img.shields.io/badge/Control_from-Telegram-26A5E4?logo=telegram&logoColor=white)](https://telegram.org/)
[![Cloudflare R2](https://img.shields.io/badge/Optional_storage-Cloudflare_R2-F38020?logo=cloudflare&logoColor=white)](https://developers.cloudflare.com/r2/)

[Get started](#quick-start) · [Commands](#commands) ·
[Documentation](https://github.com/hitesh920/Mirror-Bot/wiki) ·
[Configuration](https://github.com/hitesh920/Mirror-Bot/wiki/Configuration) ·
[Troubleshooting](https://github.com/hitesh920/Mirror-Bot/wiki/Operations-and-Troubleshooting)

</div>

> [!IMPORTANT]
> Mirror-Bot is an owner-only bot for private, authorized transfers. It is not
> a public leech service. You are responsible for the files you download,
> store, and share.

## What it does

Mirror-Bot gives one Telegram user a clean interface for moving files without
manually managing download tools, archives, and uploads.

```text
Telegram command
      ↓
Detect and resolve the source
      ↓
Download ──→ optionally rename, ZIP, or extract
      ↓
Validate paths, disk space, and progress
      ↓
Upload to Telegram or Cloudflare R2
      ↓
Clean the temporary workspace
```

Supported inputs include HTTP/HTTPS links, magnets, `.torrent` files, Telegram
media, yt-dlp-compatible sites, batches, and common file hosts. Transfers have
live progress, cancellation, bounded concurrency, retries, disk and stall
guards, graceful shutdown, and failure cleanup.

See the [Architecture](https://github.com/hitesh920/Mirror-Bot/wiki/Architecture)
and [Delivery](https://github.com/hitesh920/Mirror-Bot/wiki/Delivery) pages for
the complete capability and lifecycle reference.

## Quick start

You need a Linux server with Docker Engine and Docker Compose, a Telegram bot
token, Telegram API credentials, and your numeric Telegram user ID.

```bash
git clone https://github.com/hitesh920/Mirror-Bot.git
cd Mirror-Bot

cp .env.example .env
mkdir -p data/downloads data/logs
chmod 600 .env
```

Set the four required values in `.env`:

```dotenv
BOT_TOKEN=123456789:replace_with_your_bot_token
OWNER_ID=123456789
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace_with_your_api_hash
```

Start the service:

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

The bot is ready when the logs contain `BOT STARTED` and
`Starting Telegram UI`. Send `/start` or `/help` from the account configured as
`OWNER_ID`.

For R2 setup, advanced safety settings, networking, and production procedures,
continue with [Getting Started](https://github.com/hitesh920/Mirror-Bot/wiki/Getting-Started)
and [Configuration](https://github.com/hitesh920/Mirror-Bot/wiki/Configuration).

## Commands

Every command is restricted to `OWNER_ID`.

| Command | Purpose |
| --- | --- |
| `/add <link>` | Add a URL, magnet, or torrent link |
| Reply with `/add` | Add a replied Telegram file, `.torrent`, or supported link |
| Reply with `/add -b [count]` | Collect a batch from one or more messages |
| `/status` | Show active transfer progress |
| `/cancel <task-id>` | Cancel one task |
| `/cancelall` | Cancel all active tasks |
| `/search <name>` | Search current R2 uploads |
| `/delete <key-or-link>` | Delete R2 content after confirmation |
| `/r2stats` | Show R2 usage information |
| `/stats` | Show bot and server statistics |
| `/speedtest` | Test server network speed |
| `/logs` | Send recent sanitized logs |
| `/restart` | Gracefully restart the bot |
| `/ping` | Measure Telegram response latency |
| `/help` | Show the built-in command guide |

The [Commands Wiki page](https://github.com/hitesh920/Mirror-Bot/wiki/Commands)
documents `/add` flags, batch behavior, and the BotFather command menu.

## Documentation

The detailed user, operator, and engineering documentation lives in the
[GitHub Wiki](https://github.com/hitesh920/Mirror-Bot/wiki):

| Page | Contents |
| --- | --- |
| [Getting Started](https://github.com/hitesh920/Mirror-Bot/wiki/Getting-Started) | Installation and first transfer |
| [Commands](https://github.com/hitesh920/Mirror-Bot/wiki/Commands) | Commands, options, batches, and BotFather menu |
| [Configuration](https://github.com/hitesh920/Mirror-Bot/wiki/Configuration) | Telegram, R2, runtime, and safety settings |
| [Architecture](https://github.com/hitesh920/Mirror-Bot/wiki/Architecture) | Runtime design, source engines, processing, and code layout |
| [Delivery](https://github.com/hitesh920/Mirror-Bot/wiki/Delivery) | Telegram and Cloudflare R2 behavior |
| [Networking](https://github.com/hitesh920/Mirror-Bot/wiki/Networking) | Ports, selectors, and firewall requirements |
| [Deployment](https://github.com/hitesh920/Mirror-Bot/wiki/Deployment) | Updates, restarts, shutdown, and rollback |
| [Operations and Troubleshooting](https://github.com/hitesh920/Mirror-Bot/wiki/Operations-and-Troubleshooting) | Logs, diagnostics, and common failures |
| [Security](https://github.com/hitesh920/Mirror-Bot/wiki/Security) | Credential and operational security |
| [Development](https://github.com/hitesh920/Mirror-Bot/wiki/Development) | Validation, standards, and release checklist |
| [Roadmap](https://github.com/hitesh920/Mirror-Bot/wiki/Roadmap) | Recommended future improvements |

## Development

Mirror-Bot targets Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

python -m ruff check .
python -m ruff format --check .
python -m pytest -q
```

CI runs Ruff and the full test suite for every push and pull request. Review the
[development guide](https://github.com/hitesh920/Mirror-Bot/wiki/Development)
before changing transfer, cleanup, deployment, or security behavior.

## Security

- Never commit or share `.env`, Telegram sessions, logs, cookies, or signed URLs.
- Keep the R2 bucket private and credentials scoped to the intended bucket.
- Never publish qBittorrent port `8080`.
- Treat selector and download URLs as bearer secrets.
- Rotate exposed credentials immediately.
- Download and distribute only content you are authorized to handle.

Read the complete [security model](https://github.com/hitesh920/Mirror-Bot/wiki/Security).

---

<div align="center">

Built for fast, temporary, owner-controlled file delivery.

</div>
