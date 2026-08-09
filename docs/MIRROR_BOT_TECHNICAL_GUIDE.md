# Mirror-Bot Technical Guide

This is the operational and engineering reference for
[`hitesh920/Mirror-Bot`](https://github.com/hitesh920/Mirror-Bot).

It documents the current `master` branch: how the service is structured, how
production is configured, how releases are deployed, and what must be verified
before a change is considered safe.

Mirror-Bot is a private, owner-controlled Telegram service. The repository is
public, so production credentials and operational data must never be committed.

## 1. System overview

Mirror-Bot accepts a transfer request through Telegram, downloads the source
into an isolated workspace, optionally processes it, delivers the result, and
then removes local temporary data.

The application supports:

- Direct HTTP and HTTPS downloads.
- Reply-based direct-link batches with separate-task and aggregate-ZIP modes.
- Magnet links and `.torrent` files.
- Selective torrent downloads through a temporary browser page.
- Replied Telegram documents, videos, audio, photos, and animations.
- yt-dlp-compatible video and audio sources.
- File-host and redirect resolvers.
- ZIP creation and archive extraction.
- Password-protected ZIP creation and extraction.
- Telegram and Cloudflare R2 delivery.

The production service runs in one Docker container named `mirror-bot`.

That container includes:

- Python 3.12.
- The asynchronous Mirror-Bot application.
- `qbittorrent-nox`.
- FFmpeg and ffprobe.
- 7-Zip and UnRAR.
- Deno for yt-dlp-compatible extractors that require it.

`start.sh` supervises both the Python application and qBittorrent. If either
process exits unexpectedly, the container exits and Docker applies the
configured restart policy.

## 2. Design priorities

The codebase is built around a few operational rules.

### Owner-only control

Telegram commands are restricted to `OWNER_ID`. Callback actions also verify
the requesting user before changing task state.

### Temporary local storage

Each task receives a UUID and a dedicated directory:

```text
/app/downloads/<task-id>/
```

The directory is deleted after completion, cancellation, or failure. Startup
recovery removes abandoned workspaces left by an unclean container stop.

### Explicit task state

A task moves through named phases such as:

- `queued`
- `fetching metadata`
- `selecting`
- `downloading`
- `processing`
- `scanning`
- `extracting`
- `archiving`
- `splitting`
- `uploading`
- `complete`
- `cancelled`
- `error`

Terminal tasks cannot be moved back into a non-terminal phase. The in-memory
registry retains at most 200 completed, cancelled, or failed task records.

### Bounded concurrency

`TASK_LIMIT` controls how many tasks may execute concurrently. Accepted tasks
wait for a semaphore slot when the limit is reached.

### Defensive cleanup

The shutdown path:

- Stops accepting new work.
- Requests cancellation for active tasks.
- Closes torrent selection pages.
- Waits for task cleanup.
- Removes qBittorrent state where necessary.
- Closes background services.
- Stops qBittorrent only after the bot has finished cleanup.

Cleanup failures are logged without preventing the remaining shutdown steps.

## 3. Code organization

The source tree is divided by responsibility.

### `mirrorbot/app.py`

Creates shared runtime objects, starts the Telegram client, registers command
handlers, validates the dump channel, and coordinates graceful shutdown.

### `mirrorbot/commands/`

Contains thin Telegram handlers:

- `add.py` parses `/add` requests and destination choices.
- `common.py` handles health, status, cancellation, logs, speed tests, and
  restart.
- `r2.py` handles R2 statistics, search, and confirmed deletion.

Business logic should remain outside command handlers.

### `mirrorbot/core/`

Contains:

- Environment configuration and validation.
- Task, source, destination, and phase models.
- `/add` option parsing.
- Source detection.
- Shared errors.
- Decimal size formatting.
- Structured, redacted logging.

### `mirrorbot/downloaders/`

Implements:

- Direct and collection downloads.
- Telegram file downloads.
- qBittorrent integration.
- Torrent metadata and file selection.
- yt-dlp video and audio downloads.
- Child-process monitoring and termination.

### `mirrorbot/resolvers/`

Resolves supported file hosts and redirect services before the download engine
starts. Resolver logging records the resolver and destination host without
writing full signed URLs into logs.

### `mirrorbot/services/`

Owns the reusable application logic:

- Task execution and lifecycle management.
- ZIP creation and extraction.
- File and path safety checks.
- Disk and stall protection.
- Telegram and R2 delivery.
- R2 usage analytics and retention.
- Media probing and thumbnail creation.
- Temporary public URL discovery.
- Startup recovery and restart state.
- Background task and runtime coordination.

### `mirrorbot/telegram/`

Contains user-facing messages, inline keyboards, expiring callback state, and
live status rendering.

### `scripts/`

`run_qbittorrent.py` starts qBittorrent with an isolated profile, captures its
temporary Web UI password, and keeps qBittorrent logs bounded.

## 4. Source handling

### Direct links

Direct downloads use `aiohttp`, bounded retry behavior, redirect support,
source-specific headers and cookies, filename sanitization, and streamed writes
to disk.

Resolved collections download up to three files concurrently while preserving
their relative folder structure. Duplicate target names are made unique.

### Batch links

`/add -b` extracts supported direct links from the replied message. A numeric
count fetches that exact consecutive message-ID range without searching past
missing messages. Collection preserves first-seen order, removes duplicates,
requires at least two valid links, and caps a batch at 20 links.

Separate mode creates ordinary independent tasks that share one selected
destination and use the global task queue. ZIP mode creates one visible parent
task, resolves and downloads up to three links concurrently in isolated child
workspaces, keeps successful results when another link fails, and creates a
store-only ZIP. Normal files are placed at the archive root with collision-safe
names; resolved collection folders keep their internal structure. Cancellation
stops child operations and removes their partial workspaces.

### File-host resolvers

The resolver layer currently includes support for services such as:

- MediaFire
- GoFile
- PixelDrain
- WeTransfer
- OneDrive
- 1fichier
- DoodStream
- Linkbox
- KrakenFiles
- Send.cm
- StreamTape
- pCloud
- Solidfiles
- Upload.ee
- Racaty
- Compatible redirect and shortener services

Resolvers are external-service integrations and can break when a provider
changes its website or API. Failures should be reported as resolver errors
rather than leaking low-level response details to users.

### Torrents

qBittorrent runs on `localhost:8080` inside the container. Its Web UI/API port
must never be published publicly.

Torrent handling:

- Adds the magnet or `.torrent`.
- Waits for metadata.
- Stops the torrent before selection.
- Opens a temporary tokenized selector on port `8001`.
- Applies file priorities.
- Downloads only selected files.
- Validates the final qBittorrent path against the task workspace.
- Removes skipped files and qBittorrent state.

Selection pages expire after `TORRENT_SELECTION_TIMEOUT`. The listener exists
only while at least one selection is active, so connection refusal while idle
is expected.

### Telegram files

The bot can download supported media from the message being replied to with
`/add`. A replied `.torrent` file is routed through the torrent engine.

### yt-dlp

yt-dlp sources offer:

- Video at 360p, 480p, 720p, or 1080p.
- MP3 audio at 64, 128, 192, 256, or 320 kbps.
- Playlist and multi-output handling.
- Safe custom output names constrained to the task workspace.

## 5. Processing and safety

### Archive handling

The bot supports archive extraction and ZIP creation. Passwords may be supplied
with `/add` options.

Extraction validates produced paths and rejects unsafe output. When extraction
fails, the original downloaded file is delivered and the completion message
contains a warning.

### Path validation

Inputs and engine results are constrained to the task workspace. Symlinked
upload trees are rejected.

Custom names are sanitized before becoming filesystem paths. qBittorrent
content paths are validated again before cleanup or delivery.

### Disk reserve

Before and during a transfer, the service preserves the larger of:

- Approximately 5.37 GB free disk space.
- 5 percent of the filesystem capacity.

A task is stopped with a clear disk-space error when continuing would violate
the reserve.

### Stall detection

Downloading and uploading tasks are monitored every five seconds. A task that
makes no byte or progress movement for ten minutes is cancelled as stalled.

## 6. Delivery behavior

### Telegram

Telegram delivery is media-aware.

The service:

- Detects video, audio, image, animation, or document delivery.
- Uses ffprobe metadata when available.
- Generates video thumbnails.
- Enables video streaming metadata.
- Splits files larger than 2,000,000,000 bytes.
- Tracks combined progress across every file and part.
- Handles Telegram FloodWait responses.
- Uploads to `TELEGRAM_DUMP_CHAT_ID` when configured.
- Falls back to the requesting private chat when the dump destination fails
  before any file is posted.

The completion message lists up to 20 uploaded files and their Telegram links.

### Cloudflare R2

The R2 bucket remains private.

Objects use this structure:

```text
<R2_PREFIX>/<task-id>/<relative-path>
```

Files of roughly 67 MB or larger use multipart upload with roughly 34 MB
parts. Active multipart uploads are aborted when a task is cancelled or fails.

Each uploaded object stores metadata describing:

- The Mirror-Bot task ID.
- Whether the object is a file or folder page.
- The original generated download link.
- The intended deletion timestamp when retention is enabled.

Single-file uploads return one download button.

Folder uploads create one private HTML landing page. That page:

- Lists every uploaded file.
- Displays only each file's basename, without its stored folder path.
- Uses each file's original stored link.
- Shows decimal KB, MB, and GB sizes.
- Includes a Copy all action using basenames rather than folder paths.

To normalize basename labels on folder pages created by an older release:

```bash
docker compose exec bot python scripts/update_r2_folder_pages.py
```

The migration preserves link and expiry metadata. It is idempotent and reports
only aggregate counts.

Generated links are signed for seven days. The default object retention is two
days, so object deletion is normally the effective access limit.

`/search <name>` returns links stored during upload and does not generate
replacements. `/search *` lists every current upload newest-first, groups a
folder as one result, and reports the combined size of its files.

The in-process expiry sweeper:

- Runs every hour.
- Lists only objects under `R2_PREFIX`.
- Caches folder-page expiry metadata until the object changes.
- Sends the configured owner one Telegram warning when an upload enters its
  final 12 hours. A folder and its objects produce one grouped warning.
- Stores hashed sent-warning markers in
  `logs/.r2-delete-warnings.json`, which is retained by the persistent logs
  volume so container restarts do not repeat a warning.
- Deletes objects older than `R2_AUTO_DELETE_SECONDS`.
- Does nothing when retention is set to `0`.

Because the sweep runs hourly, the warning normally arrives with between 11
and 12 hours remaining. If Telegram is unavailable, deletion continues and the
warning remains eligible for a later retry; a failed notification is not marked
as sent.

A matching two-day R2 lifecycle rule is recommended as a server-side fallback.
It should apply only to the bot-managed prefix.

`/delete` always requests confirmation. `/delete all` removes every object
under `R2_PREFIX` but preserves the bucket.

## 7. Configuration

Copy the example file before deployment:

```bash
cp .env.example .env
chmod 600 .env
```

### Required Telegram settings

- `BOT_TOKEN` is the BotFather token.
- `OWNER_ID` is the only Telegram user permitted to control the bot.
- `TELEGRAM_API_ID` is the Telegram application ID.
- `TELEGRAM_API_HASH` is the Telegram application hash.

These variables are required when `ENABLE_TELEGRAM_UI=true`.

### Optional Telegram settings

- `TELEGRAM_DUMP_CHAT_ID` selects a channel or chat for Telegram uploads. Use a
  numeric ID such as `-1001234567890` or a resolvable `@username`.
- `ENABLE_TELEGRAM_UI` defaults to `true`.

The bot must be allowed to post in the configured dump destination.

### Cloudflare R2 settings

R2 is enabled only when all of these are present:

- `R2_ENDPOINT_URL`
- `R2_BUCKET`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

Additional R2 settings:

- `R2_PREFIX` defaults to `uploads/`.
- `R2_AUTO_DELETE_SECONDS` defaults to `172800`.
- `CLOUDFLARE_ACCOUNT_ID` enables account usage data in `/r2stats`.
- `CLOUDFLARE_API_TOKEN` supplies read-only Billing and Account Analytics
  access for `/r2stats`.

The S3 credentials should be limited to Object Read & Write access on the
intended bucket. The analytics token should remain read-only.

### Runtime settings

- `TASK_LIMIT` defaults to `10` and must be at least `1`.
- `STATUS_UPDATE_INTERVAL` defaults to `10` seconds and must be at least `1`.
- `TORRENT_SELECTION_PORT` defaults to `8001` and must be a valid TCP port.
- `TORRENT_SELECTION_TIMEOUT` defaults to `300` seconds.
- `PUBLIC_BASE_URL` overrides automatic public-host detection.
- `TZ` defaults to `Asia/Kolkata`.

Invalid numeric settings stop startup with an explicit configuration error.

## 8. Networking

The Compose file publishes only:

```text
8001/tcp  Temporary torrent selector
```

qBittorrent uses `8080/tcp` internally and must not be exposed.

When `PUBLIC_BASE_URL` is empty, the service attempts to determine the public
host through several external IP services. If those fail, it uses the local
network address and finally `localhost`.

Public torrent selection requires:

- TCP `8001` allowed by the cloud network or security list.
- TCP `8001` allowed by the host firewall.
- TCP `8001` allowed by any `Docker-USER` firewall rules.
- The Compose port mapping active.
- A currently active selection listener.

Do not add a reverse proxy, placeholder server, or additional public port
without confirming the application's actual listener and threat model.

## 9. Deployment

### First deployment

```bash
git clone https://github.com/hitesh920/Mirror-Bot.git
cd Mirror-Bot

cp .env.example .env
mkdir -p data/downloads data/logs
chmod 600 .env

docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

Expected startup evidence includes:

- Container `mirror-bot` is running.
- Logs contain `BOT STARTED`.
- Logs contain `Starting Telegram UI`.
- The dump channel is confirmed reachable when configured.
- The configured owner receives a response to `/help`.
- Other Telegram users cannot control the bot.

### Routine update

Do not recreate the container during an important transfer.

```bash
git status --short
git pull --ff-only origin master
docker compose build --pull bot
docker compose up -d --no-deps --force-recreate bot
docker compose ps
docker compose logs --tail=100 bot
```

After deployment, verify:

- The VPS commit matches `origin/master`.
- The container restart count is stable.
- Both `python -m mirrorbot` and `qbittorrent-nox` are running.
- Telegram startup succeeds.
- Port `8001` remains the only published application port.

### Restart without rebuilding

```bash
docker compose restart bot
docker compose logs --tail=100 bot
```

### Stop

```bash
docker compose down
```

Compose allows 40 seconds for graceful shutdown and uses
`restart: unless-stopped`.

### Rollback

Before a production update, record the current commit:

```bash
git rev-parse HEAD
```

To roll back:

```bash
git switch --detach <known-good-commit>
docker compose up -d --build --force-recreate bot
docker compose logs --tail=100 bot
```

Return to normal tracking after the problem is resolved:

```bash
git switch master
git pull --ff-only origin master
```

## 10. Logging and diagnostics

Persistent application logs are written to:

```text
data/logs/bot.log
```

Application log behavior:

- UTC ISO timestamps.
- Structured task and failure fields.
- Approximately 5.24 MB per log file.
- Approximately 52.4 MB total retained application logs.
- Up to 20 rotated files.
- Seven-day retention.
- Reduced noise from dependencies.
- Redaction of common tokens, credentials, authorization headers, magnets,
  signed query parameters, and temporary-page tokens.

`/logs` exports the latest 2,000 sanitized lines.

Useful commands:

```bash
docker compose logs --tail=200 bot
docker compose logs -f bot
tail -n 200 data/logs/bot.log
grep 'event=task.failed' data/logs/bot.log
grep 'task=<short-id>' data/logs/bot.log
```

Redaction is a safety layer, not permission to publish logs without review.

### Common failures

If the container exits immediately:

- Inspect `docker compose logs bot`.
- Check required Telegram variables.
- Validate `.env` syntax with `docker compose config`.
- Confirm mounted paths are directories.

If the bot does not answer:

- Verify `OWNER_ID`.
- Verify Telegram token and API credentials.
- Confirm `ENABLE_TELEGRAM_UI=true`.
- Check outbound network access.

If a temporary selector URL fails:

- Confirm there is an active torrent selection.
- Check public TCP `8001`.
- Check `PUBLIC_BASE_URL`.
- Check cloud, host, and Docker firewall layers separately.

If a transfer fails:

- Search the log for its short task ID.
- Check `failure_category`.
- Check free disk space and filesystem permissions.
- Confirm the source URL has not expired.
- Check whether the external resolver has changed.

If abandoned data remains after a crash:

- Restart once to allow startup recovery.
- Stop the container before any manual cleanup.
- Never delete `data/downloads` while the bot is active.

## 11. Security model

The following rules are mandatory:

- Never commit `.env`.
- Never commit Telegram session files.
- Never commit logs, cookies, magnets, signed URLs, or temporary-page links.
- Keep `.env` readable only by the deployment user.
- Keep the R2 bucket private.
- Use bucket-scoped S3 credentials.
- Use a separate read-only Cloudflare analytics token.
- Never expose qBittorrent port `8080`.
- Treat download and selector links as bearer secrets.
- Review staged files before every push.
- Rotate exposed credentials immediately.

Back up only irreplaceable configuration and use encrypted storage. Downloads
are temporary data and application logs are operational data, not backups.

## 12. Development and validation

Mirror-Bot targets Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m compileall -q mirrorbot scripts
bash -n start.sh
docker compose config
docker compose build bot
```

When local dependencies are undesirable, run validation in an isolated Python
3.12 Docker container or on a non-production VPS workspace.

Transfer-related changes should test:

- One small source for each affected download engine.
- The affected delivery destination.
- Cancellation while downloading.
- Cancellation while uploading.
- A deliberate failure.
- Cleanup after every terminal outcome.
- Restart and shutdown behavior.
- Absence of secrets in staged files and logs.

R2 retention changes should also test the 12-hour warning boundary, one-warning
deduplication across restarts, folder grouping, notification failure retries,
and deletion behavior when Telegram is unavailable.

### Coding standards

- Keep command handlers owner-only and thin.
- Put reusable behavior in `services`, `downloaders`, or `resolvers`.
- Keep all task files inside the task workspace.
- Stream large files rather than loading them into memory.
- Preserve cancellation at every long-running boundary.
- Make cleanup idempotent.
- Add regression tests for every bug fix.
- Add new environment variables to `.env.example` and this guide.
- Do not expose a new port without documenting and reviewing it.

## 13. Release checklist

A release is ready when:

- The intended diff has been reviewed.
- No unrelated user files are staged.
- No credentials or signed links appear in the diff.
- pytest passes.
- Ruff lint and formatting pass.
- Python compilation passes.
- `start.sh` syntax passes.
- Compose configuration validates.
- The Docker image builds.
- A small affected transfer succeeds.
- Cancellation and failure clean up correctly.
- Active production transfers are finished before recreation.
- The deployment commit matches GitHub.
- The live container is stable after deployment.
- Startup logs contain no unexpected errors.
- A known-good rollback commit is recorded.

## 14. Recommended next improvements

### Reliability automation

Add GitHub Actions for:

- pytest
- Ruff lint and formatting
- Python compilation
- Compose validation
- Docker image build
- Secret scanning
- Dependency vulnerability checks

Pin production dependencies or generate a reproducible lock file. Add a
container health check that verifies both the bot process and qBittorrent.

### Upload management

Add `/uploads` or `/recent` with:

- The latest uploads grouped by task.
- File or folder type.
- Total size.
- Remaining retention time.
- Stored Download links.
- Confirmed Delete actions.

The command should reuse stored links and never generate new ones.

### Resumable R2 multipart uploads

Persist multipart upload IDs and completed part ETags so a large upload can
continue after a temporary failure or controlled restart.

### Clean private download URLs

An optional Cloudflare Worker on `workers.dev` could replace long S3-presigned
URLs with signed, opaque links while keeping the R2 bucket private.

Any implementation should:

- Stream the R2 body.
- Support HTTP Range requests.
- Use an HMAC signature.
- Avoid buffering large files.
- Disable caching.
- Return `404` after object deletion.
- Preserve stored-link behavior for `/search`.

### Persistent operational history

SQLite could preserve compact task outcomes and failure history across restarts.
This is useful for diagnostics, but a full web dashboard is unnecessary for the
current low-volume, temporary-sharing workload.

## 15. Documentation maintenance

Update this guide whenever a change affects:

- Commands.
- Environment variables.
- Public ports.
- Persistent data.
- Task lifecycle.
- Source or destination support.
- Cleanup and retention.
- Deployment or rollback.
- Security assumptions.

Keep the root [README](../README.md) concise and user-facing. Keep operational
details, engineering constraints, and release procedures here.
