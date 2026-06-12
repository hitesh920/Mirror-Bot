from pathlib import Path

import pytest

from mirrorbot.core.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    local = tmp_path / "local"
    local.mkdir()
    downloads = tmp_path / "work"
    downloads.mkdir()
    return Config(
        bot_token="test", owner_id=1, telegram_api_id=1,
        telegram_api_hash="test", local_download_root=local,
        google_drive_folder_id="", task_limit=1, status_update_interval=10,
        public_base_url="http://127.0.0.1:8000", torrent_selection_port=18000,
        torrent_selection_timeout=1, jellyfin_api_key="", tmdb_api_key="",
        download_dir=downloads,
    )
