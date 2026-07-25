from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .aggregation import PlateRegistry
from .dtk import Plate
from .runtime_io import (
    RuntimeStorageError,
    atomic_json,
    prepare_private_directory,
    private_relative_path,
)
from .zoom import ZoomController, plate_to_target

TRACE_MAX_BYTES = 64 * 1024
TRACE_MAX_EVENTS = 256
TRACE_MAX_CAMERAS = 16
RESULT_FILENAME = "synthetic-result.json"

_CAMERA_PATTERN = re.compile(r"SYNTH-CAM-[0-9]{2}\Z")
_TOKEN_PATTERN = re.compile(r"SYNTH-[0-9]{2}\Z")
_MIN_UNIX_MS = 946_684_800_000  # 2000-01-01T00:00:00Z
_MAX_UNIX_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z

_DEFAULT_TRACE_JSON = """
{
  "events": [
    {
      "box": {"height": 24, "width": 132, "x": 174, "y": 412},
      "camera": "SYNTH-CAM-01",
      "frame": {"height": 720, "width": 1280},
      "offset_ms": 0,
      "token": "SYNTH-01"
    },
    {
      "box": {"height": 25, "width": 146, "x": 846, "y": 388},
      "camera": "SYNTH-CAM-02",
      "frame": {"height": 720, "width": 1280},
      "offset_ms": 250,
      "token": "SYNTH-02"
    },
    {
      "box": {"height": 22, "width": 138, "x": 478, "y": 396},
      "camera": "SYNTH-CAM-02",
      "frame": {"height": 720, "width": 1280},
      "offset_ms": 500,
      "token": "SYNTH-01"
    },
    {
      "box": {"height": 26, "width": 154, "x": 716, "y": 374},
      "camera": "SYNTH-CAM-01",
      "frame": {"height": 720, "width": 1280},
      "offset_ms": 900,
      "token": "SYNTH-02"
    },
    {
      "box": {"height": 23, "width": 142, "x": 286, "y": 402},
      "camera": "SYNTH-CAM-01",
      "frame": {"height": 720, "width": 1280},
      "offset_ms": 1250,
      "token": "SYNTH-01"
    }
  ],
  "schema_version": 1,
  "start_unix_ms": 1767225600000
}
""".strip()


class TraceValidationError(ValueError):
    """Raised when a synthetic trace violates its bounded public contract."""


@dataclass(frozen=True)
class FrameGeometry:
    width: int
    height: int

    def to_json(self) -> dict[str, int]:
        return {"height": self.height, "width": self.width}


@dataclass(frozen=True)
class EventBox:
    x: int
    y: int
    width: int
    height: int

    def to_json(self) -> dict[str, int]:
        return {
            "height": self.height,
            "width": self.width,
            "x": self.x,
            "y": self.y,
        }


@dataclass(frozen=True)
class SyntheticEvent:
    offset_ms: int
    camera: str
    token: str
    frame: FrameGeometry
    box: EventBox

    def to_json(self) -> dict[str, Any]:
        return {
            "box": self.box.to_json(),
            "camera": self.camera,
            "frame": self.frame.to_json(),
            "offset_ms": self.offset_ms,
            "token": self.token,
        }


@dataclass(frozen=True)
class SyntheticTrace:
    schema_version: int
    start_unix_ms: int
    events: tuple[SyntheticEvent, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "events": [event.to_json() for event in self.events],
            "schema_version": self.schema_version,
            "start_unix_ms": self.start_unix_ms,
        }

    @property
    def cameras(self) -> tuple[str, ...]:
        return tuple(sorted({event.camera for event in self.events}))


class EventClock:
    """Mutable injected clock advanced only by validated trace events."""

    def __init__(self, initial_seconds: float) -> None:
        self.current_seconds = initial_seconds

    def __call__(self) -> float:
        return self.current_seconds


def default_trace() -> SyntheticTrace:
    return decode_trace(_DEFAULT_TRACE_JSON.encode("ascii"))


