from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.sync_upstream import sync_repository
from tests.test_fnos_server import FakeGithubHandler, RunningServer, make_ui, ui_archive


class SyncUpstreamTests(unittest.TestCase):
    def test_sync_is_idempotent(self) -> None:
        archive = ui_archive("sync")
        FakeGithubHandler.archive = archive
        FakeGithubHandler.archive_digest = hashlib.sha256(archive).hexdigest()
        FakeGithubHandler.release_version = "2.3.4"
        github = ThreadingHTTPServer(("127.0.0.1", 0), FakeGithubHandler)
        with tempfile.TemporaryDirectory() as temporary, RunningServer(github):
            root = Path(temporary)
            make_ui(root / "app/www", "old")
            (root / "app/upstream-version").write_text("1.0.0\n")
            (root / "manifest").write_text("appname = metacubexd\nversion = 1.0.0\n")
            api = f"http://127.0.0.1:{github.server_address[1]}"

            first = sync_repository(root, version="2.3.4", api_base=api)
            second = sync_repository(root, version="2.3.4", api_base=api)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual((root / "app/upstream-version").read_text(), "2.3.4\n")
            self.assertIn("version = 2.3.4", (root / "manifest").read_text())
            self.assertIn("sync", (root / "app/www/index.html").read_text())


if __name__ == "__main__":
    unittest.main()
