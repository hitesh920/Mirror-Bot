import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from mirrorbot.gdown_worker import content_length, folder_metadata


class DriveWorkerTests(unittest.TestCase):
    @patch("mirrorbot.gdown_worker._get_session")
    def test_content_length_follows_google_confirmation_page(self, get_session):
        confirmation = Mock()
        confirmation.headers = {"content-type": "text/html", "content-length": "100"}
        confirmation.text = '<a href="/uc?export=download&amp;confirm=x">download</a>'

        download = Mock()
        download.headers = {
            "content-type": "application/octet-stream",
            "content-disposition": 'attachment; filename="movie.mkv"',
            "content-length": "1048576",
        }
        session = Mock()
        session.get.side_effect = [confirmation, download]
        get_session.return_value = (session, "")

        self.assertEqual(content_length("file-id"), 1_048_576)
        self.assertEqual(session.get.call_count, 2)

    @patch("mirrorbot.gdown_worker._get_session")
    def test_content_length_rejects_unresolved_html_size(self, get_session):
        response = Mock()
        response.headers = {"content-type": "text/html", "content-length": "177500"}
        response.text = "not a confirmation page"
        session = Mock()
        session.get.return_value = response
        get_session.return_value = (session, "")

        self.assertEqual(content_length("file-id"), 0)

    @patch("mirrorbot.gdown_worker.content_length", side_effect=[100, 250])
    @patch("mirrorbot.gdown_worker.gdown.download_folder")
    def test_folder_metadata_aggregates_all_file_sizes(self, download_folder, length):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            download_folder.return_value = [
                Mock(id="one", local_path=str(root / "Movie" / "one.mkv")),
                Mock(id="two", local_path=str(root / "Movie" / "Extras" / "two.srt")),
            ]

            name, total = folder_metadata("https://drive.google.com/drive/folders/id", temp)

        self.assertEqual(name, "Movie")
        self.assertEqual(total, 350)
        self.assertEqual(length.call_count, 2)

    @patch("mirrorbot.gdown_worker.content_length", side_effect=[100, 0])
    @patch("mirrorbot.gdown_worker.gdown.download_folder")
    def test_folder_metadata_uses_unknown_total_if_any_size_is_unknown(
        self, download_folder, length
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            download_folder.return_value = [
                Mock(id="one", local_path=str(root / "Series" / "one.mkv")),
                Mock(id="two", local_path=str(root / "Series" / "two.mkv")),
            ]

            _, total = folder_metadata("https://drive.google.com/drive/folders/id", temp)

        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
