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

[Get started](#quick-start) · [See commands](#commands) ·
[Configure](#configuration) · [Operate](#operations) · [Develop](#development)

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

### Highlights

| Area | Capabilities |
| --- | --- |
| Sources | HTTP/HTTPS links, magnets, `.torrent` files, Telegram media, replied links, yt-dlp-supported sites, and common file hosts |
| Processing | Rename outputs, create ZIP archives, password-protect ZIPs, and extract regular or password-protected archives |
| Delivery | Upload to the requesting Telegram chat, an optional dump channel, or a private Cloudflare R2 bucket |
| Torrents | Select individual files through a short-lived, tokenized browser page |
| Batches | Collect up to 20 unique direct links from one or more Telegram messages and upload separately or as one ZIP |
| Visibility | Live progress, speed and ETA, task cancellation, server statistics, network tests, R2 statistics, and sanitized logs |
| Reliability | Concurrency limits, retries, byte-level R2 progress tracking, stall and disk guards, graceful shutdown, and failure cleanup |

Built-in host resolvers cover MediaFire, GoFile, PixelDrain, WeTransfer,
OneDrive, 1fichier, DoodStream, Linkbox, KrakenFiles, Send.cm, StreamTape,
pCloud, Solidfiles, Upload.ee, Racaty, and compatible redirect services.

## Quick start

### 1. Prepare the server

You need:

- A Linux server with Docker Engine and the Docker Compose plugin
- A Telegram bot token from [BotFather](https://t.me/BotFather)
- A Telegram API ID and API hash from
  [my.telegram.org](https://my.telegram.org/)
- Your numeric Telegram user ID
- Enough free disk space for downloads and temporary processing
- Optionally, a private Cloudflare R2 bucket

The image already contains Python 3.12, qBittorrent, FFmpeg, 7-Zip, UnRAR,
Deno, and the application. You do not need to install them on the host.

### 2. Clone and configure

```bash
git clone https://github.com/hitesh920/Mirror-Bot.git
cd Mirror-Bot

cp .env.example .env
mkdir -p data/downloads data/logs
chmod 600 .env
```

Open `.env` and set the four required values:

```dotenv
BOT_TOKEN=123456789:replace_with_your_bot_token
OWNER_ID=123456789
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace_with_your_api_hash
```

> [!WARNING]
> Never commit or share `.env`. If a credential is exposed, rotate it before
> continuing.

### 3. Start Mirror-Bot

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

The bot is ready when the logs include `BOT STARTED` and
`Starting Telegram UI`. Send `/start` or `/help` from the Telegram account whose
ID is configured as `OWNER_ID`.

## Your first transfer

Send a direct link:

```text
/add https://example.com/file.zip
```

Or reply to a Telegram file, link, or `.torrent` attachment:

```text
/add
```

Mirror-Bot detects the source, asks for any source-specific choices, and then
shows the available destinations. Processing options can be added directly to
the command:

```text
/add https://example.com/files -z
/add https://example.com/archive.zip -e
/add https://example.com/archive.zip -ep secret
/add https://example.com/video -n vacation-video
```

For yt-dlp sources, choose video/audio and quality before the destination. For
torrents, the bot provides a temporary selection page so you can choose files
before downloading.

## Commands

Every command is restricted to `OWNER_ID`.

| Command | Purpose |
| --- | --- |
| `/add <link>` | Add a URL, magnet, or torrent link |
| Reply with `/add` | Add a replied Telegram file, `.torrent`, or supported link |
| Reply with `/add -b [count]` | Collect a batch from the replied message and optional following message IDs |
| `/status` | Show active tasks, phases, progress, speed, and ETA |
| `/cancel <task-id>` | Cancel one task |
| `/cancelall` | Cancel all active tasks |
| `/search <name>` | Search current R2 uploads by name |
| `/search *` | List all current R2 uploads, newest first |
| `/delete <key-or-link>` | Delete one R2 file or folder after confirmation |
| `/delete all` | Delete all objects managed under `R2_PREFIX` after confirmation |
| `/r2stats` | Show R2 objects, storage use, operations, and available billing data |
| `/stats` | Show uptime, CPU, memory, disk, and task totals |
| `/speedtest` | Measure the server's network speed |
| `/logs` | Send the latest sanitized application logs |
| `/restart` | Gracefully restart the bot service |
| `/ping` | Check command responsiveness |
| `/start` | Confirm the bot is online |
| `/help` | Open the built-in command guide |

<details>
<summary><strong>BotFather command menu</strong></summary>

Paste this list into BotFather's `/setcommands` flow:

```text
add - Add a link, torrent, or replied Telegram file
status - Show active transfer progress
cancel - Cancel one task by ID
cancelall - Cancel every active task
search - Search current Cloudflare R2 uploads
delete - Delete Cloudflare R2 uploads
r2stats - Show Cloudflare R2 usage
stats - Show bot and server statistics
speedtest - Test server network speed
logs - Send recent sanitized logs
restart - Gracefully restart Mirror-Bot
ping - Check bot responsiveness
help - Show the command guide
```

</details>

### `/add` options

```text
/add <link> [-z | -zp <password> | -e | -ep <password> | -n <name>]
```

| Option | Result |
| --- | --- |
| `-z` | Create a ZIP archive |
| `-zp <password>` | Create a password-protected ZIP archive |
| `-e` | Extract a supported archive |
| `-ep <password>` | Extract a password-protected archive |
| `-n <name>` | Set a custom task/output name |

### Batch mode

Reply to a message containing links:

```text
/add -b
/add -b 3
/add -b 3 -n release-bundle
```

`-b 3` checks the replied message and the next two exact Telegram message IDs.
Batch mode requires at least two and accepts at most 20 unique supported direct
links. It skips duplicates, malformed links, Telegram sources, magnets,
torrents, and yt-dlp sources. You can upload successful results separately or
combine them into one uncompressed ZIP. In batch mode, `-n` names that ZIP;
other archive flags cannot be combined with `-b`.

## Configuration

Start from [`.env.example`](.env.example). Blank optional values disable their
related feature.

### Telegram — required

| Variable | Description |
| --- | --- |
| `BOT_TOKEN` | Token created by BotFather |
| `OWNER_ID` | Numeric ID of the only account allowed to control the bot |
| `TELEGRAM_API_ID` | Application ID from my.telegram.org |
| `TELEGRAM_API_HASH` | Application hash from my.telegram.org |

### Telegram — optional

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_DUMP_CHAT_ID` | empty | Channel ID such as `-1001234567890`, or `@username`, used for Telegram uploads |

The bot must be able to post in the configured dump chat. If it is unavailable,
delivery falls back to the requesting private chat when possible.

### Cloudflare R2

All four R2 credential variables are required to enable R2 delivery.

| Variable | Default | Description |
| --- | --- | --- |
| `R2_ENDPOINT_URL` | empty | Account-specific S3 endpoint |
| `R2_BUCKET` | `mirror-bot` | Private destination bucket |
| `R2_ACCESS_KEY_ID` | empty | Bucket-scoped S3 access key |
| `R2_SECRET_ACCESS_KEY` | empty | Bucket-scoped S3 secret |
| `R2_PREFIX` | `uploads/` | Prefix owned and managed by the bot |
| `R2_AUTO_DELETE_SECONDS` | `172800` | Object retention in seconds; `0` disables automatic deletion |
| `CLOUDFLARE_ACCOUNT_ID` | empty | Enables account data in `/r2stats` |
| `CLOUDFLARE_API_TOKEN` | empty | Read-only Billing and Account Analytics token for `/r2stats` |

Use a private bucket and an R2 token limited to **Object Read & Write** for that
bucket. Keep the optional Cloudflare API token read-only.

### Runtime

| Variable | Default | Description |
| --- | --- | --- |
| `TASK_LIMIT` | `10` | Maximum number of concurrently executing tasks |
| `STATUS_UPDATE_INTERVAL` | `10` | Seconds between Telegram status refreshes |
| `TORRENT_SELECTION_PORT` | `8001` | Port used by temporary torrent-selection pages |
| `TORRENT_SELECTION_TIMEOUT` | `300` | Selection-page lifetime in seconds |
| `PUBLIC_BASE_URL` | auto-detected | Optional public URL override for temporary pages |
| `TZ` | `Asia/Kolkata` | Container timezone |

### Advanced safety and timeouts

These variables are optional; the built-in defaults are shown.

| Variable | Default | Description |
| --- | --- | --- |
| `LOG_FILE` | `logs/bot.log` | Application log path inside the container |
| `DISK_MIN_RESERVE_BYTES` | `5368709120` | Minimum free disk space to preserve |
| `DISK_RESERVE_RATIO` | `0.05` | Minimum free fraction to preserve; the larger reserve wins |
| `STALL_TIMEOUT_SECONDS` | `600` | Maximum time without transfer progress |
| `R2_STALL_TIMEOUT_SECONDS` | `1800` | Longer no-progress allowance for R2 uploads |
| `GUARD_CHECK_INTERVAL_SECONDS` | `5` | Disk/stall watchdog interval |
| `TORRENT_METADATA_TIMEOUT` | `300` | Maximum torrent metadata wait |
| `TORRENT_ADD_TIMEOUT` | `60` | Maximum wait for qBittorrent registration |

Invalid numeric values, ports, or retention settings stop startup with a clear
configuration error instead of silently using unsafe values.

## Delivery behavior

### Telegram

- Files are sent with useful media metadata and generated thumbnails when
  available.
- Oversized files are split for Telegram delivery.
- Recoverable upload errors and Telegram flood waits are retried.
- A configured dump channel keeps large uploads out of the private bot chat.

### Cloudflare R2

- Every task receives an isolated path:
  `R2_PREFIX/<task-id>/<relative-path>`.
- A single file returns a private download link. Multiple files create a small
  HTML folder page with individual links and a **Copy all** action.
- Download links are signed for seven days. Objects are removed after
  `R2_AUTO_DELETE_SECONDS`, so the shorter object lifetime is the effective
  access period.
- The expiry sweep runs hourly and sends one Telegram warning when an upload
  enters its final 12 hours.
- Large files use multipart uploads with bounded retries. Progress is measured
  from uploaded bytes and throttled before Telegram status updates, avoiding a
  request per chunk.
- Failed uploads abort incomplete multipart sessions and remove keys created by
  that task, preventing abandoned parts and unexpected bucket use.
- `/delete all` removes objects only under `R2_PREFIX`; it never deletes the
  bucket itself.

> [!TIP]
> A matching R2 lifecycle rule is a useful server-side backup for automatic
> deletion.

## Networking

| Port | Exposure | Purpose |
| --- | --- | --- |
| `8001/tcp` | Published by Compose | Short-lived torrent file selector |
| `8080/tcp` | Container-internal only | qBittorrent Web API |

Do not publish port `8080`. The selector on port `8001` exists only while a
selection is active, so a connection refusal while idle is expected. Restrict
port `8001` with a firewall when possible and treat selection links as secrets.

## Operations

### Check status and logs

```bash
docker compose ps
docker compose logs --tail=100 bot
tail -n 200 data/logs/bot.log
```

Follow logs or inspect task failures:

```bash
docker compose logs -f bot
grep 'event=task.failed' data/logs/bot.log
```

Logs are rotated and sanitized, and `/logs` exports only the latest sanitized
lines. Review any log file before sharing it publicly.

### Update

Let active transfers finish before recreating the container:

```bash
git status --short
git pull --ff-only
docker compose build --pull bot
docker compose up -d --no-deps --force-recreate bot
docker compose ps
docker compose logs --tail=100 bot
```

### Restart or stop

```bash
docker compose restart bot
docker compose down
```

Shutdown stops new work, cancels active tasks, closes temporary pages, removes
qBittorrent state, and gives local cleanup time to finish.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The bot does not reply | Confirm the sender matches `OWNER_ID`, then inspect `docker compose logs --tail=100 bot` |
| Telegram delivery fails | Check bot permissions and `TELEGRAM_DUMP_CHAT_ID`; test without the dump channel |
| Torrent selector does not open | Check port `8001`, the server firewall, and `PUBLIC_BASE_URL` |
| R2 upload fails | Verify all four R2 variables and bucket-level Object Read & Write permission |
| A transfer stalls | Inspect the task phase and retry messages; review the relevant stall timeout before increasing it |
| Downloads stop for low disk | Run `df -h` and clear completed data while preserving the configured reserve |
| Startup exits immediately | Run `docker compose config` and check the first configuration error in the logs |

## Development

Mirror-Bot targets Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

python -m ruff check .
python -m ruff format --check .
python -m pytest -q
docker compose config
docker compose build bot
```

CI runs Ruff checks and the full pytest suite for every push and pull request.

### Repository layout

```text
mirrorbot/
├── commands/       Telegram command handlers
├── core/           Configuration, models, parsing, and shared primitives
├── downloaders/    Direct, Telegram, torrent, batch, and yt-dlp downloads
├── resolvers/      File-host and redirect resolvers
├── services/       Task lifecycle, processing, delivery, safety, and runtime
└── telegram/       Messages, keyboards, state, and live status UI

scripts/            qBittorrent launcher
tests/              Unit and boundary tests
docs/               Architecture, operations, and maintenance notes
```

For a deeper implementation and operations reference, read the
[Mirror-Bot Technical Guide](docs/MIRROR_BOT_TECHNICAL_GUIDE.md).

## Security checklist

- Keep `.env`, Telegram sessions, cookies, logs, and credentials out of Git.
- Set `.env` permissions to `600`.
- Keep the R2 bucket private and scope tokens to the smallest required access.
- Never expose qBittorrent port `8080`.
- Treat temporary selector URLs and signed R2 URLs like passwords.
- Review staged changes and logs before publishing them.
- Rotate any credential immediately after accidental exposure.
- Download and distribute only content you are authorized to handle.

---

<div align="center">

Built for fast, temporary, owner-controlled file delivery.

</div>
