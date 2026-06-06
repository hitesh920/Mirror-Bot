import unittest
from pathlib import Path

from mirrorbot.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.status import format_status, progress_bar, task_status


def make_task() -> Task:
    return Task(
        "abcdef12-0000",
        1,
        1,
        1,
        Source(SourceType.DIRECT_URL, "https://example.com/file"),
        Destination.LOCAL_MOVIES,
        AddOptions(),
        Path("/tmp/task"),
        phase=TaskPhase.DOWNLOADING,
        name="file.bin",
    )


class StatusTests(unittest.TestCase):
    def test_progress_bar_is_always_ten_characters(self):
        self.assertEqual(progress_bar(0), "[----------]")
        self.assertEqual(progress_bar(0.1), "[#---------]")
        self.assertEqual(progress_bar(0.2), "[##--------]")
        self.assertEqual(progress_bar(0.3), "[###-------]")
        self.assertEqual(progress_bar(1), "[##########]")

    def test_known_size_layout(self):
        task = make_task()
        task.downloaded = 20
        task.size = 100
        task.progress = 0.2
        text = task_status(task)
        self.assertIn("<code>[##--------]</code> <b>20.0%</b>", text)
        self.assertIn("<b>Processed:</b>", text)
        self.assertIn("<b>Size:</b>", text)
        self.assertIn("<code>abcdef12</code>", text)

    def test_unknown_size_uses_empty_fixed_bar(self):
        task = make_task()
        task.downloaded = 20
        text = task_status(task)
        self.assertIn("<code>[----------]</code> <b>--</b>", text)
        self.assertIn("<code>Unknown</code>", text)

    def test_active_count_is_footer(self):
        self.assertTrue(format_status([make_task()]).endswith("<code>1</code>"))


if __name__ == "__main__":
    unittest.main()
