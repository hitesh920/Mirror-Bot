import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from mirrorbot.downloaders.direct import download_direct
from mirrorbot.models import AddOptions, Destination, Source, SourceType, Task
from mirrorbot.resolvers import resolve_source
from mirrorbot.resolvers.base import (
    ResolvedCollection,
    ResolvedDownload,
    ResolvedFile,
    ResolverError,
    resolved_source,
    safe_name,
    safe_relative_path,
)
from mirrorbot.resolvers.direct_hosts import (
    KrakenFilesResolver,
    PCloudResolver,
    SendCmResolver,
    SolidFilesResolver,
    StreamTapeResolver,
    UploadEeResolver,
)
from mirrorbot.resolvers.gofile import GoFileResolver
from mirrorbot.resolvers.mediafire import MediaFireResolver, extract_mediafire_link
from mirrorbot.resolvers.onedrive import OneDriveResolver, onedrive_ids
from mirrorbot.resolvers.ouo import OuoResolver, extract_csrf
from mirrorbot.resolvers.pixeldrain import PixelDrainResolver
from mirrorbot.resolvers.redirects import RedirectResolver
from mirrorbot.resolvers.wetransfer import WeTransferResolver, transfer_parts
from mirrorbot.source_detector import detect_source


class ResolverTests(unittest.TestCase):
    def test_resolver_host_routing(self):
        self.assertTrue(MediaFireResolver().supports("https://www.mediafire.com/file/x"))
        self.assertTrue(PixelDrainResolver().supports("https://pixeldrain.com/u/x"))
        self.assertTrue(WeTransferResolver().supports("https://we.tl/t-x"))
        self.assertTrue(OneDriveResolver().supports("https://1drv.ms/u/s!x"))
        self.assertTrue(RedirectResolver().supports("https://bit.ly/x"))
        self.assertTrue(OuoResolver().supports("https://ouo.io/x"))
        self.assertTrue(GoFileResolver().supports("https://gofile.io/d/x"))
        self.assertTrue(SolidFilesResolver().supports("https://solidfiles.com/v/x"))
        self.assertTrue(UploadEeResolver().supports("https://www.upload.ee/files/1/x"))
        self.assertTrue(StreamTapeResolver().supports("https://streamtape.com/v/x"))
        self.assertTrue(PCloudResolver().supports("https://u.pcloud.link/x"))
        self.assertTrue(SendCmResolver().supports("https://send.cm/d/x"))
        self.assertTrue(KrakenFilesResolver().supports("https://krakenfiles.com/view/x"))
        self.assertFalse(PixelDrainResolver().supports("https://pixeldrain.com/api/file/x"))

    def test_mediafire_download_link_extraction(self):
        page = (
            '<a aria-label="Download file" '
            'href="https://download1.mediafire.com/a/b/file.zip">download</a>'
        )
        self.assertEqual(
            extract_mediafire_link(page),
            "https://download1.mediafire.com/a/b/file.zip",
        )

    def test_wetransfer_parts(self):
        self.assertEqual(
            transfer_parts("https://wetransfer.com/downloads/transfer/security"),
            ("transfer", "security"),
        )

    def test_onedrive_ids(self):
        self.assertEqual(
            onedrive_ids("https://onedrive.live.com/?resid=ABC!123&authkey=!KEY"),
            ("ABC!123", "!KEY"),
        )

    def test_resolved_source_preserves_metadata(self):
        source = Source(
            SourceType.DIRECT_URL,
            "https://bit.ly/x",
            metadata={"headers": {"A": "1"}, "cookies": {"a": "1"}},
        )
        resolved = resolved_source(
            source,
            ResolvedDownload(
                "https://example.com/file",
                headers={"B": "2"},
                cookies={"b": "2"},
            ),
            "redirect",
        )
        self.assertEqual(resolved.metadata["original_url"], source.value)
        self.assertEqual(resolved.metadata["headers"], {"A": "1", "B": "2"})
        self.assertEqual(resolved.metadata["cookies"], {"a": "1", "b": "2"})

    def test_resolved_collection_is_attached_to_source(self):
        collection = ResolvedCollection(
            "Folder",
            [ResolvedFile("https://example.com/a", "a.bin", "sub", 10)],
        )
        source = resolved_source(
            Source(SourceType.DIRECT_URL, "https://example.com/share"),
            collection,
            "test",
        )
        self.assertEqual(source.filename, "Folder")
        self.assertIs(source.metadata["collection"], collection)
        self.assertEqual(collection.total_size, 10)

    def test_resolved_collection_rejects_unsafe_paths(self):
        with self.assertRaises(ResolverError):
            safe_relative_path("../outside")
        self.assertEqual(safe_name("..", "fallback"), "fallback")

    def test_ouo_csrf_extraction(self):
        self.assertEqual(
            extract_csrf('<input name="_token" value="abc">'),
            "abc",
        )

    def test_resolver_hosts_are_detected_as_direct_urls(self):
        urls = [
            "https://www.mediafire.com/file/x",
            "https://pixeldrain.com/u/x",
            "https://gofile.io/d/x",
            "https://ouo.io/x",
            "https://streamtape.com/v/x",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(detect_source(url).type, SourceType.DIRECT_URL)


class AsyncResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_resolution_stops_after_one_resolver(self):
        class CollectionResolver:
            name = "collection-test"
            calls = 0

            def supports(self, _url):
                return True

            async def resolve(self, _url, _session):
                self.calls += 1
                return ResolvedCollection(
                    "Folder",
                    [ResolvedFile("https://example.com/a", "a.bin", size=1)],
                )

        resolver = CollectionResolver()
        with patch("mirrorbot.resolvers.RESOLVERS", (resolver,)):
            source = await resolve_source(
                Source(SourceType.DIRECT_URL, "https://example.com/share")
            )
        self.assertEqual(resolver.calls, 1)
        self.assertIn("collection", source.metadata)

    async def test_collection_downloader_preserves_tree_and_aggregates_progress(self):
        app = web.Application()

        async def file_a(_request):
            return web.Response(body=b"abc")

        async def file_b(_request):
            return web.Response(body=b"defg")

        app.router.add_get("/a", file_a)
        app.router.add_get("/b", file_b)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        collection = ResolvedCollection(
            "Folder",
            [
                ResolvedFile(f"http://127.0.0.1:{port}/a", "a.bin", "one", 3),
                ResolvedFile(f"http://127.0.0.1:{port}/b", "b.bin", "two", 4),
                ResolvedFile(f"http://127.0.0.1:{port}/a", "a.bin", "one", 3),
            ],
        )
        with tempfile.TemporaryDirectory() as temp:
            source = Source(
                SourceType.DIRECT_URL,
                "https://example.com/share",
                "Folder",
                {"collection": collection},
            )
            task = Task(
                "task-id",
                1,
                1,
                1,
                source,
                Destination.LOCAL_MOVIES,
                AddOptions(),
                Path(temp),
            )
            result = await download_direct(task)
            self.assertEqual((result / "one" / "a.bin").read_bytes(), b"abc")
            self.assertEqual((result / "one" / "a (2).bin").read_bytes(), b"abc")
            self.assertEqual((result / "two" / "b.bin").read_bytes(), b"defg")
            self.assertEqual(task.downloaded, 10)
            self.assertEqual(task.progress, 1)
        await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
