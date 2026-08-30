import asyncio
import re
from pathlib import Path
from shutil import move, rmtree
from urllib.parse import urlparse

import aiohttp

from ..core.errors import TaskFailure
from ..core.models import AddOptions, Source, SourceType, Task
from ..resolvers import resolve_source
from ..resolvers.base import USER_AGENT, safe_name
from .direct import download_direct

BATCH_DOWNLOAD_CONCURRENCY = 3
URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


class BatchDownloadError(TaskFailure):
    category = "batch_download"


def _archive_stem(task: Task) -> str:
    name = safe_name(task.options.name, f"batch-{task.message_id}")
    return Path(name).stem if name.lower().endswith(".zip") else name


def _unique_target(root: Path, name: str) -> Path:
    candidate = root / name
    used = {item.name.lower() for item in root.iterdir()}
    index = 2
    while candidate.name.lower() in used:
        candidate = root / f"{Path(name).stem} ({index}){Path(name).suffix}"
        index += 1
    return candidate


def _failure_summary(index: int, source: Source, error: Exception) -> str:
    host = urlparse(source.value).hostname or "unknown host"
    detail = URL_IN_TEXT.sub("[link]", str(error)).replace("\n", " ").strip()
    if not detail:
        detail = type(error).__name__
    return f"Link {index} ({host}): {detail[:120]}"


async def download_batch(task: Task) -> Path:
    sources = task.source.metadata.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BatchDownloadError("Batch contains no download sources")

    task.batch_total = len(sources)
    root = task.work_dir / _archive_stem(task)
    parts = task.work_dir / ".batch-parts"
    root.mkdir(parents=True, exist_ok=True)
    parts.mkdir(parents=True, exist_ok=True)
    task.name = f"{root.name}.zip"
    task.begin_progress()
    semaphore = asyncio.Semaphore(BATCH_DOWNLOAD_CONCURRENCY)
    move_lock = asyncio.Lock()
    counter_lock = asyncio.Lock()
    jobs: list[asyncio.Task] = []

    def _child(index: int, source: Source) -> Task:
        return Task(
            id=f"{task.id}-{index}",
            user_id=task.user_id,
            chat_id=task.chat_id,
            message_id=task.message_id,
            source=source,
            destination=task.destination,
            options=AddOptions(),
            work_dir=parts / f"{index:02d}",
        )

    # Allocate every child up front so update_progress() iterates a list that
    # is never mutated by another coroutine (#3).
    children: list[Task] = [
        _child(index, source) for index, source in enumerate(sources, 1)
    ]

    resolver_session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})

    async def run_source(index: int, source: Source, child: Task) -> None:
        try:
            async with semaphore:
                if task.cancelled:
                    raise asyncio.CancelledError()
                child.source = await resolve_source(child.source, resolver_session)
                if child.source.type != SourceType.DIRECT_URL:
                    raise BatchDownloadError("resolved to a non-direct source")
                downloaded = await download_direct(child)
                if task.cancelled:
                    raise asyncio.CancelledError()
                async with move_lock:
                    target = _unique_target(root, downloaded.name)
                    await asyncio.to_thread(move, str(downloaded), str(target))
                async with counter_lock:
                    task.batch_completed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with counter_lock:
                task.batch_failed += 1
                task.processing_warnings.append(_failure_summary(index, source, exc))
        finally:
            await asyncio.to_thread(rmtree, child.work_dir, True)

    async def update_progress() -> None:
        while any(not job.done() for job in jobs):
            total = sum(child.downloaded for child in children)
            known_sizes = [child.size for child in children if child.size]
            size = sum(known_sizes) if len(known_sizes) == len(children) else 0
            task.report_progress(total, size=size)
            if not size:
                task.progress = (
                    task.batch_completed / task.batch_total if task.batch_total else 0
                )
                task.eta = 0
            active_names = [
                child.current_file or child.name
                for child in children
                if child.current_file or child.name
            ]
            if active_names:
                task.current_file = active_names[-1]
            await asyncio.sleep(0.5)

    jobs = [
        asyncio.create_task(run_source(index, source, children[index - 1]))
        for index, source in enumerate(sources, 1)
    ]
    progress_job = asyncio.create_task(update_progress())
    try:
        await asyncio.gather(*jobs)
    except BaseException:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise
    finally:
        progress_job.cancel()
        await asyncio.gather(progress_job, return_exceptions=True)
        task.report_progress(sum(child.downloaded for child in children))
        task.speed = 0
        task.eta = 0
        await asyncio.to_thread(rmtree, parts, True)
        await asyncio.gather(resolver_session.close(), return_exceptions=True)

    if task.batch_completed == 0:
        raise BatchDownloadError("Every link in the batch failed; nothing was uploaded")
    final_size = sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
    task.report_progress(final_size, size=final_size, complete=True)
    return root
