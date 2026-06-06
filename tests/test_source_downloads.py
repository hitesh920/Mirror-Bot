import tempfile
import unittest
from pathlib import Path

from mirrorbot.downloaders.gdrive import downloaded_result, gdown_command, is_drive_folder
from mirrorbot.downloaders.process import path_size
from mirrorbot.models import SourceType
from mirrorbot.source_detector import detect_source


class SourceDownloadTests(unittest.TestCase):
    def test_source_detection(self):
        self.assertEqual(
            detect_source("https://drive.google.com/file/d/abc/view").type,
            SourceType.GOOGLE_DRIVE,
        )
        self.assertEqual(detect_source("remote:path/file").type, SourceType.RCLONE)
        self.assertEqual(detect_source("https://t.me/example/123").type, SourceType.UNSUPPORTED)

    def test_drive_folder_detection(self):
        self.assertTrue(is_drive_folder("https://drive.google.com/drive/folders/abc"))
        self.assertFalse(is_drive_folder("https://drive.google.com/file/d/abc/view"))
        self.assertEqual(
            gdown_command("https://drive.google.com/file/d/abc/view", Path("/tmp/task")),
            [
                "python",
                "-m",
                "mirrorbot.gdown_worker",
                "https://drive.google.com/file/d/abc/view",
                "/tmp/task/",
            ],
        )
        self.assertIn(
            "--folder",
            gdown_command("https://drive.google.com/drive/folders/abc", Path("/tmp/task")),
        )

    def test_downloaded_result_groups_multiple_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.txt").write_text("one")
            (root / "two.txt").write_text("two")
            result = downloaded_result(root, "")
            self.assertEqual(result.name, "Google Drive")
            self.assertEqual(path_size(result), 6)

    def test_drive_folder_with_one_file_stays_a_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.txt").write_text("one")
            result = downloaded_result(root, "", preserve_folder=True)
            self.assertTrue(result.is_dir())
            self.assertEqual((result / "one.txt").read_text(), "one")


if __name__ == "__main__":
    unittest.main()
