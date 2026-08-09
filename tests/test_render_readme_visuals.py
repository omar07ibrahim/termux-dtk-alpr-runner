from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from alpr_runner.synthetic import (
    canonical_trace_bytes,
    default_trace,
    render_ascii_summary,
    run_synthetic,
    write_result,
)
from alpr_runner.media_probe import canonical_json
from tools import evidence_media_codec as media_codec
from tools import render_readme_visuals as renderer


class CanonicalFixtureTests(unittest.TestCase):
    def test_fixture_is_byte_canonical_default_trace(self) -> None:
        fixture = (renderer.REPOSITORY / renderer.FIXTURE_RELATIVE).read_bytes()

        self.assertEqual(fixture, canonical_trace_bytes(default_trace()))
        self.assertEqual(
            json.loads(fixture),
            default_trace().to_json(),
        )

    def test_public_command_is_fixed_and_path_redacted(self) -> None:
        self.assertEqual(
            renderer.COMMAND_DISPLAY,
            (
                "python3.12",
                "-S",
                "-m",
                "alpr_runner.synthetic",
                "--trace",
                "examples/synthetic-events-v1.json",
                "--out",
                "PRIVATE-RUNTIME",
            ),
        )
        self.assertEqual(
            renderer.MEDIA_COMMAND_DISPLAY,
            (
                "python3.12",
                "-S",
                "tools/probe_media.py",
                "--json",
            ),
        )

    def test_output_inventory_has_explicit_evidence_kinds(self) -> None:
        self.assertEqual(
            set(renderer.OUTPUT_KINDS),
            renderer.EXPECTED_GENERATED_NAMES - {renderer.MANIFEST_NAME},
        )
        self.assertEqual(
            {kind for kind in renderer.OUTPUT_KINDS.values()},
            {
                "runtime-derived",
                "runtime-derived-normalized",
                "runtime-derived-lossless",
                "architecture-only",
                "workflow-only",
            },
        )
        self.assertEqual(
            set(renderer.OUTPUT_LANES),
            set(renderer.OUTPUT_KINDS),
        )
        self.assertEqual(
            {
                name
                for name, lanes in renderer.OUTPUT_LANES.items()
                if "synthetic_events" in lanes
            },
            renderer.EVENT_OUTPUT_NAMES,
        )
        self.assertEqual(
            {
                name
                for name, lanes in renderer.OUTPUT_LANES.items()
                if "synthetic_media" in lanes
            },
            renderer.MEDIA_OUTPUT_NAMES,
        )
        self.assertEqual(
            renderer.EVENT_OUTPUT_NAMES & renderer.MEDIA_OUTPUT_NAMES,
            {renderer.SETUP_SVG},
        )
        self.assertEqual(
            renderer.OUTPUT_LANES[renderer.SETUP_SVG],
            ("synthetic_events", "synthetic_media"),
        )
        self.assertEqual(len(renderer.EXPECTED_GENERATED_NAMES), 15)

    def test_source_snapshot_includes_every_vendor_independent_test(self) -> None:
        paths = {path.as_posix() for path in renderer._input_paths()}

        self.assertIn("docs/media-evidence.md", paths)
        self.assertIn("examples/synthetic-media-v1.json", paths)
        self.assertIn("requirements-media-evidence.lock", paths)
        self.assertIn("tests/test_runtime_io.py", paths)
        self.assertIn("tests/test_evidence_media_codec.py", paths)
        self.assertIn("tools/evidence_media_codec.py", paths)
        self.assertIn("tools/probe_media.py", paths)
        self.assertEqual(
            {
                path.relative_to(renderer.REPOSITORY).as_posix()
                for path in (renderer.REPOSITORY / "tests").glob("test_*.py")
            },
            {path for path in paths if path.startswith("tests/test_")},
        )
        self.assertEqual(
            {
                path.relative_to(renderer.REPOSITORY).as_posix()
                for path in (renderer.REPOSITORY / "tools").glob("*.py")
            },
            {path for path in paths if path.startswith("tools/")},
        )


class PublishedEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generated = renderer.GENERATED_DIRECTORY
        cls.result = json.loads((cls.generated / renderer.RUNTIME_RESULT).read_bytes())
        cls.media_receipt_payload = (
            cls.generated / renderer.MEDIA_RECEIPT
        ).read_bytes()
        cls.media_receipt = json.loads(cls.media_receipt_payload)
        cls.manifest = json.loads((cls.generated / renderer.MANIFEST_NAME).read_bytes())

    def test_committed_bundle_reproduces_byte_for_byte(self) -> None:
        self.assertEqual(renderer.main(["--check"]), 0)

    def test_manifest_inventory_is_exact_and_manifest_is_not_self_hashed(
        self,
    ) -> None:
        self.assertEqual(
            set(self.manifest["generated_tree"]),
            renderer.EXPECTED_GENERATED_NAMES,
        )
        self.assertEqual(self.manifest["schema_version"], 3)
        self.assertEqual(
            set(self.manifest["lanes"]),
            {"synthetic_events", "synthetic_media"},
        )
        self.assertEqual(
            self.manifest["manifest"],
            {
                "path": "docs/visuals/generated/manifest.sha256.json",
                "self_hash": "excluded-by-design",
                "written_last": True,
            },
        )
        output_paths = {Path(item["path"]).name for item in self.manifest["outputs"]}
        self.assertEqual(output_paths, set(renderer.OUTPUT_KINDS))
        self.assertNotIn(renderer.MANIFEST_NAME, output_paths)

    def test_manifest_hashes_every_nonmanifest_output(self) -> None:
        for record in self.manifest["outputs"]:
            path = renderer.REPOSITORY / record["path"]
            payload = path.read_bytes()
            self.assertEqual(record["bytes"], len(payload))
            self.assertEqual(
                record["sha256"],
                hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(
                record["evidence_kind"],
                renderer.OUTPUT_KINDS[path.name],
            )
            self.assertEqual(
                record["lanes"],
                list(renderer.OUTPUT_LANES[path.name]),
            )

    def test_manifest_input_snapshot_matches_current_sources(self) -> None:
        self.assertEqual(
            self.manifest["inputs"],
            list(renderer._snapshot_inputs()),
        )
        self.assertTrue(
            self.manifest["determinism"]["source_hashes_stable_before_and_after"]
        )
        event = self.manifest["lanes"]["synthetic_events"]["determinism"]
        self.assertEqual(
            event["byte_identical_cli_runs"],
            2,
        )
        self.assertTrue(event["stdout_matches_canonical_summary"])
        self.assertIn(
            "renderer-added",
            event["transcript_framing"],
        )
        media = self.manifest["lanes"]["synthetic_media"]
        self.assertEqual(
            media["boundary"],
            self.media_receipt["boundary"],
        )
        self.assertEqual(
            media["receipt_sha256"],
            hashlib.sha256(self.media_receipt_payload).hexdigest(),
        )
        self.assertEqual(
            set(self.manifest["lanes"]["synthetic_events"]["outputs"]),
            renderer.EVENT_OUTPUT_NAMES,
        )
        self.assertEqual(
            set(media["outputs"]),
            renderer.MEDIA_OUTPUT_NAMES,
        )
        self.assertEqual(
            media["determinism"]["byte_identical_public_cli_receipts"],
            2,
        )
        self.assertTrue(
            media["determinism"][
                "cli_receipt_equals_frame_capture_receipt"
            ]
        )
        self.assertTrue(
            media["determinism"]["lossless_rasters_from_decoded_frames"]
        )

    def test_runtime_artifact_states_the_non_recognition_boundary(self) -> None:
        self.assertEqual(self.result["backend"], "synthetic-events")
        self.assertFalse(self.result["recognition_performed"])
        self.assertEqual(
            self.result["recognition_accuracy"],
            "not_evaluated",
        )
        self.assertEqual(self.result["aggregation"]["event_count"], 5)
        self.assertEqual(
            self.result["aggregation"]["unique_token_count"],
            2,
        )
        self.assertEqual(self.result["trace"]["camera_count"], 2)

    def test_runtime_artifact_uses_only_fake_tokens_and_cameras(self) -> None:
        self.assertEqual(
            {event["token"] for event in self.result["events"]},
            {"SYNTH-01", "SYNTH-02"},
        )
        self.assertEqual(
            {event["camera"] for event in self.result["events"]},
            {"SYNTH-CAM-01", "SYNTH-CAM-02"},
        )

    def test_terminal_transcript_has_exact_stdout_and_declared_framing(
        self,
    ) -> None:
        transcript = (self.generated / renderer.TERMINAL_TRANSCRIPT).read_text(
            encoding="ascii"
        )

        expected = (
            "$ python3.12 -S -m alpr_runner.synthetic "
            "--trace examples/synthetic-events-v1.json "
            "--out PRIVATE-RUNTIME\n"
            + render_ascii_summary(self.result, renderer.RUNTIME_RESULT)
            + "# exit=0\n"
        )
        self.assertEqual(transcript, expected)

    def test_media_receipt_and_transcript_are_exact_public_cli_bytes(
        self,
    ) -> None:
        self.assertEqual(
            self.media_receipt_payload,
            canonical_json(self.media_receipt),
        )
        renderer._validate_media_receipt(self.media_receipt_payload)
        transcript = (
            self.generated / renderer.MEDIA_TRANSCRIPT
        ).read_bytes()
        expected = (
            b"$ "
            + b" ".join(
                item.encode("ascii")
                for item in renderer.MEDIA_COMMAND_DISPLAY
            )
            + b"\n"
            + self.media_receipt_payload
            + b"# exit=0\n"
        )
        self.assertEqual(transcript, expected)
        self.assertFalse(
            self.media_receipt["boundary"]["recognition_performed"]
        )
        self.assertEqual(
            self.media_receipt["boundary"]["recognition_accuracy"],
            "not_evaluated",
        )

    def test_contact_sheet_is_exact_frames_zero_eight_and_seventeen(
        self,
    ) -> None:
        payload = (
            self.generated / renderer.MEDIA_CONTACT_SHEET_PNG
        ).read_bytes()
        width, height, pixels = media_codec.decode_rgb_png(payload)

        self.assertEqual((width, height), (960, 192))
        frame_hashes = self.media_receipt["decoded_rgb"]["frame_sha256"]
        for panel, frame_index in enumerate((0, 8, 17)):
            recovered = bytearray()
            for source_y in range(96):
                for source_x in range(160):
                    output_x = panel * 320 + source_x * 2
                    output_y = source_y * 2
                    positions = [
                        ((output_y + y_delta) * width + output_x + x_delta)
                        * 3
                        for y_delta in (0, 1)
                        for x_delta in (0, 1)
                    ]
                    samples = {
                        pixels[position : position + 3]
                        for position in positions
                    }
                    self.assertEqual(len(samples), 1)
                    recovered.extend(samples.pop())
            self.assertEqual(
                hashlib.sha256(recovered).hexdigest(),
                frame_hashes[frame_index],
            )

    def test_lossless_gif_round_trips_all_receipt_frames_and_timing(
        self,
    ) -> None:
        payload = (self.generated / renderer.MEDIA_GIF).read_bytes()
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            renderer.PINNED_GIF_SHA256,
        )
        decoded = media_codec.decode_lossless_gif(payload)

        self.assertEqual((decoded.width, decoded.height), (160, 96))
        self.assertEqual(decoded.loop_count, 0)
        self.assertEqual(
            decoded.delays_cs,
            media_codec.GIF_DELAYS_CS,
        )
        self.assertEqual(sum(decoded.delays_cs), 300)
        self.assertEqual(
            [
                hashlib.sha256(frame).hexdigest()
                for frame in decoded.frames
            ],
            self.media_receipt["decoded_rgb"]["frame_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(b"".join(decoded.frames)).hexdigest(),
            self.media_receipt["decoded_rgb"]["sha256"],
        )
        self.assertLessEqual(len(payload), renderer.MAX_GIF_BYTES)

    def test_runtime_figures_contain_values_from_the_json_result(self) -> None:
        event_flow = (self.generated / renderer.EVENT_FLOW_SVG).read_text(
            encoding="utf-8"
        )
        zoom = (self.generated / renderer.ZOOM_GEOMETRY_SVG).read_text(encoding="utf-8")

        for token in self.result["aggregation"]["tokens"]:
            self.assertIn(
                f"{token['token']}: {token['event_count']} events",
                event_flow,
            )
        for camera, state in self.result["camera_zoom_state"].items():
            self.assertIn(camera, zoom)
            self.assertIn(
                f"zoom ratio {float(state['zoom_ratio']):.2f}×",
                zoom,
            )
        media_terminal = (
            self.generated / renderer.MEDIA_TERMINAL_SVG
        ).read_text(encoding="utf-8")
        media_architecture = (
            self.generated / renderer.MEDIA_ARCHITECTURE_SVG
        ).read_text(encoding="utf-8")
        decoded = self.media_receipt["decoded_rgb"]
        self.assertIn(decoded["sha256"], media_terminal)
        self.assertIn(
            self.media_receipt["runtime"]["ffmpeg"]["sha256"],
            media_terminal,
        )
        self.assertIn(
            f"{decoded['frame_count']} × RGB24",
            media_architecture,
        )
        self.assertIn(decoded["sha256"], media_architecture)

    def test_explanatory_figures_disclaim_runtime_proof(self) -> None:
        architecture = (self.generated / renderer.ARCHITECTURE_SVG).read_text(
            encoding="utf-8"
        )
        setup = (self.generated / renderer.SETUP_SVG).read_text(encoding="utf-8")
        media_architecture = (
            self.generated / renderer.MEDIA_ARCHITECTURE_SVG
        ).read_text(encoding="utf-8")
        media_failure = (
            self.generated / renderer.MEDIA_FAILURE_SVG
        ).read_text(encoding="utf-8")

        self.assertIn("ARCHITECTURE — explanatory diagram", architecture)
        self.assertIn("WORKFLOW — explanatory setup guide", setup)
        self.assertIn("not runtime proof", architecture)
        self.assertIn("not runtime proof", setup)
        self.assertIn(
            "ARCHITECTURE — explanatory diagram, not runtime proof",
            media_architecture,
        )
        self.assertIn(
            "FAILURE MODEL — clean branch runtime-verified",
            media_failure,
        )
        self.assertIn("failure branches explanatory", media_failure)

    def test_all_svgs_are_accessible_and_reference_free(self) -> None:
        namespace = "{http://www.w3.org/2000/svg}"
        for name in renderer.SVG_NAMES:
            with self.subTest(name=name):
                payload = (self.generated / name).read_bytes()
                renderer._validate_svg(name, payload)
                root = ET.fromstring(payload)
                self.assertEqual(root.get("role"), "img")
                self.assertEqual(len(root.findall(f"{namespace}title")), 1)
                self.assertEqual(len(root.findall(f"{namespace}desc")), 1)

    def test_published_bundle_passes_privacy_scan_and_public_modes(self) -> None:
        inventory = renderer._inventory_generated_tree(
            self.generated,
            allow_missing=False,
        )
        self.assertEqual(set(inventory), renderer.EXPECTED_GENERATED_NAMES)
        for name, status in inventory.items():
            with self.subTest(name=name):
                self.assertEqual(stat.S_IMODE(status.st_mode), 0o644)
                self.assertLessEqual(
                    status.st_size,
                    renderer._maximum_output_bytes(name),
                )
                renderer._privacy_scan(
                    name,
                    (self.generated / name).read_bytes(),
                )


class RendererDefenseTests(unittest.TestCase):
    def test_inventory_rejects_unexpected_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "expected.txt").write_text("ok", encoding="utf-8")
            (directory / "unexpected.txt").write_text("no", encoding="utf-8")

            with self.assertRaisesRegex(
                renderer.EvidenceError,
                "unexpected",
            ):
                renderer._inventory_generated_tree(
                    directory,
                    expected_names={"expected.txt"},
                    allow_missing=False,
                )

    def test_inventory_rejects_symlink_and_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target"
            target.write_text("target", encoding="utf-8")
            link = directory / "allowed"
            link.symlink_to(target)
            with self.assertRaisesRegex(
                renderer.EvidenceError,
                "regular files",
            ):
                renderer._inventory_generated_tree(
                    directory,
                    expected_names={"allowed", "target"},
                    allow_missing=False,
                )

        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                os.mkfifo(directory / "allowed", 0o600)
                with self.assertRaisesRegex(
                    renderer.EvidenceError,
                    "regular files",
                ):
                    renderer._inventory_generated_tree(
                        directory,
                        expected_names={"allowed"},
                        allow_missing=False,
                    )

    def test_relative_reader_rejects_symlinked_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.txt"
            target.write_text("safe", encoding="utf-8")
            (root / "linked.txt").symlink_to(target)

            with self.assertRaisesRegex(
                renderer.EvidenceError,
                "regular file",
            ):
                renderer._require_relative_file(
                    root,
                    Path("linked.txt"),
                    maximum_bytes=100,
                )

    def test_relative_reader_rejects_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (outside / "input.txt").write_text("outside", encoding="utf-8")
            (root / "linked").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(renderer.EvidenceError):
                renderer._require_relative_file(
                    root,
                    Path("linked/input.txt"),
                    maximum_bytes=100,
                )

    def test_relative_reader_rejects_real_ancestor_swap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside_temporary,
        ):
            root = Path(temporary)
            checked = root / "checked"
            checked.mkdir()
            (checked / "input.txt").write_text("inside", encoding="utf-8")
            outside = Path(outside_temporary)
            (outside / "input.txt").write_text("outside", encoding="utf-8")
            original = root / "checked-original"
            real_open = os.open
            swapped = False

            def racing_open(
                path: os.PathLike[str] | str,
                flags: int,
                mode: int = 0o600,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal swapped
                if path == "checked" and dir_fd is not None and not swapped:
                    checked.rename(original)
                    checked.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with (
                    mock.patch.object(
                        renderer.os,
                        "open",
                        side_effect=racing_open,
                    ),
                    self.assertRaises(renderer.EvidenceError),
                ):
                    renderer._require_relative_file(
                        root,
                        Path("checked/input.txt"),
                        maximum_bytes=100,
                    )
            finally:
                if checked.is_symlink():
                    checked.unlink()
                if original.exists():
                    original.rename(checked)

            self.assertTrue(swapped)

    def test_relative_reader_rejects_real_leaf_swap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside_temporary,
        ):
            root = Path(temporary)
            input_path = root / "input.txt"
            input_path.write_text("inside", encoding="utf-8")
            outside = Path(outside_temporary) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            original = root / "input-original.txt"
            real_open = os.open
            swapped = False

            def racing_open(
                path: os.PathLike[str] | str,
                flags: int,
                mode: int = 0o600,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal swapped
                if path == "input.txt" and dir_fd is not None and not swapped:
                    input_path.rename(original)
                    input_path.symlink_to(outside)
                    swapped = True
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with (
                    mock.patch.object(
                        renderer.os,
                        "open",
                        side_effect=racing_open,
                    ),
                    self.assertRaises(renderer.EvidenceError),
                ):
                    renderer._require_relative_file(
                        root,
                        Path("input.txt"),
                        maximum_bytes=100,
                    )
            finally:
                if input_path.is_symlink():
                    input_path.unlink()
                if original.exists():
                    original.rename(input_path)

            self.assertTrue(swapped)

    def test_bounded_runner_rejects_excess_output(self) -> None:
        with self.assertRaisesRegex(
            renderer.EvidenceError,
            "output limit",
        ):
            renderer._run_bounded(
                [
                    sys.executable,
                    "-S",
                    "-c",
                    "import sys; sys.stdout.write('x' * 10000)",
                ],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=True,
                stream_limit=100,
                timeout_seconds=2,
            )

    def test_bounded_runner_enforces_timeout(self) -> None:
        with self.assertRaisesRegex(renderer.EvidenceError, "timed out"):
            renderer._run_bounded(
                [
                    sys.executable,
                    "-S",
                    "-c",
                    "import time; time.sleep(2)",
                ],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=True,
                stream_limit=100,
                timeout_seconds=0.05,
            )

    def test_cleanup_signals_group_before_reaping_leader(self) -> None:
        process = mock.Mock()
        process.pid = 424242
        process.returncode = None
        owner = renderer._OwnedProcess(process)
        events: list[str] = []

        def record_wait(_pid: int, _flags: int) -> tuple[int, int]:
            events.append("wait")
            return (424242, 0)

        def record_signal(_group: int, _selected: signal.Signals) -> None:
            events.append("signal")

        with (
            mock.patch.object(owner, "observe", return_value=True) as observe,
            mock.patch.object(
                renderer.os,
                "killpg",
                side_effect=record_signal,
            ) as killpg,
            mock.patch.object(
                renderer.os,
                "waitpid",
                side_effect=record_wait,
            ) as waitpid,
        ):
            returncode = renderer._terminate_process_group(owner)

        self.assertEqual(returncode, 0)
        self.assertEqual(events, ["signal", "wait"])
        self.assertEqual(process.returncode, 0)
        self.assertTrue(owner.reaped)
        self.assertFalse(owner.signal_allowed)
        self.assertEqual(observe.call_count, 2)
        killpg.assert_called_once_with(424242, signal.SIGKILL)
        waitpid.assert_called_once_with(424242, 0)

    def test_ownership_loss_never_signals_numeric_process_group(self) -> None:
        process = mock.Mock()
        process.pid = 424242
        owner = renderer._OwnedProcess(process)

        with (
            mock.patch.object(
                renderer.os,
                "waitid",
                side_effect=ChildProcessError("externally reaped"),
            ),
            mock.patch.object(renderer.os, "killpg") as killpg,
            self.assertRaisesRegex(
                renderer._OwnershipLost,
                "ownership was lost",
            ),
        ):
            renderer._terminate_process_group(owner)

        self.assertTrue(owner.ownership_lost)
        self.assertFalse(owner.signal_allowed)
        killpg.assert_not_called()

    def test_bounded_runner_rejects_unsafe_sigchld_before_spawn(self) -> None:
        real_getsignal = renderer.signal.getsignal

        def unsafe_sigchld(selected: signal.Signals) -> object:
            if selected == signal.SIGCHLD:
                return signal.SIG_IGN
            return real_getsignal(selected)

        with (
            mock.patch.object(
                renderer.signal,
                "getsignal",
                side_effect=unsafe_sigchld,
            ),
            mock.patch.object(renderer.subprocess, "Popen") as popen,
            self.assertRaisesRegex(
                renderer.EvidenceError,
                "default SIGCHLD",
            ),
        ):
            renderer._run_bounded(
                [sys.executable, "-S", "-c", "pass"],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=True,
            )

        popen.assert_not_called()

    def test_bounded_runner_rejects_blocked_termination_signal(self) -> None:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGTERM},
        )
        try:
            with (
                mock.patch.object(renderer.subprocess, "Popen") as popen,
                self.assertRaisesRegex(
                    renderer.EvidenceError,
                    "termination signals to be unblocked",
                ),
            ):
                renderer._run_bounded(
                    [sys.executable, "-S", "-c", "pass"],
                    cwd=renderer.REPOSITORY,
                    env=renderer._demo_environment(),
                    fixed_command_no_detach=True,
                )
            popen.assert_not_called()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def test_signal_mask_enter_interruption_restores_exact_mask(self) -> None:
        original = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        source, first_line = inspect.getsourcelines(
            renderer._SignalMaskScope.__enter__
        )
        return_line = first_line + next(
            index
            for index, line in enumerate(source)
            if line.strip() == "return self"
        )

        def interrupt_at_return(
            frame: object,
            event: str,
            argument: object,
        ) -> object:
            del argument
            if (
                event == "line"
                and getattr(frame, "f_code", None)
                is renderer._SignalMaskScope.__enter__.__code__
                and getattr(frame, "f_lineno", None) == return_line
            ):
                sys.settrace(None)
                raise KeyboardInterrupt("before context adoption")
            return interrupt_at_return

        observed: set[signal.Signals] | None = None
        try:
            sys.settrace(interrupt_at_return)
            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "before context adoption",
            ):
                with renderer._SignalMaskScope({signal.SIGUSR1}):
                    self.fail("interrupted scope must not be entered")
        finally:
            sys.settrace(None)
            observed = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            signal.pthread_sigmask(signal.SIG_SETMASK, original)

        self.assertEqual(observed, original)

    def test_termination_scope_enter_interruption_restores_handlers(
        self,
    ) -> None:
        selected_signals = renderer._TerminationSignalScope._signals
        previous = {
            selected: signal.getsignal(selected)
            for selected in selected_signals
        }
        source, first_line = inspect.getsourcelines(
            renderer._TerminationSignalScope.__enter__
        )
        return_line = first_line + next(
            index
            for index, line in enumerate(source)
            if line.strip() == "return self"
        )

        def interrupt_at_return(
            frame: object,
            event: str,
            argument: object,
        ) -> object:
            del argument
            if (
                event == "line"
                and getattr(frame, "f_code", None)
                is renderer._TerminationSignalScope.__enter__.__code__
                and getattr(frame, "f_lineno", None) == return_line
            ):
                sys.settrace(None)
                raise KeyboardInterrupt("before handler-scope adoption")
            return interrupt_at_return

        observed: dict[signal.Signals, object] = {}
        try:
            sys.settrace(interrupt_at_return)
            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "before handler-scope adoption",
            ):
                with renderer._TerminationSignalScope():
                    self.fail("interrupted scope must not be entered")
        finally:
            sys.settrace(None)
            observed = {
                selected: signal.getsignal(selected)
                for selected in selected_signals
            }
            for selected, handler in previous.items():
                signal.signal(selected, handler)

        self.assertEqual(observed, previous)

    def test_subreaper_enable_interruption_rolls_back_state(self) -> None:
        state = {"enabled": False, "interrupted": False}

        def get_state() -> bool:
            return state["enabled"]

        def set_state(enabled: bool) -> None:
            state["enabled"] = enabled
            if enabled and not state["interrupted"]:
                state["interrupted"] = True
                raise KeyboardInterrupt("after successful enable")

        self.assertFalse(renderer._SUBREAPER_ACTIVE)
        with (
            mock.patch.object(
                renderer,
                "_get_child_subreaper",
                side_effect=get_state,
            ),
            mock.patch.object(
                renderer,
                "_set_child_subreaper",
                side_effect=set_state,
            ),
            self.assertRaisesRegex(
                KeyboardInterrupt,
                "successful enable",
            ),
        ):
            with renderer._ChildSubreaper():
                self.fail("interrupted scope must not be entered")

        self.assertFalse(state["enabled"])
        self.assertFalse(renderer._SUBREAPER_ACTIVE)

    def test_subreaper_disable_interruption_reconciles_state(self) -> None:
        state = {
            "enabled": False,
            "disable_interrupted": False,
        }

        def get_state() -> bool:
            return state["enabled"]

        def set_state(enabled: bool) -> None:
            state["enabled"] = enabled
            if not enabled and not state["disable_interrupted"]:
                state["disable_interrupted"] = True
                raise KeyboardInterrupt("after successful disable")

        self.assertFalse(renderer._SUBREAPER_ACTIVE)
        with (
            mock.patch.object(
                renderer,
                "_get_child_subreaper",
                side_effect=get_state,
            ),
            mock.patch.object(
                renderer,
                "_set_child_subreaper",
                side_effect=set_state,
            ),
            mock.patch.object(
                renderer,
                "_verify_waitable_child_status",
            ),
            self.assertRaisesRegex(
                KeyboardInterrupt,
                "successful disable",
            ),
        ):
            with renderer._ChildSubreaper():
                self.assertTrue(state["enabled"])

        self.assertFalse(state["enabled"])
        self.assertFalse(renderer._SUBREAPER_ACTIVE)

    def test_popen_interruption_after_spawn_is_contained(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = renderer.subprocess.Popen

        def spawn_then_interrupt(
            *args: object,
            **kwargs: object,
        ) -> subprocess.Popen[bytes]:
            process = real_popen(*args, **kwargs)
            processes.append(process)
            raise KeyboardInterrupt("after successful spawn")

        try:
            with (
                mock.patch.object(
                    renderer.subprocess,
                    "Popen",
                    side_effect=spawn_then_interrupt,
                ),
                self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "successful spawn",
                ),
            ):
                renderer._run_bounded(
                    [
                        sys.executable,
                        "-S",
                        "-c",
                        "import time;time.sleep(5)",
                    ],
                    cwd=renderer.REPOSITORY,
                    env=renderer._demo_environment(),
                    fixed_command_no_detach=True,
                )

            self.assertEqual(len(processes), 1)
            self.assertFalse(Path(f"/proc/{processes[0].pid}").exists())
            self.assertIsNotNone(processes[0].poll())
        finally:
            for process in processes:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_preexisting_child_is_rejected_without_being_signalled(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-S", "-c", "import time;time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with self.assertRaisesRegex(
                renderer.EvidenceError,
                "pre-existing child",
            ):
                renderer._run_bounded(
                    [sys.executable, "-S", "-c", "pass"],
                    cwd=renderer.REPOSITORY,
                    env=renderer._demo_environment(),
                    fixed_command_no_detach=True,
                )
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=2)

    def test_completed_nonzero_command_is_reaped_without_status_loss(
        self,
    ) -> None:
        completed = renderer._run_bounded(
            [sys.executable, "-S", "-c", "raise SystemExit(7)"],
            cwd=renderer.REPOSITORY,
            env=renderer._demo_environment(),
            fixed_command_no_detach=True,
        )

        self.assertEqual(completed.returncode, 7)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_selector_construction_failure_still_reaps_spawned_process(
        self,
    ) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = renderer.subprocess.Popen

        def capture_process(
            *args: object,
            **kwargs: object,
        ) -> subprocess.Popen[bytes]:
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.object(
                renderer.subprocess,
                "Popen",
                side_effect=capture_process,
            ),
            mock.patch.object(
                renderer.selectors,
                "DefaultSelector",
                side_effect=OSError("forced selector failure"),
            ),
            self.assertRaisesRegex(
                renderer.EvidenceError,
                "selector is unavailable",
            ),
        ):
            renderer._run_bounded(
                [sys.executable, "-S", "-c", "import time;time.sleep(30)"],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=True,
            )

        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(Path(f"/proc/{processes[0].pid}").exists())

    def test_primary_and_cleanup_failures_are_both_preserved(self) -> None:
        real_cleanup = renderer._terminate_process_group

        def cleanup_then_fail(
            owner: renderer._OwnedProcess,
        ) -> int:
            real_cleanup(owner)
            raise renderer.EvidenceError("forced cleanup failure")

        with (
            mock.patch.object(
                renderer,
                "_terminate_process_group",
                side_effect=cleanup_then_fail,
            ),
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            renderer._run_bounded(
                [sys.executable, "-S", "-c", "import time;time.sleep(30)"],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=True,
                timeout_seconds=0.05,
            )

        messages = [
            str(error)
            for error in raised.exception.exceptions
        ]
        self.assertTrue(any("timed out" in message for message in messages))
        self.assertTrue(
            any("forced cleanup failure" in message for message in messages)
        )

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(),
        "real process-group regression requires procfs",
    )
    def test_real_descendant_cannot_outlive_exited_leader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            child_code = (
                "import pathlib,subprocess,sys;"
                "child=subprocess.Popen("
                "[sys.executable,'-S','-c','import time;time.sleep(5)']);"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid),encoding='ascii')"
            )

            with self.assertRaisesRegex(renderer.EvidenceError, "timed out"):
                renderer._run_bounded(
                    [
                        sys.executable,
                        "-S",
                        "-c",
                        child_code,
                        str(pid_file),
                    ],
                    cwd=renderer.REPOSITORY,
                    env=renderer._demo_environment(),
                    fixed_command_no_detach=True,
                    timeout_seconds=1.0,
                    stream_limit=1024,
                )

            child_pid = int(pid_file.read_text(encoding="ascii"))
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(),
        "detached-child cleanup regression requires procfs",
    )
    def test_signal_aware_wrapper_reaps_detached_child_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            cleanup_file = Path(temporary) / "cleanup.ok"
            wrapper_code = (
                "import os,pathlib,signal,subprocess,sys,threading,time;"
                "child=subprocess.Popen("
                "[sys.executable,'-S','-c','import time;time.sleep(5)'],"
                "start_new_session=True);"
                "stop=threading.Event();"
                "signal.signal(signal.SIGTERM,lambda *_args:stop.set());"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid),encoding='ascii');"
                "stop.wait(5);"
                "os.killpg(child.pid,signal.SIGTERM);"
                "child.wait(timeout=1);"
                "pathlib.Path(sys.argv[2]).write_text("
                    "'clean',encoding='ascii')"
            )
            with self.assertRaisesRegex(
                renderer.EvidenceError,
                "timed out",
            ):
                renderer._run_bounded(
                    [
                        sys.executable,
                        "-S",
                        "-c",
                        wrapper_code,
                        str(pid_file),
                        str(cleanup_file),
                    ],
                    cwd=renderer.REPOSITORY,
                    env=renderer._demo_environment(),
                    fixed_command_no_detach=False,
                    signal_aware_wrapper=True,
                    timeout_seconds=1.0,
                    stream_limit=1024,
                )

            child_pid = int(pid_file.read_text(encoding="ascii"))
            self.assertEqual(
                cleanup_file.read_text(encoding="ascii"),
                "clean",
            )
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(),
        "parent-signal cleanup regression requires Linux procfs",
    )
    def test_sigterm_to_renderer_cleans_wrapper_and_detached_child(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            wrapper_pid_file = directory / "wrapper.pid"
            child_pid_file = directory / "child.pid"
            ready_file = directory / "ready"
            cleanup_file = directory / "cleanup.ok"
            wrapper_code = (
                "import os,pathlib,signal,subprocess,sys,threading;"
                "child=subprocess.Popen("
                "[sys.executable,'-S','-c','import time;time.sleep(5)'],"
                "stdin=subprocess.DEVNULL,"
                "stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL,"
                "start_new_session=True);"
                "stop=threading.Event();"
                "signal.signal(signal.SIGTERM,lambda *_args:stop.set());"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(os.getpid()),encoding='ascii');"
                "pathlib.Path(sys.argv[2]).write_text("
                "str(child.pid),encoding='ascii');"
                "pathlib.Path(sys.argv[3]).write_text("
                "'ready',encoding='ascii');"
                "stop.wait(5);"
                "os.killpg(child.pid,signal.SIGTERM);"
                "child.wait(timeout=1);"
                "pathlib.Path(sys.argv[4]).write_text("
                "'clean',encoding='ascii')"
            )
            driver_code = (
                "import pathlib,sys;"
                "from tools import render_readme_visuals as renderer;"
                "command=[sys.executable,'-S','-c',sys.argv[1],"
                "sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5]];"
                "\ntry:\n"
                " renderer._run_bounded("
                "command,cwd=renderer.REPOSITORY,"
                "env=renderer._demo_environment(),"
                "fixed_command_no_detach=False,"
                "signal_aware_wrapper=True,"
                "timeout_seconds=10,stream_limit=1024)\n"
                "except renderer.EvidenceError:\n"
                " raise SystemExit(23)\n"
                "raise SystemExit(24)"
            )
            driver = subprocess.Popen(
                [
                    sys.executable,
                    "-S",
                    "-c",
                    driver_code,
                    wrapper_code,
                    str(wrapper_pid_file),
                    str(child_pid_file),
                    str(ready_file),
                    str(cleanup_file),
                ],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            deadline = time.monotonic() + 3
            while not ready_file.exists():
                if driver.poll() is not None:
                    self.fail(
                        f"renderer driver exited before readiness: "
                        f"{driver.returncode}"
                    )
                if time.monotonic() >= deadline:
                    self.fail("renderer driver did not publish readiness")
                time.sleep(0.01)

            wrapper_pid = int(
                wrapper_pid_file.read_text(encoding="ascii")
            )
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
            os.kill(driver.pid, signal.SIGTERM)

            self.assertEqual(driver.wait(timeout=15), 23)
            self.assertEqual(
                cleanup_file.read_text(encoding="ascii"),
                "clean",
            )
            self.assertFalse(Path(f"/proc/{wrapper_pid}").exists())
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(),
        "subreaper regression requires Linux procfs",
    )
    def test_signal_aware_wrapper_contains_uncooperative_detached_child(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            wrapper_code = (
                "import pathlib,signal,subprocess,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "child=subprocess.Popen("
                "[sys.executable,'-S','-c',"
                "'import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(5)'],"
                "stdin=subprocess.DEVNULL,"
                "stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL,"
                "start_new_session=True);"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid),encoding='ascii');"
                "time.sleep(5)"
            )
            previous_subreaper = renderer._get_child_subreaper()
            with (
                mock.patch.object(
                    renderer,
                    "SIGNAL_AWARE_WRAPPER_CLEANUP_SECONDS",
                    0.05,
                ),
                self.assertRaises(BaseExceptionGroup) as raised,
            ):
                renderer._run_bounded(
                    [
                        sys.executable,
                        "-S",
                        "-c",
                        wrapper_code,
                        str(pid_file),
                    ],
                    cwd=renderer.REPOSITORY,
                    env=renderer._demo_environment(),
                    fixed_command_no_detach=False,
                    signal_aware_wrapper=True,
                    timeout_seconds=1.0,
                    stream_limit=1024,
                )

            messages = [
                str(error)
                for error in raised.exception.exceptions
            ]
            self.assertTrue(
                any("timed out" in message for message in messages)
            )
            self.assertTrue(
                any(
                    "did not complete nested cleanup" in message
                    for message in messages
                )
            )
            child_pid = int(pid_file.read_text(encoding="ascii"))
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
            self.assertEqual(
                renderer._get_child_subreaper(),
                previous_subreaper,
            )

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(),
        "subreaper regression requires Linux procfs",
    )
    def test_crashed_wrapper_cannot_orphan_detached_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            wrapper_code = (
                "import pathlib,subprocess,sys;"
                "child=subprocess.Popen("
                "[sys.executable,'-S','-c','import time;time.sleep(5)'],"
                "stdin=subprocess.DEVNULL,"
                "stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL,"
                "start_new_session=True);"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid),encoding='ascii');"
                "raise SystemExit(9)"
            )
            completed = renderer._run_bounded(
                [
                    sys.executable,
                    "-S",
                    "-c",
                    wrapper_code,
                    str(pid_file),
                ],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=False,
                signal_aware_wrapper=True,
                timeout_seconds=2,
                stream_limit=1024,
            )

            self.assertEqual(completed.returncode, 9)
            child_pid = int(pid_file.read_text(encoding="ascii"))
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_bounded_runner_requires_no_detach_contract(self) -> None:
        with self.assertRaisesRegex(
            renderer.EvidenceError,
            "no-detach",
        ):
            renderer._run_bounded(
                [sys.executable, "-S", "-c", "pass"],
                cwd=renderer.REPOSITORY,
                env=renderer._demo_environment(),
                fixed_command_no_detach=False,
            )

    def test_privacy_scan_rejects_identity_secret_and_runtime_metadata(
        self,
    ) -> None:
        cases = (
            b"/home/person/project",
            b"operator@example.invalid",
            b"api_key=private",
            b"rtsp://camera.invalid/live",
            b'{"confidence": 90}',
            b'{"vehicle_make": "example"}',
            b"github_" + b"pat_0123456789abcdefghijklmnop",
            b"AK" + b"IA0123456789ABCDEF",
        )
        for payload in cases:
            with (
                self.subTest(payload=payload),
                self.assertRaises(renderer.EvidenceError),
            ):
                renderer._privacy_scan("unsafe.txt", payload)

    def test_privacy_scan_distinguishes_product_words_from_host_identity(self) -> None:
        environment = {
            "USER": "runner",
            "LOGNAME": "runner",
            "SUDO_USER": "runner",
            "HOSTNAME": "build-node-123",
            "PORTFOLIO_SECRET": "private-environment-marker",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            renderer._privacy_scan(
                "safe.txt",
                b"Termux DTK ALPR Runner executes alpr_runner contracts.",
            )
            unsafe_payloads = (
                b'{"user":"runner"}',
                b"sudo_user=runner",
                b"runner@build-host",
                b"/tmp/runner/session",
                b"https://runner:8443/status",
                b'{"hostname":"build-node-123"}',
                b"host=build-node-123:8443",
                b"hostname=build-node-123.internal",
                b"prefix private-environment-marker suffix",
            )
            for payload in unsafe_payloads:
                with (
                    self.subTest(payload=payload),
                    self.assertRaises(renderer.EvidenceError),
                ):
                    renderer._privacy_scan("unsafe.txt", payload)

    def test_svg_validator_rejects_external_and_inaccessible_content(self) -> None:
        declaration = b'<?xml version="1.0" encoding="UTF-8"?>\n'
        inaccessible = (
            declaration + b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'viewBox="0 0 10 10"></svg>'
        )
        external = (
            declaration
            + b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" role="img" aria-labelledby="t d"><title id="t">T</title><desc id="d">D</desc><image href="https://example.invalid/a.png"/></svg>"""
        )
        event_handler = (
            declaration
            + b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" role="img" aria-labelledby="t d" onload="alert(1)"><title id="t">T</title><desc id="d">D</desc></svg>"""
        )
        stylesheet = (
            declaration
            + b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" role="img" aria-labelledby="t d"><title id="t">T</title><desc id="d">D</desc><style>@import url(https://example.invalid/a.css)</style></svg>"""
        )
        processing_instruction = (
            declaration
            + b'<?xml-stylesheet href="https://example.invalid/a.css"?>\n'
            + b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" role="img" aria-labelledby="t d"><title id="t">T</title><desc id="d">D</desc></svg>"""
        )

        with self.assertRaisesRegex(renderer.EvidenceError, "role=img"):
            renderer._validate_svg("bad.svg", inaccessible)
        with self.assertRaisesRegex(renderer.EvidenceError, "unsafe"):
            renderer._validate_svg("bad.svg", external)
        with self.assertRaisesRegex(renderer.EvidenceError, "unsafe"):
            renderer._validate_svg("bad.svg", event_handler)
        with self.assertRaisesRegex(renderer.EvidenceError, "unsafe"):
            renderer._validate_svg("bad.svg", stylesheet)
        with self.assertRaisesRegex(
            renderer.EvidenceError,
            "processing instruction",
        ):
            renderer._validate_svg("bad.svg", processing_instruction)

    def test_runtime_validator_rejects_self_reported_claims(self) -> None:
        forged = json.loads(
            (renderer.GENERATED_DIRECTORY / renderer.RUNTIME_RESULT).read_bytes()
        )
        forged["events"][0]["aggregate_count"] = 999
        forged["recognition_accuracy"] = 0.99
        forged["throughput_fps"] = 999
        forged["trace"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(
            renderer.EvidenceError,
            "accuracy claim|canonical production run",
        ):
            renderer._validate_runtime_result(forged)

    def test_demo_rejects_forged_stdout_with_genuine_artifact(self) -> None:
        def forged_runner(
            command: list[str],
            **_kwargs: object,
        ) -> renderer.CommandResult:
            write_result(
                Path(command[-1]),
                run_synthetic(default_trace()),
            )
            return renderer.CommandResult(
                returncode=0,
                stdout=b"FORGED DETERMINISTIC STDOUT\n",
                stderr=b"",
            )

        with (
            _TemporaryWorkspace() as workspace,
            mock.patch.object(
                renderer,
                "_run_bounded",
                side_effect=forged_runner,
            ),
            self.assertRaisesRegex(
                renderer.EvidenceError,
                "canonical result summary",
            ),
        ):
            renderer._run_demo_once(workspace, "run-1")

    def test_temp_root_swap_cannot_redirect_creation_or_cleanup(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside_temporary,
        ):
            root = Path(temporary)
            outside = Path(outside_temporary)
            pinned = renderer._prepare_private_temp_root(root)
            original = root / ".t-original"
            temporary_root = root / renderer.TEMP_RELATIVE
            temporary_root.rename(original)
            temporary_root.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaisesRegex(
                    renderer.EvidenceError,
                    "changed while in use",
                ):
                    pinned.make_directory("readme-stage-")
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                temporary_root.unlink()
                original.rename(temporary_root)
                pinned.close()

    def test_publication_replaces_manifest_last(self) -> None:
        payloads = {
            **{name: f"{name}\n".encode("ascii") for name in renderer.OUTPUT_KINDS},
            renderer.MANIFEST_NAME: b"manifest\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replaced: list[str] = []
            real_replace = os.replace

            def recording_replace(
                source: os.PathLike[str] | str,
                destination: os.PathLike[str] | str,
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                replaced.append(os.fspath(destination))
                real_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                mock.patch.object(renderer, "REPOSITORY", root),
                mock.patch.object(
                    renderer,
                    "GENERATED_DIRECTORY",
                    root / renderer.GENERATED_RELATIVE,
                ),
                mock.patch.object(
                    renderer.os,
                    "replace",
                    side_effect=recording_replace,
                ),
            ):
                renderer._publish(payloads)

            self.assertEqual(replaced[-1], renderer.MANIFEST_NAME)
            self.assertEqual(
                {path.name for path in (root / renderer.GENERATED_RELATIVE).iterdir()},
                renderer.EXPECTED_GENERATED_NAMES,
            )

    def test_publication_rejects_real_ancestor_swap(self) -> None:
        payloads = {
            **{name: f"{name}\n".encode("ascii") for name in renderer.OUTPUT_KINDS},
            renderer.MANIFEST_NAME: b"manifest\n",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside_temporary,
        ):
            root = Path(temporary)
            generated = root / renderer.GENERATED_RELATIVE
            generated.mkdir(parents=True)
            outside = Path(outside_temporary)
            (outside / "generated").mkdir()
            visuals = generated.parent
            original = visuals.with_name("visuals-original")
            real_open_chain = renderer._open_directory_chain
            destination_open_count = 0

            def racing_open_chain(
                path: Path,
            ) -> tuple[
                list[int],
                list[tuple[int, str, int, renderer._FileSnapshot]],
            ]:
                nonlocal destination_open_count
                if Path(path) == generated:
                    destination_open_count += 1
                    if destination_open_count == 2:
                        visuals.rename(original)
                        visuals.symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                return real_open_chain(path)

            try:
                with (
                    mock.patch.object(renderer, "REPOSITORY", root),
                    mock.patch.object(
                        renderer,
                        "GENERATED_DIRECTORY",
                        generated,
                    ),
                    mock.patch.object(
                        renderer,
                        "_open_directory_chain",
                        side_effect=racing_open_chain,
                    ),
                    self.assertRaises(renderer.EvidenceError),
                ):
                    renderer._publish(payloads)
            finally:
                if visuals.is_symlink():
                    visuals.unlink()
                if original.exists():
                    original.rename(visuals)

            self.assertEqual(
                list((outside / "generated").iterdir()),
                [],
            )


class _TemporaryWorkspace:
    def __enter__(self) -> renderer._PinnedTemporaryDirectory:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = renderer._prepare_private_temp_root(Path(self.temporary.name))
        self.workspace = self.root.make_directory("readme-evidence-")
        return self.workspace

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.workspace.remove()
        self.root.close()
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