def canonical_trace_bytes(trace: SyntheticTrace | None = None) -> bytes:
    """Serialize the public synthetic fixture in one stable byte form."""

    selected = default_trace() if trace is None else trace
    return (
        json.dumps(
            selected.to_json(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def read_trace(path: str | Path) -> SyntheticTrace:
    """Read one stable regular file through a component-pinned POSIX path."""

    parts = _absolute_trace_parts(path)
    directory_flags = _required_open_flags(
        os.O_RDONLY,
        "O_CLOEXEC",
        "O_DIRECTORY",
        "O_NOFOLLOW",
    )
    file_flags = _required_open_flags(
        os.O_RDONLY,
        "O_CLOEXEC",
        "O_NOFOLLOW",
        "O_NONBLOCK",
    )

    directory_descriptors: list[int] = []
    pinned_entries: list[tuple[int, str, tuple[int, int, int]]] = []
    descriptor: int | None = None
    try:
        directory_descriptors.append(os.open("/", directory_flags))
        for component in parts[:-1]:
            parent = directory_descriptors[-1]
            child = os.open(component, directory_flags, dir_fd=parent)
            directory_descriptors.append(child)
            child_status = os.fstat(child)
            if not stat.S_ISDIR(child_status.st_mode):
                raise TraceValidationError(
                    "trace path contains a non-directory component"
                )
            expected = _entry_identity(child_status)
            _require_visible_entry(parent, component, expected, directory=True)
            pinned_entries.append((parent, component, expected))

        parent = directory_descriptors[-1]
        leaf = parts[-1]
        descriptor = os.open(leaf, file_flags, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise TraceValidationError("trace input must be a regular file")
        if before.st_size > TRACE_MAX_BYTES:
            raise TraceValidationError(
                f"trace input exceeds {TRACE_MAX_BYTES} bytes"
            )

        payload = _read_bounded_trace(descriptor)
        after = os.fstat(descriptor)
        if _file_snapshot(before) != _file_snapshot(after):
            raise TraceValidationError("trace input changed while being read")
        _require_visible_entry(
            parent,
            leaf,
            _entry_identity(after),
            directory=False,
        )
        for pinned_parent, component, expected in pinned_entries:
            _require_visible_entry(
                pinned_parent,
                component,
                expected,
                directory=True,
            )
    except TraceValidationError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise TraceValidationError("trace file could not be opened safely") from error
    finally:
        _close_trace_descriptors(descriptor, directory_descriptors)

    return decode_trace(payload)


def decode_trace(payload: bytes) -> SyntheticTrace:
    if len(payload) > TRACE_MAX_BYTES:
        raise TraceValidationError(
            f"trace input exceeds {TRACE_MAX_BYTES} bytes"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TraceValidationError("trace input must be UTF-8 JSON") from error

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_mapping_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (
        json.JSONDecodeError,
        RecursionError,
        TraceValidationError,
        ValueError,
    ) as error:
        if isinstance(error, TraceValidationError):
            raise
        raise TraceValidationError("trace input must be valid JSON") from error
    return validate_trace(raw)


def validate_trace(raw: object) -> SyntheticTrace:
    trace = _require_mapping(raw, "trace")
    _require_exact_keys(
        trace,
        {"events", "schema_version", "start_unix_ms"},
        "trace",
    )
    schema_version = _require_integer(
        trace["schema_version"],
        "schema_version",
        minimum=1,
        maximum=1,
    )
    start_unix_ms = _require_integer(
        trace["start_unix_ms"],
        "start_unix_ms",
        minimum=_MIN_UNIX_MS,
        maximum=_MAX_UNIX_MS,
    )

    raw_events = trace["events"]
    if not isinstance(raw_events, list):
        raise TraceValidationError("events must be an array")
    if not raw_events:
        raise TraceValidationError("events must not be empty")
    if len(raw_events) > TRACE_MAX_EVENTS:
        raise TraceValidationError(
            f"events exceed the {TRACE_MAX_EVENTS} event limit"
        )

    events: list[SyntheticEvent] = []
    previous_offset = -1
    for index, raw_event in enumerate(raw_events):
        event = _validate_event(raw_event, index)
        if event.offset_ms < previous_offset:
            raise TraceValidationError("event offsets must be nondecreasing")
        previous_offset = event.offset_ms
        events.append(event)

    cameras = {event.camera for event in events}
    if len(cameras) < 2:
        raise TraceValidationError(
            "trace must exercise at least two synthetic cameras"
        )
    if len(cameras) > TRACE_MAX_CAMERAS:
        raise TraceValidationError(
            f"trace exceeds the {TRACE_MAX_CAMERAS} camera limit"
        )
    if start_unix_ms + previous_offset > _MAX_UNIX_MS:
        raise TraceValidationError("trace timestamps exceed the supported range")

    return SyntheticTrace(
        schema_version=schema_version,
        start_unix_ms=start_unix_ms,
        events=tuple(events),
    )


def run_synthetic(trace: SyntheticTrace) -> dict[str, Any]:
    """Run validated events through production aggregation and zoom geometry."""

    clock = EventClock(trace.start_unix_ms / 1000.0)
    registry = PlateRegistry(
        print_every=0,
        print_min_seconds=0.0,
        clock=clock,
        timestamp_formatter=_format_utc_timestamp,
    )
    controllers = {
        camera: ZoomController(max_zoom=4.0) for camera in trace.cameras
    }
    event_results: list[dict[str, Any]] = []
    last_zoom_by_camera: dict[str, dict[str, Any]] = {}

    for sequence, item in enumerate(trace.events, start=1):
        clock.current_seconds = (
            trace.start_unix_ms + item.offset_ms
        ) / 1000.0
        plate = Plate(
            text=item.token,
            country="synthetic-event",
            confidence=0,
            x=item.box.x,
            y=item.box.y,
            width=item.box.width,
            height=item.box.height,
        )
        target = plate_to_target(
            plate,
            item.frame.width,
            item.frame.height,
        )
        target.label = f"synthetic-token:{item.token}"
        command = controllers[item.camera].next([target])
        target_json = target.to_json()
        zoom_json = command.to_json()
        frame_json = item.frame.to_json()
        recorded, _should_print = registry.record(
            item.camera,
            plate,
            target_json,
            zoom_json,
            frame_json,
        )
        if recorded is None:
            raise RuntimeError("validated synthetic token was not aggregatable")
        last_zoom_by_camera[item.camera] = zoom_json
        event_results.append(
            {
                "aggregate_count": recorded["count"],
                "camera": item.camera,
                "first_for_token": recorded["is_new"],
                "frame": frame_json,
                "seen_by_cameras": sorted(recorded["cameras"]),
                "sequence": sequence,
                "target": target_json,
                "time": recorded["time"],
                "token": item.token,
                "zoom_command": zoom_json,
            }
        )

    snapshot = registry.snapshot()
    token_results = [
        {
            "cameras": dict(sorted(item["cameras"].items())),
            "event_count": item["count"],
            "first_seen": item["first_seen"],
            "last_camera": item["last_camera"],
            "last_frame": item["last_frame_size"],
            "last_seen": item["last_seen"],
            "last_target": item["last_target"],
            "last_zoom": item["last_zoom"],
            "normalized_key": item["key"],
            "token": item["text"],
        }
        for item in snapshot["plates"]
    ]

    canonical_trace = json.dumps(
        trace.to_json(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return {
        "aggregation": {
            "event_count": snapshot["total_recognitions"],
            "tokens": token_results,
            "unique_token_count": snapshot["total_unique_plates"],
        },
        "backend": "synthetic-events",
        "camera_zoom_state": dict(sorted(last_zoom_by_camera.items())),
        "events": event_results,
        "input_kind": "validated-synthetic-event-trace",
        "notice": (
            "No images were processed and no recognition engine was invoked."
        ),
        "pipeline": [
            "strict trace validation",
            "production plate-to-target geometry",
            "per-camera production zoom controller",
            "production cross-camera registry",
        ],
        "recognition_accuracy": "not_evaluated",
        "recognition_performed": False,
        "schema_version": 1,
        "trace": {
            "camera_count": len(trace.cameras),
            "event_count": len(trace.events),
            "sha256": hashlib.sha256(canonical_trace).hexdigest(),
            "start_time": _format_utc_timestamp(
                trace.start_unix_ms / 1000.0
            ),
        },
    }


def write_result(output_directory: str | Path, result: dict[str, Any]) -> Path:
    output = prepare_private_directory(output_directory)
    artifact = output / RESULT_FILENAME
    atomic_json(artifact, result)
    return artifact


def render_ascii_summary(
    result: dict[str, Any],
    artifact_name: str = RESULT_FILENAME,
) -> str:
    aggregation = result["aggregation"]
    lines = [
        "ALPR SYNTHETIC EVENT DEMO",
        f"backend={result['backend']}",
        f"recognition_accuracy={result['recognition_accuracy']}",
        "recognition_performed=false",
        "input=validated synthetic events (no images)",
        (
            f"events={aggregation['event_count']} "
            f"cameras={result['trace']['camera_count']} "
            f"unique_tokens={aggregation['unique_token_count']}"
        ),
    ]
    for token in aggregation["tokens"]:
        cameras = ",".join(sorted(token["cameras"]))
        lines.append(
            f"{token['token']} events={token['event_count']} "
            f"cameras={cameras}"
        )
    lines.append(f"artifact={artifact_name}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run fake SYNTH-xx events through aggregation and zoom control. "
            "This command does not perform image recognition."
        )
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Explicit private directory for synthetic-result.json.",
    )
    parser.add_argument(
        "--trace",
        help=(
            "Optional bounded synthetic trace JSON. "
            "The built-in deterministic trace is used when omitted."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        trace = read_trace(args.trace) if args.trace else default_trace()
        result = run_synthetic(trace)
        artifact = write_result(args.out, result)
        output = artifact.parent
        artifact_name = private_relative_path(artifact, output)
    except (RuntimeStorageError, TraceValidationError) as error:
        parser.exit(2, f"error: {error}\n")
    print(render_ascii_summary(result, artifact_name), end="")
    return 0


def _validate_event(raw: object, index: int) -> SyntheticEvent:
    context = f"events[{index}]"
    event = _require_mapping(raw, context)
    _require_exact_keys(
        event,
        {"box", "camera", "frame", "offset_ms", "token"},
        context,
    )
    offset_ms = _require_integer(
        event["offset_ms"],
        f"{context}.offset_ms",
        minimum=0,
        maximum=86_400_000,
    )
    camera = _require_string(event["camera"], f"{context}.camera")
    if _CAMERA_PATTERN.fullmatch(camera) is None:
        raise TraceValidationError(
            f"{context}.camera must match SYNTH-CAM-00"
        )
    token = _require_string(event["token"], f"{context}.token")
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise TraceValidationError(f"{context}.token must match SYNTH-00")

    frame_raw = _require_mapping(event["frame"], f"{context}.frame")
    _require_exact_keys(frame_raw, {"height", "width"}, f"{context}.frame")
    frame = FrameGeometry(
        width=_require_integer(
            frame_raw["width"],
            f"{context}.frame.width",
            minimum=64,
            maximum=8192,
        ),
        height=_require_integer(
            frame_raw["height"],
            f"{context}.frame.height",
            minimum=64,
            maximum=8192,
        ),
    )

    box_raw = _require_mapping(event["box"], f"{context}.box")
    _require_exact_keys(
        box_raw,
        {"height", "width", "x", "y"},
        f"{context}.box",
    )
    box = EventBox(
        x=_require_integer(
            box_raw["x"],
            f"{context}.box.x",
            minimum=0,
            maximum=frame.width - 1,
        ),
        y=_require_integer(
            box_raw["y"],
            f"{context}.box.y",
            minimum=0,
            maximum=frame.height - 1,
        ),
        width=_require_integer(
            box_raw["width"],
            f"{context}.box.width",
            minimum=1,
            maximum=frame.width,
        ),
        height=_require_integer(
            box_raw["height"],
            f"{context}.box.height",
            minimum=1,
            maximum=frame.height,
        ),
    )
    if box.x + box.width > frame.width or box.y + box.height > frame.height:
        raise TraceValidationError(f"{context}.box must fit inside its frame")

    return SyntheticEvent(
        offset_ms=offset_ms,
        camera=camera,
        token=token,
        frame=frame,
        box=box,
    )


def _mapping_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TraceValidationError("trace JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise TraceValidationError("trace JSON non-finite numbers are not allowed")


def _absolute_trace_parts(path: str | Path) -> tuple[str, ...]:
    try:
        requested = Path(path).expanduser()
        absolute = Path(os.path.abspath(os.fspath(requested)))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise TraceValidationError("trace path is invalid") from error
    parts = absolute.parts
    if not parts or parts[0] != "/" or len(parts) == 1:
        raise TraceValidationError("trace path must name a file")
    relative_parts = parts[1:]
    if any(part in {"", ".", ".."} for part in relative_parts):
        raise TraceValidationError("trace path is invalid")
    return relative_parts


def _required_open_flags(base: int, *names: str) -> int:
    flags = base
    for name in names:
        value = getattr(os, name, None)
        if value is None:
            raise TraceValidationError(
                "safe trace reading requires POSIX open flags"
            )
        flags |= value
    return flags


def _close_trace_descriptors(
    descriptor: int | None,
    directory_descriptors: list[int],
) -> None:
    candidates = (
        ([] if descriptor is None else [descriptor])
        + list(reversed(directory_descriptors))
    )
    for candidate in candidates:
        try:
            os.close(candidate)
        except OSError:
            pass


def _read_bounded_trace(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    while consumed <= TRACE_MAX_BYTES:
        try:
            chunk = os.read(
                descriptor,
                min(8192, TRACE_MAX_BYTES + 1 - consumed),
            )
        except OSError as error:
            raise TraceValidationError("trace input could not be read") from error
        if not chunk:
            break
        chunks.append(chunk)
        consumed += len(chunk)
    if consumed > TRACE_MAX_BYTES:
        raise TraceValidationError(
            f"trace input exceeds {TRACE_MAX_BYTES} bytes"
        )
    return b"".join(chunks)


def _entry_identity(status: os.stat_result) -> tuple[int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        stat.S_IFMT(status.st_mode),
    )


def _file_snapshot(
    status: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        stat.S_IFMT(status.st_mode),
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _require_visible_entry(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, int, int],
    *,
    directory: bool,
) -> None:
    try:
        visible = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise TraceValidationError("trace path changed while being read") from error
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if stat.S_IFMT(visible.st_mode) != expected_type:
        raise TraceValidationError("trace path changed while being read")
    if _entry_identity(visible) != expected:
        raise TraceValidationError("trace path changed while being read")


def _require_mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TraceValidationError(f"{context} must be an object")
    return value


def _require_exact_keys(
    mapping: dict[str, Any],
    expected: set[str],
    context: str,
) -> None:
    if not all(isinstance(key, str) for key in mapping):
        raise TraceValidationError(f"{context} field names must be text")
    actual = set(mapping)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise TraceValidationError(
            f"{context} is missing fields: {', '.join(missing)}"
        )
    if extra:
        raise TraceValidationError(f"{context} has unknown fields")


def _require_integer(
    value: object,
    context: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceValidationError(f"{context} must be an integer")
    if not minimum <= value <= maximum:
        raise TraceValidationError(
            f"{context} must be between {minimum} and {maximum}"
        )
    return value


def _require_string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise TraceValidationError(f"{context} must be text")
    return value


def _format_utc_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    raise SystemExit(main())
