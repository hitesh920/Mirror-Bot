# Mirror-Bot

A private, owner-only Telegram transfer bot for downloading, processing, and delivering files to Telegram, Google Drive, or BuzzHeavier.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![License](https://img.shields.io/github/license/hitesh920/Mirror-Bot)](LICENSE)

> Mirror-Bot is designed for a single trusted owner. It has no public dashboard and does not provide local media-library storage.

## Highlights

- One `/add` flow for direct URLs, magnets, torrent files, Google Drive, BuzzHeavier, yt-dlp links, and replied Telegram files.
- Telegram, Google Drive, and BuzzHeavier delivery destinations.
- Optional Telegram dump channel to keep the bot conversation clean.
- qBittorrent file selection through a temporary token-protected page.
- Archive extraction and ZIP creation, including password-protected archives.
- Live status with progress, size, speed, ETA, phase, and cancellation.
- Google Drive upload, download, search, share, deletion, and quota tools.
- Google Drive uploads routed into public General, Movies, Series, or Games folders.
- Direct-host resolution and shortener bypass support.
- Disk reserve protection, stalled-transfer detection, structured logs, and graceful shutdown.

## Transfer Flow

1. Send `/add <link>` or reply to a Telegram file with `/add`.
2. For yt-dlp sources, choose video/audio and quality.
3. Choose Telegram, Google Drive, or BuzzHeavier.
4. For Google Drive, choose General, Movies, Series, or Games.
5. For torrents, review and select files on the temporary selector page.
6. Monitor progress with `/status`.
7. Receive one completion message with result links.

### Processing Flags

```text
/add <link> -e
/add <link> -ep password
/add <link> -z
/add <link> -zp password
/add <link> -n "Custom name"
```

- `-e`: extract after download
- `-ep <password>`: extract a password-protected archive
- `-z`: create a ZIP archive
- `-zp <password>`: create a password-protected ZIP
- `-n <name>`: override the task display name

Extraction failures fall back to delivering the original archive when the source is otherwise usable.

## Commands

| Command | Purpose |
|---|---|
| `/add <link>` | Add a URL, magnet, Drive link, BuzzHeavier link, or yt-dlp source |
| `/add` as a reply | Download a replied Telegram file, torrent file, or link |
| `/status` | Show live active-task progress |
| `/stats` | Show uptime, CPU, RAM, disk, and task count |
| `/cancel <task-id>` | Cancel one task |
| `/cancelall` | Cancel every active task and selector |
| `/search <name>` | Search Google Drive on a temporary results page |
| `/share <drive-link>` | Create a temporary public Drive share page |
| `/delete <drive-link-or-id>` | Permanently delete a Google Drive item |
| `/gdstats` | Show Drive authentication and quota |
| `/speedtest` | Test server network speed |
| `/logs` | Send the latest sanitized application logs |
| `/restart` | Gracefully restart Mirror-Bot |
| `/help` | Show command help |

All commands are restricted to `OWNER_ID`.

## Temporary Pages

Mirror-Bot has no persistent web dashboard. It opens only short-lived tokenized pages required by Telegram workflows.

| Port | Service |
|---|---|
| `8001` | Torrent file selector |
| `8002` | Google Drive search results |
| `8003` | Google Drive public share page |

Open these TCP ports in the VPS firewall and cloud ingress rules. Anyone with a valid random URL can access that page until it expires.

## Requirements

- Docker Engine with Docker Compose
- Telegram bot token
- Telegram API ID and API hash
- Publicly reachable VPS for temporary pages
- Optional Google OAuth credentials
- Optional BuzzHeavier account ID

The container includes qBittorrent-nox, FFmpeg, 7-Zip, UnRAR, Deno, and the Python runtime.

## Installation

```bash
git clone https://github.com/hitesh920/Mirror-Bot.git
cd Mirror-Bot
cp .env.example .env
```

Fill the required values in `.env`, then start the bot:

```bash
docker compose up -d --build
docker compose logs -f bot
```

Check the running service:

```bash
docker compose ps
```

Only the `mirror-bot` container should run.

## Configuration

### Required

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `OWNER_ID` | Telegram user ID allowed to control the bot |
| `TELEGRAM_API_ID` | Telegram API application ID |
| `TELEGRAM_API_HASH` | Telegram API application hash |

### Optional

| Variable | Default | Description |
|---|---:|---|
| `TELEGRAM_DUMP_CHAT_ID` | Empty | Channel ID or username for Telegram uploads |
| `GOOGLE_DRIVE_FOLDER_ID` | Empty | Parent folder containing the four managed upload categories |
| `BUZZHEAVIER_ACCOUNT_ID` | Empty | BuzzHeavier account identifier |
| `TASK_LIMIT` | `10` | Maximum concurrent tasks |
| `STATUS_UPDATE_INTERVAL` | `10` | Telegram live-status interval in seconds |
| `TORRENT_SELECTION_PORT` | `8001` | Torrent selector port |
| `TORRENT_SELECTION_TIMEOUT` | `300` | Metadata/selection timeout in seconds |
| `PUBLIC_BASE_URL` | Auto-detected | Emergency override for generated public URLs |
| `TZ` | `Asia/Kolkata` | Container timezone |
| `ENABLE_TELEGRAM_UI` | `true` | Enable Telegram command handling |

### Telegram Dump Channel

1. Create a Telegram channel.
2. Add the bot as an administrator with permission to post.
3. Set `TELEGRAM_DUMP_CHAT_ID` to a numeric channel ID such as `-1001234567890` or a resolvable `@username`.

When configured, uploaded files go to the channel and the requesting chat receives only status and completion messages. If the channel is unavailable, PM fallback remains available when the task originated from Telegram.

## Google Drive

At startup, Mirror-Bot finds or creates `General`, `Movies`, `Series`, and
`Games` inside `GOOGLE_DRIVE_FOLDER_ID`. These folders are made publicly
readable, and every Telegram Drive upload asks which category to use.

Place these files beside `docker-compose.yml`:

```text
credentials.json
token.pickle
```

The Compose configuration mounts them at:

```text
/app/data/google/credentials.json
/app/data/google/token.pickle
```

Use the repository token-generation helper when a new OAuth token is required. Keep both files private and never commit them.

## Storage

Persistent runtime data:

```text
data/
├── downloads/   # temporary per-task workspaces
└── logs/        # rotating application logs
```

Completed files are delivered externally. Task workspaces are cleaned after success, failure, cancellation, and startup recovery.

## Operations

```bash
# Follow logs
docker compose logs -f bot

# Restart
docker compose restart bot

# Rebuild after an update
git pull --ff-only
docker compose build bot
docker compose up -d --no-deps --force-recreate bot

# Stop
docker compose down
```

Mirror-Bot forwards shutdown signals, cancels active work, closes temporary page servers, removes qBittorrent leftovers, and waits for cleanup before exit.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q
ruff check mirrorbot tests
docker compose config
```

The project keeps Telegram handlers thin and routes transfer work through shared task, downloader, processor, and delivery services.

## Security

- Keep `.env`, bot tokens, API credentials, OAuth files, cookies, and dump-channel details private.
- Temporary pages use random tokens but are still public to anyone who has the URL.
- Application logs redact secrets, magnets, authorization headers, and tokenized URLs.
- Google Drive sharing never changes permissions automatically.
- Run the bot only on infrastructure you control.
- Review source copyright and service terms before transferring content.

## License

See [LICENSE](LICENSE).
