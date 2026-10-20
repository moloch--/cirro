#!/usr/bin/env python3
"""Focused pre-execution checks for the standalone release smoke tool."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_graphqlite_release as smoke


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.binary = self.directory / "cirro"
        self.binary.write_bytes(b"packaged binary")
        self.metadata = {
            "version": "1.3.0", "target": "aarch64-apple-darwin",
            "cirro_revision": "a" * 40, "graphqlite_revision": "b" * 40,
            "binary_sha256": smoke.sha256(self.binary),
            "embedded_extension_sha256": "c" * 64,
            "features": ["azure", "tailscale", "graph", "graphqlite"],
        }

    def write_metadata(self):
        (self.directory / "build-info.json").write_text(json.dumps(self.metadata))

    def test_archive_corruption_is_rejected(self):
        archive = self.directory / "package.tar.xz"
        archive.write_bytes(b"archive content")
        checksum = archive.with_name(archive.name + ".sha256")
        checksum.write_text(smoke.sha256(archive) + "  " + archive.name + "\n")
        smoke.verify_archive_checksum(archive)
        archive.write_bytes(b"changed archive")
        with self.assertRaisesRegex(AssertionError, "checksum does not match"):
            smoke.verify_archive_checksum(archive)

    def test_binary_corruption_is_rejected(self):
        self.write_metadata()
        self.binary.write_bytes(b"changed binary")
        with self.assertRaisesRegex(AssertionError, "binary checksum"):
            smoke.verify_build_info(self.binary)

    def test_metadata_types_and_features_are_enforced(self):
        for field, value in (("cirro_revision", 42), ("target", None),
                             ("features", ["graph", "graphqlite"]),
                             ("features", ["azure", "tailscale", "graph", "graphqlite", {}])):
            with self.subTest(field=field, value=value):
                original = self.metadata[field]
                self.metadata[field] = value
                self.write_metadata()
                with self.assertRaises(AssertionError):
                    smoke.verify_build_info(self.binary)
                self.metadata[field] = original

    def test_expected_build_identity_is_enforced(self):
        self.write_metadata()
        smoke.verify_build_info(self.binary, "aarch64-apple-darwin", "a" * 40)
        for target, revision in (("x86_64-apple-darwin", "a" * 40),
                                 ("aarch64-apple-darwin", "b" * 40)):
            with self.subTest(target=target, revision=revision):
                with self.assertRaisesRegex(AssertionError, "differs from expected"):
                    smoke.verify_build_info(self.binary, target, revision)

    def test_runtime_overrides_and_toolchain_path_are_excluded(self):
        with patch.dict(os.environ, {"CIRRO_GRAPHQLITE_EXTENSION": "/build/graphqlite.so",
                                   "LD_LIBRARY_PATH": "/build", "DYLD_LIBRARY_PATH": "/build",
                                   "PATH": "/build/mingw/bin", "AWS_SECRET_ACCESS_KEY": "synthetic"}):
            environment = smoke.isolated_environment(self.directory / "cache", self.directory / "tmp")
        self.assertNotIn("/build", environment["PATH"])
        for key in ("CIRRO_GRAPHQLITE_EXTENSION", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(key, environment)

    def test_result_comparison_preserves_types_and_list_order(self):
        self.assertNotEqual(smoke.encoded(True), smoke.encoded(1))
        self.assertNotEqual(smoke.encoded(1), smoke.encoded(1.0))
        self.assertNotEqual(smoke.encoded([True]), smoke.encoded("[true]"))
        self.assertNotEqual(smoke.encoded([1, 2]), smoke.encoded([2, 1]))


if __name__ == "__main__":
    unittest.main()
