import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .paths import ensure_inside, local_category_root

LOGGER = logging.getLogger(__name__)
EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})E(\d{1,3})\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
NOISE_RE = re.compile(
    r"(?ix)\b(?:2160p|1080p|720p|480p|web[- .]?dl|webrip|bluray|brrip|hdtv|"
    r"x26[45]|h26[45]|hevc|av1|aac|ddp?\d(?:\.\d)?|atmos|proper|repack|remux)\b.*$"
)
SITE_RE = re.compile(r"\[[^\]]+\]")
SEPARATORS_RE = re.compile(r"[._]+")
SPACE_RE = re.compile(r"\s+")
INVALID_PATH_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TMDB_CACHE: dict[tuple[str, str, str], tuple[str, str, float, int | None]] = {}


@dataclass(frozen=True)
class MediaMatch:
    media_type: str
    title: str
    year: str = ""
    season: int | None = None
    episode: int | None = None
    confidence: float = 0.0
    tmdb_id: int | None = None

    @property
    def folder_name(self) -> str:
        value = f"{self.title} ({self.year})" if self.year else self.title
        return INVALID_PATH_RE.sub(" ", value).strip(" .") or "Unknown media"


def clean_release_title(name: str) -> tuple[str, str, int | None, int | None]:
    base = Path(name).stem
    base = SITE_RE.sub(" ", base)
    episode_match = EPISODE_RE.search(base)
    season = int(episode_match.group(1)) if episode_match else None
    episode = int(episode_match.group(2)) if episode_match else None
    if episode_match:
        base = base[: episode_match.start()]
    year_match = YEAR_RE.search(base)
    year = year_match.group(1) if year_match else ""
    if year_match:
        base = base[: year_match.start()]
    base = NOISE_RE.sub(" ", base)
    base = SEPARATORS_RE.sub(" ", base)
    base = re.sub(r"[- ]+$", "", base)
    base = SPACE_RE.sub(" ", base).strip()
    title = base or Path(name).stem
    return title, year, season, episode


def resolve_media(name: str, category: str, api_key: str) -> MediaMatch:
    title, parsed_year, season, episode = clean_release_title(name)
    media_type = "tv" if category == "series" else "movie"
    fallback = MediaMatch(media_type, title, parsed_year, season, episode)
    cache_key = (media_type, title.casefold(), parsed_year)
    cached = TMDB_CACHE.get(cache_key)
    if cached:
        official, year, confidence, tmdb_id = cached
        return MediaMatch(media_type, official, year, season, episode, confidence, tmdb_id)
    if not api_key or not title:
        return fallback
    query = urlencode({"api_key": api_key, "query": title, "include_adult": "false"})
    request = Request(
        f"https://api.themoviedb.org/3/search/{media_type}?{query}",
        headers={"User-Agent": "MirrorBot/1.0", "Accept": "application/json"},
    )
    results = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=8) as response:
                import json
                results = json.load(response).get("results", [])
            break
        except Exception as exc:
            LOGGER.warning(
                "TMDb lookup attempt=%s failed title=%r type=%s error=%s",
                attempt + 1, title, media_type, type(exc).__name__,
            )
            if attempt < 2:
                time.sleep(attempt + 1)
    if results is None:
        return fallback
    normalized = title.casefold()
    best = None
    best_score = 0.0
    for result in results[:10]:
        candidate = result.get("name" if media_type == "tv" else "title") or ""
        original = result.get("original_name" if media_type == "tv" else "original_title") or ""
        score = max(
            SequenceMatcher(None, normalized, candidate.casefold()).ratio(),
            SequenceMatcher(None, normalized, original.casefold()).ratio(),
        )
        candidate_date = result.get("first_air_date" if media_type == "tv" else "release_date") or ""
        if parsed_year and len(candidate_date) >= 4 and candidate_date[:4] != parsed_year:
            score -= 0.20
        if score > best_score:
            best, best_score = result, score
    if not best or best_score < 0.72:
        LOGGER.info("TMDb match uncertain title=%r score=%.2f", title, best_score)
        return fallback
    official = best.get("name" if media_type == "tv" else "title") or title
    date = best.get("first_air_date" if media_type == "tv" else "release_date") or ""
    year = date[:4] if len(date) >= 4 else parsed_year
    LOGGER.info("TMDb matched title=%r official=%r year=%s score=%.2f", title, official, year, best_score)
    TMDB_CACHE[cache_key] = (official, year, best_score, best.get("id"))
    return MediaMatch(media_type, official, year, season, episode, best_score, best.get("id"))



