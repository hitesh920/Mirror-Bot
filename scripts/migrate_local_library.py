import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mirrorbot.core.config import Config
from mirrorbot.core.logging_config import setup_logging
from mirrorbot.services.jellyfin_api import JellyfinApi
from mirrorbot.services.media_library import apply_media_permissions, migrate_library

parser = argparse.ArgumentParser()
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
setup_logging()
config = Config.load()
stats = migrate_library(config.local_download_root, config.tmdb_api_key, dry_run=not args.apply)
if args.apply:
    apply_media_permissions(config.local_download_root)
    try:
        JellyfinApi(config.jellyfin_api_key).scan_library()
    except Exception as exc:
        logging.getLogger(__name__).warning("Jellyfin scan after migration failed: %s", type(exc).__name__)
logging.getLogger(__name__).info("Migration complete apply=%s stats=%s", args.apply, stats)
print(stats)
