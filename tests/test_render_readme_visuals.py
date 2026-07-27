from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
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
                "architecture-only",
                "workflow-only",
            },
        )

    def test_source_snapshot_includes_every_vendor_independent_test(self) -> None:
        paths = {path.as_posix() for path in renderer._input_paths()}

        self.assertIn("docs/media-evidence.md", paths)
        self.assertIn("requirements-media-evidence.lock", paths)
        self.assertIn("tests/test_runtime_io.py", paths)
        self.assertEqual(
            {
                path.relative_to(renderer.REPOSITORY).as_posix()
                for path in (renderer.REPOSITORY / "tests").glob("test_*.py")
            },
            {path for path in paths if path.startswith("tests/test_")},
        )


class PublishedEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generated = renderer.GENERATED_DIRECTORY
        cls.result = json.loads((cls.generated / renderer.RUNTIME_RESULT).read_bytes())
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

    def test_manifest_input_snapshot_matches_current_sources(self) -> None:
        self.assertEqual(
            self.manifest["inputs"],
            list(renderer._snapshot_inputs()),
        )
        self.assertTrue(
            self.manifest["determinism"]["source_hashes_stable_before_and_after"]
        )
        self.assertEqual(
            self.manifest["determinism"]["byte_identical_cli_runs"],
            2,
        )
        self.assertTrue(
            self.manifest["determinism"]["stdout_matches_canonical_summary"]
        )
        self.assertIn(
            "renderer-added",
            self.manifest["determinism"]["transcript_framing"],
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

    def test_explanatory_figures_disclaim_runtime_proof(self) -> None:
        architecture = (self.generated / renderer.ARCHITECTURE_SVG).read_text(
            encoding="utf-8"
        )
        setup = (self.generated / renderer.SETUP_SVG).read_text(encoding="utf-8")

        self.assertIn("ARCHITECTURE — explanatory diagram", architecture)
        self.assertIn("WORKFLOW — explanatory setup guide", setup)
        self.assertIn("not runtime proof", architecture)
        self.assertIn("not runtime proof", setup)

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
                mode: int = 0o777,
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
                mode: int = 0o777,
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

    def test_cleanup_signals_group_even_after_leader_exit(self) -> None:
        process = mock.Mock()
        process.pid = 424242
        process.poll.return_value = 0
        process.wait.return_value = 0

        with mock.patch.object(renderer.os, "killpg") as killpg:
            renderer._terminate_process_group(process)

        killpg.assert_called_once_with(424242, signal.SIGKILL)
        process.wait.assert_called_once_with(timeout=renderer.PROCESS_CLEANUP_SECONDS)

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
                "[sys.executable,'-S','-c','import time;time.sleep(30)']);"
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
                    timeout_seconds=0.2,
                    stream_limit=1024,
                )

            child_pid = int(pid_file.read_text(encoding="ascii"))
            state = ""
            for _attempt in range(100):
                try:
                    fields = (
                        Path(f"/proc/{child_pid}/stat")
                        .read_text(encoding="ascii")
                        .split()
                    )
                except FileNotFoundError:
                    state = "gone"
                    break
                state = fields[2]
                if state == "Z":
                    break
                time.sleep(0.01)
            self.assertIn(state, {"gone", "Z"})

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
            b"github_pat_0123456789abcdefghijklmnop",
            b"AKIA0123456789ABCDEF",
        )
        for payload in cases:
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