def media_identity_name(downloaded: Path, category: str) -> str:
    if downloaded.is_file():
        return downloaded.name
    files = [item for item in downloaded.rglob("*") if item.is_file() and not item.is_symlink()]
    if category == "series":
        for item in files:
            if clean_release_title(item.name)[2] is not None:
                return item.name
    if files:
        return max(files, key=lambda item: item.stat().st_size).name
    return downloaded.name


def media_target(root: Path, category: str, downloaded: Path, match: MediaMatch) -> Path:
    category_root = local_category_root(root, category)
    target = category_root / match.folder_name
    if not match.year:
        title = match.folder_name.casefold()
        candidates = [
            item for item in category_root.iterdir()
            if item.is_dir() and not item.is_symlink()
            and (item.name.casefold() == title or item.name.casefold().startswith(f"{title} ("))
        ]
        if len(candidates) == 1:
            target = candidates[0]
            LOGGER.info("Reusing existing canonical folder title=%r target=%s", match.title, target)
    if category == "series" and match.season is not None:
        target /= f"Season {match.season:02d}"
    ensure_inside(root, target)
    return target


def apply_media_permissions(root: Path, target: Path | None = None) -> None:
    root = root.resolve()
    target = (target or root).resolve()
    ensure_inside(root, target)
    root_stat = root.stat()
    items = [target]
    if target.is_dir():
        items.extend(target.rglob("*"))
    for item in items:
        if item.is_symlink():
            continue
        try:
            os.chown(item, root_stat.st_uid, root_stat.st_gid)
            os.chmod(item, 0o775 if item.is_dir() else 0o664)
        except OSError:
            LOGGER.exception("Could not apply media permissions path=%s", item)


def migrate_library(root: Path, tmdb_api_key: str, dry_run: bool = True) -> dict[str, int]:
    stats = {"moved": 0, "skipped": 0, "uncertain": 0, "conflicts": 0}
    for category in ("movies", "series"):
        category_root = local_category_root(root, category)
        plans: list[tuple[Path, Path, Path]] = []
        reserved_targets: set[Path] = set()
        for old_folder in list(category_root.iterdir()):
            if not old_folder.is_dir() or old_folder.name.startswith("."):
                continue
            folder_plans: list[tuple[Path, Path, Path]] = []
            uncertain = False
            conflict = False
            for source in [item for item in old_folder.rglob("*") if item.is_file()]:
                match = resolve_media(source.name, category, tmdb_api_key)
                if match.confidence < 0.72:
                    uncertain = True
                    LOGGER.warning("Migration uncertain source=%s parsed_title=%r", source, match.title)
                    break
                target = media_target(root, category, source, match) / source.name
                if target.resolve(strict=False) == source.resolve():
                    continue
                if target.exists() or target in reserved_targets or any(plan[2] == target for plan in folder_plans):
                    conflict = True
                    LOGGER.warning("Migration conflict source=%s target=%s", source, target)
                    break
                folder_plans.append((old_folder, source, target))
            if uncertain:
                stats["uncertain"] += 1
                continue
            if conflict:
                stats["conflicts"] += 1
                continue
            if not folder_plans:
                stats["skipped"] += 1
                continue
            plans.extend(folder_plans)
            reserved_targets.update(plan[2] for plan in folder_plans)
            LOGGER.info("Migration %s source=%s files=%s", "preview" if dry_run else "plan", old_folder, len(folder_plans))

        if dry_run:
            stats["moved"] += len(plans)
            continue
        for old_folder, source, target in plans:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            stats["moved"] += 1
            LOGGER.info("Migrated media source=%s target=%s", source, target)
            apply_media_permissions(root, target.parent)
        for old_folder in {plan[0] for plan in plans}:
            for folder in sorted((item for item in old_folder.rglob("*") if item.is_dir()), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
            try:
                old_folder.rmdir()
            except OSError:
                LOGGER.info("Migration kept non-empty source folder=%s", old_folder)
    return stats
