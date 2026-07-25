from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from alpr_runner.synthetic import (
    RESULT_FILENAME,
    TRACE_MAX_BYTES,
    TraceValidationError,
    decode_trace,
    default_trace,
    main,
    read_trace,
    render_ascii_summary,
    run_synthetic,
    validate_trace,
    write_result,
)


class DefaultSyntheticRunTests(unittest.TestCase):
    def test_default_trace_is_deterministic_and_cross_camera(self) -> None:
        trace = default_trace()
        first = run_synthetic(trace)
        second = run_synthetic(trace)

        self.assertEqual(first, second)
        self.assertEqual(first["backend"], "synthetic-events")
        self.assertEqual(first["recognition_accuracy"], "not_evaluated")
        self.assertFalse(first["recognition_performed"])
        self.assertEqual(first["trace"]["camera_count"], 2)
        self.assertEqual(first["trace"]["event_count"], 5)
        self.assertEqual(
            first["trace"]["sha256"],
            "1da6eebe4db756e94d9d9a2c47a6c6f3f40aeeb23c72b94b494c895b35cc06f4",
        )
        self.assertEqual(first["aggregation"]["event_count"], 5)
        self.assertEqual(first["aggregation"]["unique_token_count"], 2)
        self.assertEqual(
            [
                (item["token"], item["event_count"])
                for item in first["aggregation"]["tokens"]
            ],
            [("SYNTH-01", 3), ("SYNTH-02", 2)],
        )
        self.assertEqual(
            first["events"][2]["seen_by_cameras"],
            ["SYNTH-CAM-01", "SYNTH-CAM-02"],
        )
        self.assertGreater(
            first["camera_zoom_state"]["SYNTH-CAM-01"]["zoom_ratio"],
            1.0,
        )
        self.assertGreater(
            first["camera_zoom_state"]["SYNTH-CAM-02"]["zoom_ratio"],
            1.0,
        )

    def test_result_has_no_recognition_claim_or_host_source_metadata(self) -> None:
        encoded = json.dumps(run_synthetic(default_trace()), sort_keys=True)

        self.assertIn('"recognition_accuracy": "not_evaluated"', encoded)
        self.assertIn('"recognition_performed": false', encoded)
        self.assertNotIn("rtsp://", encoded)
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("vehicle_make", encoded)
        self.assertNotIn("confidence", encoded)

    def test_ascii_summary_is_concise_explicit_and_ascii_only(self) -> None:
        summary = render_ascii_summary(run_synthetic(default_trace()))

        summary.encode("ascii")
        self.assertIn("backend=synthetic-events\n", summary)
        self.assertIn("recognition_accuracy=not_evaluated\n", summary)
        self.assertIn("recognition_performed=false\n", summary)
        self.assertIn("input=validated synthetic events (no images)\n", summary)
        self.assertIn(
            "SYNTH-01 events=3 cameras=SYNTH-CAM-01,SYNTH-CAM-02\n",
            summary,
        )

    def test_result_is_written_at_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            result = run_synthetic(default_trace())

            artifact = write_result(output, result)

            self.assertEqual(artifact.name, RESULT_FILENAME)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(artifact.read_text(encoding="utf-8")),
                result,
            )

    def test_cli_uses_explicit_output_and_prints_only_relative_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "private-runtime"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                return_code = main(["--out", str(output)])

            self.assertEqual(return_code, 0)
            summary = stdout.getvalue()
            self.assertIn("artifact=synthetic-result.json", summary)
            self.assertNotIn(str(output), summary)
            self.assertTrue((output / RESULT_FILENAME).is_file())

    def test_cli_normalizes_output_errors_without_traceback_or_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocker = root / "private-output-name"
            blocker.write_text("not a directory", encoding="utf-8")
            stderr = io.StringIO()

            with (
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["--out", str(blocker / "child")])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("error: cannot prepare", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn(str(root), stderr.getvalue())


class TraceValidationTests(unittest.TestCase):
    def test_canonical_trace_round_trips_through_strict_json(self) -> None:
        trace = default_trace()
        payload = json.dumps(trace.to_json(), sort_keys=True).encode("ascii")

        self.assertEqual(decode_trace(payload), trace)

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        with self.assertRaisesRegex(TraceValidationError, "duplicate"):
            decode_trace(
                b'{"schema_version":1,"schema_version":1,'
                b'"start_unix_ms":1767225600000,"events":[]}'
            )
        with self.assertRaisesRegex(TraceValidationError, "non-finite"):
            decode_trace(
                b'{"schema_version":1,"start_unix_ms":NaN,"events":[]}'
            )

    def test_parser_resource_errors_are_normalized(self) -> None:
        huge_integer = b'{"schema_version":' + (b"9" * 5000) + b"}"
        with self.assertRaisesRegex(TraceValidationError, "valid JSON"):
            decode_trace(huge_integer)

        nested = (b"[" * 10_000) + b"0" + (b"]" * 10_000)
        with self.assertRaisesRegex(TraceValidationError, "valid JSON"):
            decode_trace(nested)

    def test_error_output_does_not_reflect_unknown_user_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_field = "/private/person/secret"
            payload = default_trace().to_json()
            payload[secret_field] = "not-published"
            trace_path = root / "invalid.json"
            trace_path.write_text(json.dumps(payload), encoding="utf-8")
            stderr = io.StringIO()

            with (
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["--trace", str(trace_path), "--out", str(root / "out")])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("unknown fields", stderr.getvalue())
            self.assertNotIn(secret_field, stderr.getvalue())
            self.assertNotIn(str(trace_path), stderr.getvalue())

    def test_invalid_or_unsafe_fields_are_rejected(self) -> None:
        baseline = default_trace().to_json()
        cases: list[tuple[str, dict[str, object]]] = []

        extra = copy.deepcopy(baseline)
        extra["operator"] = "person"
        cases.append(("unknown fields", extra))

        real_token = copy.deepcopy(baseline)
        real_token["events"][0]["token"] = "REAL-123"
        cases.append(("SYNTH-00", real_token))

        unsafe_camera = copy.deepcopy(baseline)
        unsafe_camera["events"][0]["camera"] = "../camera"
        cases.append(("SYNTH-CAM-00", unsafe_camera))

        boolean_offset = copy.deepcopy(baseline)
        boolean_offset["events"][0]["offset_ms"] = True
        cases.append(("integer", boolean_offset))

        outside_box = copy.deepcopy(baseline)
        outside_box["events"][0]["box"]["x"] = 1200
        cases.append(("fit inside", outside_box))

        reverse_time = copy.deepcopy(baseline)
        reverse_time["events"][1]["offset_ms"] = 0
        reverse_time["events"][0]["offset_ms"] = 1
        cases.append(("nondecreasing", reverse_time))

        one_camera = copy.deepcopy(baseline)
        for event in one_camera["events"]:
            event["camera"] = "SYNTH-CAM-01"
        cases.append(("at least two", one_camera))

        for expected, payload in cases:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(TraceValidationError, expected),
            ):
                validate_trace(payload)

    def test_empty_and_oversized_payloads_are_rejected(self) -> None:
        with self.assertRaisesRegex(TraceValidationError, "valid JSON"):
            decode_trace(b"")
        with self.assertRaisesRegex(TraceValidationError, "exceeds"):
            decode_trace(b" " * (TRACE_MAX_BYTES + 1))

    def test_event_and_camera_count_limits_are_enforced(self) -> None:
        baseline = default_trace().to_json()
        too_many_events = copy.deepcopy(baseline)
        too_many_events["events"] = [
            copy.deepcopy(baseline["events"][index % 2])
            for index in range(257)
        ]
        for index, event in enumerate(too_many_events["events"]):
            event["offset_ms"] = index
        with self.assertRaisesRegex(TraceValidationError, "event limit"):
            validate_trace(too_many_events)

        too_many_cameras = copy.deepcopy(baseline)
        too_many_cameras["events"] = []
        for index in range(17):
            event = copy.deepcopy(baseline["events"][index % 2])
            event["camera"] = f"SYNTH-CAM-{index:02d}"
            event["offset_ms"] = index
            too_many_cameras["events"].append(event)
        with self.assertRaisesRegex(TraceValidationError, "camera limit"):
            validate_trace(too_many_cameras)

    def test_regular_trace_file_is_read_but_symlink_and_fifo_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "trace.json"
            regular.write_text(
                json.dumps(default_trace().to_json(), sort_keys=True),
                encoding="utf-8",
            )
            self.assertEqual(read_trace(regular), default_trace())

            linked = root / "linked.json"
            linked.symlink_to(regular)
            with self.assertRaisesRegex(TraceValidationError, "safely"):
                read_trace(linked)

            fifo = root / "trace.fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(TraceValidationError, "regular file"):
                read_trace(fifo)

    def test_oversized_trace_file_is_rejected_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.json"
            path.write_bytes(b" " * (TRACE_MAX_BYTES + 1))

            with self.assertRaisesRegex(TraceValidationError, "exceeds"):
                read_trace(path)

    def test_trace_path_rejects_symlinked_ancestor_and_directory_leaf(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_directory = root / "real"
            real_directory.mkdir()
            trace_path = real_directory / "trace.json"
            trace_path.write_text(
                json.dumps(default_trace().to_json(), sort_keys=True),
                encoding="utf-8",
            )
            linked_directory = root / "linked"
            linked_directory.symlink_to(real_directory, target_is_directory=True)

            with self.assertRaisesRegex(TraceValidationError, "safely"):
                read_trace(linked_directory / "trace.json")
            with self.assertRaisesRegex(
                TraceValidationError,
                "regular file|safely",
            ):
                read_trace(real_directory)

    def test_replaced_trace_entry_is_rejected_after_pinned_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "trace.json"
            payload = json.dumps(
                default_trace().to_json(),
                sort_keys=True,
            ).encode("utf-8")
            path.write_bytes(payload)
            replacement = root / "replacement.json"
            replacement.write_bytes(payload)
            real_read = os.read
            replaced = False

            def replacing_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                chunk = real_read(descriptor, size)
                if chunk and not replaced:
                    replaced = True
                    os.replace(replacement, path)
                return chunk

            with (
                mock.patch(
                    "alpr_runner.synthetic.os.read",
                    side_effect=replacing_read,
                ),
                self.assertRaisesRegex(
                    TraceValidationError,
                    "changed",
                ),
            ):
                read_trace(path)

    def test_same_inode_trace_rewrite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            payload = json.dumps(
                default_trace().to_json(),
                sort_keys=True,
            ).encode("utf-8")
            path.write_bytes(payload)
            real_read = os.read
            rewritten = False

            def rewriting_read(descriptor: int, size: int) -> bytes:
                nonlocal rewritten
                chunk = real_read(descriptor, size)
                if chunk and not rewritten:
                    rewritten = True
                    with path.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(b" " * len(payload))
                        stream.flush()
                        os.fsync(stream.fileno())
                return chunk

            with (
                mock.patch(
                    "alpr_runner.synthetic.os.read",
                    side_effect=rewriting_read,
                ),
                self.assertRaisesRegex(
                    TraceValidationError,
                    "changed",
                ),
            ):
                read_trace(path)

    def test_trace_reader_closes_all_descriptors_on_entry_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            path.write_text(
                json.dumps(default_trace().to_json(), sort_keys=True),
                encoding="utf-8",
            )
            real_open = os.open
            real_close = os.close
            opened: list[int] = []
            closed: list[int] = []

            def tracking_open(*args: object, **kwargs: object) -> int:
                descriptor = real_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor

            def tracking_close(descriptor: int) -> None:
                closed.append(descriptor)
                real_close(descriptor)

            with (
                mock.patch(
                    "alpr_runner.synthetic.os.open",
                    side_effect=tracking_open,
                ),
                mock.patch(
                    "alpr_runner.synthetic.os.close",
                    side_effect=tracking_close,
                ),
                mock.patch(
                    "alpr_runner.synthetic.os.stat",
                    side_effect=FileNotFoundError,
                ),
                self.assertRaisesRegex(TraceValidationError, "changed"),
            ):
                read_trace(path)

            self.assertCountEqual(opened, closed)

    def test_trace_cleanup_continues_after_one_close_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            path.write_text(
                json.dumps(default_trace().to_json(), sort_keys=True),
                encoding="utf-8",
            )
            real_open = os.open
            real_close = os.close
            opened: list[int] = []
            closed: list[int] = []

            def tracking_open(*args: object, **kwargs: object) -> int:
                descriptor = real_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor

            def one_failing_close(descriptor: int) -> None:
                real_close(descriptor)
                closed.append(descriptor)
                if len(closed) == 1:
                    raise OSError("injected close failure")

            with (
                mock.patch(
                    "alpr_runner.synthetic.os.open",
                    side_effect=tracking_open,
                ),
                mock.patch(
                    "alpr_runner.synthetic.os.close",
                    side_effect=one_failing_close,
                ),
            ):
                self.assertEqual(read_trace(path), default_trace())

            self.assertCountEqual(opened, closed)
