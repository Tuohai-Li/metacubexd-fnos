from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.build_fpk import FpkBuildError, audit_fpk


def archive_bytes(files: dict[str, bytes], *, compressed: bool = True) -> bytes:
    stream = io.BytesIO()
    mode = "w:gz" if compressed else "w"
    with tarfile.open(fileobj=stream, mode=mode) as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def valid_fpk_bytes(*, checksum: str | None = None) -> bytes:
    app_archive = archive_bytes(
        {
            "fnos_server.py": b"#!/usr/bin/env python3\n",
            "upstream-version": b"1.273.0\n",
            "ui/config": b"{}\n",
            "www/index.html": b"<html></html>\n",
        }
    )
    app_md5 = checksum or hashlib.md5(app_archive).hexdigest()
    return archive_bytes(
        {
            "app.tgz": app_archive,
            "manifest": f"appname = metacubexd\nchecksum = {app_md5}\n".encode(),
            "cmd/main": b"#!/bin/bash\n",
            "config/privilege": b"{}\n",
            "config/resource": b"{}\n",
            "wizard/config": b"[]\n",
            "ICON.PNG": b"png",
            "ICON_256.PNG": b"png",
        }
    )


class FpkAuditTests(unittest.TestCase):
    def test_accepts_gzip_outer_archive_with_matching_app_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "valid.fpk"
            package.write_bytes(valid_fpk_bytes())
            audit = audit_fpk(package)
        self.assertEqual(len(str(audit["sha256"])), 64)
        self.assertEqual(len(str(audit["app_md5"])), 32)

    def test_rejects_plain_tar_outer_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "plain-tar.fpk"
            package.write_bytes(archive_bytes({"app.tgz": b"invalid"}, compressed=False))
            with self.assertRaisesRegex(FpkBuildError, "not gzip-compressed"):
                audit_fpk(package)

    def test_rejects_mismatched_app_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "bad-checksum.fpk"
            package.write_bytes(valid_fpk_bytes(checksum="0" * 32))
            with self.assertRaisesRegex(FpkBuildError, "checksum does not match"):
                audit_fpk(package)


if __name__ == "__main__":
    unittest.main()
