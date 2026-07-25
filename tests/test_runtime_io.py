from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alpr_runner.runtime_io import (
    MAX_RUNTIME_RECORD_BYTES,
    RuntimeStorageError,
    atomic_json,
    prepare_private_directory,
    private_relative_path,
    protect_runtime_file,
    source_descriptor,
)


class PrivateDirectoryTests(unittest.TestCase):
    def test_directory_is_created_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = prepare_private_directory(Path(temporary) / "runtime")

            self.assertTrue(output.is_dir())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_existing_directory_mode_is_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            output.mkdir(mode=0o755)

            prepared = prepare_private_directory(output)

            self.assertEqual(prepared, output.resolve())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_symlink_leaf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeStorageError, "cannot prepare"):
                prepare_private_directory(linked)

    def test_symlinked_ancestor_is_rejected_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeStorageError, "cannot prepare"):
                prepare_private_directory(linked / "runtime")

            self.assertFalse((real / "runtime").exists())

    def test_filesystem_errors_are_normalized_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "private-name.txt"
            blocker.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(RuntimeStorageError) as raised:
                prepare_private_directory(blocker / "child")

            self.assertNotIn(str(blocker), str(raised.exception))


class AtomicRuntimeRecordTests(unittest.TestCase):
    def test_json_is_canonical_private_and_atomically_replaceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = prepare_private_directory(Path(temporary) / "runtime")
            path = output / "status.json"

            atomic_json(path, {"z": 1, "a": "first"})
            atomic_json(path, {"z": 2, "a": "second"})

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                '{\n  "a": "second",\n  "z": 2\n}\n',
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                [entry.name for entry in output.iterdir()],
                ["status.json"],
            )

    def test_symlink_and_fifo_destinations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = prepare_private_directory(Path(temporary) / "runtime")
            outside = Path(temporary) / "outside.json"
            outside.write_text("unchanged\n", encoding="utf-8")
            linked = output / "status.json"
            linked.symlink_to(outside)

            with self.assertRaisesRegex(RuntimeStorageError, "regular file"):
                atomic_json(linked, {"unsafe": True})
            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

            linked.unlink()
            fifo = output / "status.json"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(RuntimeStorageError, "regular file"):
                atomic_json(fifo, {"unsafe": True})

    def test_oversized_record_is_rejected_before_output_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = prepare_private_directory(Path(temporary) / "runtime")
            path = output / "status.json"

            with self.assertRaisesRegex(RuntimeStorageError, "exceeds"):
                atomic_json(path, {"payload": "x" * MAX_RUNTIME_RECORD_BYTES})

            self.assertFalse(path.exists())

    def test_parent_replacement_is_detected_after_pinned_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = prepare_private_directory(root / "runtime")
            moved = root / "runtime-original"
            real_write = os.write
            replaced = False

            def replacing_write(descriptor: int, payload: bytes) -> int:
                nonlocal replaced
                count = real_write(descriptor, payload)
                if not replaced:
                    replaced = True
                    os.replace(output, moved)
                    output.mkdir(mode=0o700)
                return count

            with (
                mock.patch(
                    "alpr_runner.runtime_io.os.write",
                    side_effect=replacing_write,
                ),
                self.assertRaisesRegex(RuntimeStorageError, "changed"),
            ):
                atomic_json(output / "status.json", {"safe": True})

            self.assertFalse((output / "status.json").exists())
            self.assertTrue((moved / "status.json").is_file())


class PublicationBoundaryTests(unittest.TestCase):
    def test_source_descriptors_never_return_path_or_url_values(self) -> None:
        samples = (
            source_descriptor("rtsp", "rtsp://person:secret@10.0.0.9/live"),
            source_descriptor("file", "/home/person/private/video.mp4"),
            source_descriptor("watch-file", "/private/frame.jpg"),
            source_descriptor("dir", "C:\\Users\\person\\frames"),
        )

        encoded = json.dumps(samples, sort_keys=True)
        for forbidden in (
            "person",
            "secret",
            "10.0.0.9",
            "/home/",
            "/private/",
            "Users",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(
            source_descriptor("device", 3), {"kind": "device", "device_index": 3}
        )

    def test_private_relative_path_cannot_escape_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = prepare_private_directory(root / "runtime")
            artifact = output / "camera" / "latest.jpg"
            artifact.parent.mkdir(mode=0o700)
            artifact.write_bytes(b"image")

            self.assertEqual(
                private_relative_path(artifact, output),
                "camera/latest.jpg",
            )
            outside = root / "outside.jpg"
            outside.write_bytes(b"outside")
            with self.assertRaisesRegex(RuntimeStorageError, "escaped"):
                private_relative_path(outside, output)

    def test_protect_runtime_file_requires_a_regular_non_symlink_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = prepare_private_directory(Path(temporary) / "runtime")
            artifact = output / "preview.jpg"
            artifact.write_bytes(b"image")

            protect_runtime_file(artifact)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

            linked = output / "linked.jpg"
            linked.symlink_to(artifact)
            with self.assertRaisesRegex(RuntimeStorageError, "cannot open"):
                protect_runtime_file(linked)
