from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from alpr_runner import media_probe


class ProbeSignalScopeTests(unittest.TestCase):
    def test_one_shot_restore_failure_is_retried_without_leaving_handler(
        self,
    ) -> None:
        selected = (signal.SIGINT, signal.SIGTERM)
        previous = {item: signal.getsignal(item) for item in selected}
        scope = media_probe._ProbeSignalScope()
        real_signal = signal.signal
        failed = False

        def fail_first_sigterm_restore(
            signum: signal.Signals,
            handler: object,
        ) -> object:
            nonlocal failed
            if (
                signum == signal.SIGTERM
                and handler == previous[signal.SIGTERM]
                and not failed
            ):
                failed = True
                raise RuntimeError("synthetic one-shot restore failure")
            return real_signal(signum, handler)

        try:
            scope.__enter__()
            with (
                mock.patch.object(
                    media_probe.signal,
                    "signal",
                    side_effect=fail_first_sigterm_restore,
                ),
                self.assertRaises(media_probe.MediaProbeError),
            ):
                scope.__exit__(None, None, None)

            self.assertTrue(failed)
            self.assertEqual(scope._previous, {})
            for item in selected:
                self.assertEqual(signal.getsignal(item), previous[item])
        finally:
            for item in selected:
                real_signal(item, previous[item])


class PinnedMediaProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = media_probe.run_reproducible_probe()
        cls.receipt = cls.result.receipt

    def test_closed_profile_delivers_the_exact_rgb_identity(self) -> None:
        raw = b"".join(self.result.frames)

        self.assertEqual(len(self.result.frames), 18)
        self.assertEqual(len(raw), media_probe.RGB_BYTES)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            media_probe.RGB_SHA256,
        )
        self.assertEqual(
            len(set(media_probe.iter_rgb_pixels(self.result.frames))),
            media_probe.RGB_UNIQUE_COLORS,
        )
        self.assertEqual(
            {
                index: hashlib.sha256(self.result.frames[index]).hexdigest()
                for index in (0, 8, 17)
            },
            {
                0: "5bb4d807877ca65727bf42991a735fd3970572f0b4a6788b1533e0bfbd2eb177",
                8: "505ed713c4a1df99ebe141471edcb6a8538378ea2f73f65374a51cd07995fb97",
                17: "3094c47fd64bbb03b0420e66f4b0ccbb7e6429545ec92f4da5acd243e9730aee",
            },
        )

    def test_receipt_proves_two_clean_supervised_runs(self) -> None:
        self.assertEqual(
            self.receipt["determinism"],
            {
                "byte_identical_rgb_runs": 2,
                "byte_identical_supervisor_receipts": 2,
                "byte_identical_y4m_renders": 2,
                "host_dispatch_disabled": True,
                "probe_runs": 2,
                "timestamp_fields": 0,
                "verified_bytes_reopened_by_path": False,
            },
        )
        self.assertEqual(
            self.receipt["supervisor"],
            {
                "cleanup_code": None,
                "exit_code": 0,
                "failure_code": None,
                "frame": {
                    "bytes_per_frame": 46_080,
                    "fps": 6,
                    "height": 96,
                    "pixel_format": "rgb24",
                    "width": 160,
                },
                "frames_delivered": 18,
                "process_group_closed": True,
                "process_reaped": True,
                "schema_version": 1,
                "source": {"kind": "synthetic"},
                "state": "ended",
                "stderr_bytes": 0,
                "stdout_bytes": 829_440,
                "termination": "none",
            },
        )

    def test_receipt_has_explicit_non_recognition_boundaries(self) -> None:
        self.assertEqual(
            self.receipt["boundary"],
            {
                "camera_used": False,
                "external_sdk_used": False,
                "performance_measured": False,
                "recognition_accuracy": "not_evaluated",
                "recognition_performed": False,
                "synthetic_media_used": True,
            },
        )
        self.assertEqual(
            self.receipt["runtime"]["command"],
            list(media_probe.NORMALIZED_COMMAND),
        )
        self.assertEqual(
            self.receipt["runtime"]["ffmpeg"]["execution_binding"],
            "write-sealed-memfd",
        )
        self.assertEqual(
            self.receipt["source"]["y4m"]["delivery_binding"],
            "write-sealed-inherited-fd",
        )

    def test_canonical_receipt_contains_no_private_runtime_identity(self) -> None:
        payload = self.result.canonical_receipt()
        decoded = json.loads(payload)

        self.assertEqual(decoded, self.receipt)
        self.assertTrue(payload.endswith(b"\n"))
        for forbidden in (
            b"/home/",
            b"/Users/",
            b"\\\\Users\\\\",
            b"media-probe-",
            os.uname().nodename.encode("utf-8"),
            b"rtsp://",
            b"timestamp_ns",
            b"stderr_text",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_private_workspaces_are_removed_after_the_probe(self) -> None:
        temporary_root = (
            media_probe.REPOSITORY
            / ".t"
            / media_probe._WORKSPACE_ROOT_NAME
        )
        leftovers = [
            path.name
            for path in temporary_root.iterdir()
            if path.name.startswith("media-probe-") and path.is_dir()
        ]

        self.assertEqual(leftovers, [])
        self.assertEqual(stat.S_IMODE(temporary_root.stat().st_mode), 0o700)


class MediaProbeCliTests(unittest.TestCase):
    def test_public_cli_emits_the_exact_canonical_receipt(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "tools/probe_media.py",
                "--json",
            ],
            cwd=media_probe.REPOSITORY,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"")
        decoded = json.loads(completed.stdout)
        self.assertEqual(completed.stdout, media_probe.canonical_json(decoded))
        self.assertEqual(decoded["decoded_rgb"]["sha256"], media_probe.RGB_SHA256)

    def test_cli_requires_the_bounded_json_mode(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", "tools/probe_media.py"],
            cwd=media_probe.REPOSITORY,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"error: select --json for the bounded public receipt\n",
        )

    def test_closed_stdout_fails_without_traceback_or_private_path(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", "tools/probe_media.py", "--json"],
            cwd=media_probe.REPOSITORY,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=lambda: os.close(1),
            check=False,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    @unittest.skipUnless(
        Path("/proc/self/task").exists(),
        "verified signal cleanup requires Linux procfs",
    )
    def test_sigterm_reaps_stopped_ffmpeg_and_removes_workspaces(self) -> None:
        workspace_root = (
            media_probe.REPOSITORY
            / ".t"
            / media_probe._WORKSPACE_ROOT_NAME
        )
        before = set(workspace_root.iterdir()) if workspace_root.exists() else set()
        process = subprocess.Popen(
            [sys.executable, "-S", "tools/probe_media.py", "--json"],
            cwd=media_probe.REPOSITORY,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child_pid: int | None = None
        try:
            deadline = time.monotonic() + 10.0
            children_path = (
                Path(f"/proc/{process.pid}/task")
                / str(process.pid)
                / "children"
            )
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    children = children_path.read_text(
                        encoding="ascii"
                    ).split()
                except FileNotFoundError:
                    children = []
                if children:
                    candidate = int(children[0])
                    try:
                        executable = os.readlink(f"/proc/{candidate}/exe")
                    except (FileNotFoundError, PermissionError):
                        time.sleep(0.001)
                        continue
                    if "memfd:pinned-ffmpeg-7.0.2" not in executable:
                        time.sleep(0.001)
                        continue
                    child_pid = candidate
                    try:
                        os.kill(child_pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        child_pid = None
                        continue
                    break
                time.sleep(0.001)
            self.assertIsNotNone(child_pid, "FFmpeg child was not observed")

            os.kill(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 1)
            self.assertEqual(stdout, b"")
            self.assertEqual(
                stderr,
                b"error: media evidence was interrupted safely\n",
            )
            assert child_pid is not None
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
            after = set(workspace_root.iterdir()) if workspace_root.exists() else set()
            self.assertEqual(after, before)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if child_pid is not None and Path(f"/proc/{child_pid}").exists():
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class PinnedRuntimeDefenseTests(unittest.TestCase):
    def _runtime_directory(self, root: Path) -> Path:
        directory = root / media_probe.FFMPEG_RELATIVE.parent
        directory.mkdir(parents=True)
        return directory

    def test_symlinked_executable_is_rejected_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._runtime_directory(root)
            target = root / "outside"
            target.write_bytes(b"not ffmpeg")
            target.chmod(0o700)
            (directory / media_probe.FFMPEG_RELATIVE.name).symlink_to(target)

            with self.assertRaisesRegex(
                media_probe.MediaProbeError,
                "not a regular file",
            ):
                media_probe._inspect_ffmpeg(root)

    def test_wrong_hash_and_unsafe_mode_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._runtime_directory(root)
            executable = directory / media_probe.FFMPEG_RELATIVE.name
            executable.write_bytes(b"not ffmpeg")
            executable.chmod(0o700)

            with self.assertRaisesRegex(
                media_probe.MediaProbeError,
                "identity is invalid",
            ):
                media_probe._inspect_ffmpeg(root)

            executable.chmod(0o722)
            with self.assertRaisesRegex(
                media_probe.MediaProbeError,
                "mode is unsafe",
            ):
                media_probe._inspect_ffmpeg(root)

    def test_ffmpeg_and_input_execution_descriptors_are_write_sealed(
        self,
    ) -> None:
        with (
            media_probe._prepare_sealed_ffmpeg(
                media_probe.REPOSITORY
            ) as ffmpeg,
            media_probe._sealed_payload(
                b"fixture",
                name="fixture-y4m",
                mode=0o400,
            ) as input_media,
        ):
            command = media_probe._real_command(ffmpeg, input_media)

            self.assertEqual(
                fcntl.fcntl(ffmpeg.fd, fcntl.F_GET_SEALS)
                & media_probe._REQUIRED_SEALS,
                media_probe._REQUIRED_SEALS,
            )
            self.assertEqual(
                fcntl.fcntl(input_media.fd, fcntl.F_GET_SEALS)
                & media_probe._REQUIRED_SEALS,
                media_probe._REQUIRED_SEALS,
            )
            self.assertEqual(
                command[0],
                media_probe.InheritedFdArgument(
                    ffmpeg.fd,
                    "/proc/self/fd/{fd}",
                ),
            )
            self.assertIn(
                media_probe.InheritedFdArgument(
                    input_media.fd,
                    "pipe:{fd}",
                ),
                command,
            )
            with self.assertRaises(OSError):
                os.write(ffmpeg.fd, b"x")
            with self.assertRaises(OSError):
                os.write(input_media.fd, b"x")

    def test_dedicated_workspace_does_not_chmod_existing_dot_t(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            temporary_root = root / ".t"
            temporary_root.mkdir()
            temporary_root.chmod(0o755)

            with media_probe._PrivateWorkspacePair(root) as workspaces:
                self.assertTrue(
                    all(
                        stat.S_IMODE(workspace.path.stat().st_mode) == 0o700
                        for workspace in workspaces
                    )
                )

            self.assertEqual(stat.S_IMODE(temporary_root.stat().st_mode), 0o755)
            dedicated = temporary_root / media_probe._WORKSPACE_ROOT_NAME
            self.assertEqual(stat.S_IMODE(dedicated.stat().st_mode), 0o700)
            self.assertEqual(list(dedicated.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
