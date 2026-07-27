from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECIPE_MAX_BYTES = 32 * 1024
WIDTH = 160
HEIGHT = 96
FRAMES_PER_SECOND = 6
FRAME_COUNT = 18
Y4M_FRAME_MARKER = b"FRAME\n"
Y4M_HEADER = b"YUV4MPEG2 W160 H96 F6:1 Ip A1:1 C420jpeg\n"
Y_PLANE_BYTES = WIDTH * HEIGHT
CHROMA_PLANE_BYTES = (WIDTH // 2) * (HEIGHT // 2)
FRAME_PAYLOAD_BYTES = Y_PLANE_BYTES + (2 * CHROMA_PLANE_BYTES)
Y4M_BYTES = len(Y4M_HEADER) + FRAME_COUNT * (
    len(Y4M_FRAME_MARKER) + FRAME_PAYLOAD_BYTES
)

_DEFAULT_RECIPE_JSON = """
{
  "fps": 6,
  "frame_count": 18,
  "height": 96,
  "scene": {
    "horizon_y": 26,
    "lane_marks": [
      {
        "bottom_width": 5,
        "bottom_x": 24,
        "top_width": 1,
        "top_x": 58,
        "yuv": [
          210,
          128,
          128
        ]
      },
      {
        "bottom_width": 4,
        "bottom_x": 80,
        "top_width": 1,
        "top_x": 80,
        "yuv": [
          210,
          128,
          128
        ]
      },
      {
        "bottom_width": 5,
        "bottom_x": 135,
        "top_width": 1,
        "top_x": 102,
        "yuv": [
          210,
          128,
          128
        ]
      }
    ],
    "road_yuv": [
      52,
      128,
      128
    ],
    "sky_yuv": [
      132,
      148,
      112
    ],
    "vehicles": [
      {
        "delta_x": 3,
        "delta_y": 0,
        "height": 20,
        "start_x": 18,
        "start_y": 58,
        "width": 30,
        "yuv": [
          112,
          104,
          174
        ]
      },
      {
        "delta_x": -2,
        "delta_y": 1,
        "height": 16,
        "start_x": 116,
        "start_y": 45,
        "width": 24,
        "yuv": [
          154,
          166,
          92
        ]
      }
    ]
  },
  "schema_version": 1,
  "width": 160
}
""".strip()


class RecipeValidationError(ValueError):
    """Raised when a synthetic-media recipe violates its closed contract."""


@dataclass(frozen=True)
class YuvColor:
    y: int
    u: int
    v: int

    def to_json(self) -> list[int]:
        return [self.y, self.u, self.v]


@dataclass(frozen=True)
class LaneMark:
    top_x: int
    bottom_x: int
    top_width: int
    bottom_width: int
    color: YuvColor

    def to_json(self) -> dict[str, object]:
        return {
            "bottom_width": self.bottom_width,
            "bottom_x": self.bottom_x,
            "top_width": self.top_width,
            "top_x": self.top_x,
            "yuv": self.color.to_json(),
        }


@dataclass(frozen=True)
class VehicleShape:
    start_x: int
    start_y: int
    width: int
    height: int
    delta_x: int
    delta_y: int
    color: YuvColor

    def to_json(self) -> dict[str, object]:
        return {
            "delta_x": self.delta_x,
            "delta_y": self.delta_y,
            "height": self.height,
            "start_x": self.start_x,
            "start_y": self.start_y,
            "width": self.width,
            "yuv": self.color.to_json(),
        }


@dataclass(frozen=True)
class SyntheticScene:
    horizon_y: int
    sky: YuvColor
    road: YuvColor
    lane_marks: tuple[LaneMark, ...]
    vehicles: tuple[VehicleShape, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "horizon_y": self.horizon_y,
            "lane_marks": [mark.to_json() for mark in self.lane_marks],
            "road_yuv": self.road.to_json(),
            "sky_yuv": self.sky.to_json(),
            "vehicles": [vehicle.to_json() for vehicle in self.vehicles],
        }


@dataclass(frozen=True)
class SyntheticMediaRecipe:
    schema_version: int
    width: int
    height: int
    fps: int
    frame_count: int
    scene: SyntheticScene

    def to_json(self) -> dict[str, object]:
        return {
            "fps": self.fps,
            "frame_count": self.frame_count,
            "height": self.height,
            "scene": self.scene.to_json(),
            "schema_version": self.schema_version,
            "width": self.width,
        }


def default_recipe() -> SyntheticMediaRecipe:
    """Return the closed, timestamp-free public media recipe."""

    return decode_recipe(_DEFAULT_RECIPE_JSON.encode("ascii"))


def canonical_recipe_bytes(
    recipe: SyntheticMediaRecipe | None = None,
) -> bytes:
    """Serialize a validated recipe in the one public canonical JSON form."""

    selected = default_recipe() if recipe is None else _normalize_recipe(recipe)
    return (
        json.dumps(
            selected.to_json(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def load_recipe(path: str | Path) -> SyntheticMediaRecipe:
    """Read one bounded regular file through a component-pinned POSIX path."""

    parts = _absolute_recipe_parts(path)
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
                raise RecipeValidationError(
                    "recipe path contains a non-directory component"
                )
            expected = _entry_identity(child_status)
            _require_visible_entry(
                parent,
                component,
                expected,
                directory=True,
            )
            pinned_entries.append((parent, component, expected))

        parent = directory_descriptors[-1]
        leaf = parts[-1]
        descriptor = os.open(leaf, file_flags, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecipeValidationError("recipe input must be a regular file")
        if before.st_size > RECIPE_MAX_BYTES:
            raise RecipeValidationError(
                f"recipe input exceeds {RECIPE_MAX_BYTES} bytes"
            )

        payload = _read_bounded_recipe(descriptor)
        after = os.fstat(descriptor)
        if _file_snapshot(before) != _file_snapshot(after):
            raise RecipeValidationError(
                "recipe input changed while being read"
            )
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
    except RecipeValidationError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise RecipeValidationError(
            "recipe file could not be opened safely"
        ) from error
    finally:
        _close_descriptors(descriptor, directory_descriptors)

    return decode_recipe(payload)


def decode_recipe(payload: bytes) -> SyntheticMediaRecipe:
    """Decode bounded UTF-8 JSON without duplicate keys or non-finite values."""

    if len(payload) > RECIPE_MAX_BYTES:
        raise RecipeValidationError(
            f"recipe input exceeds {RECIPE_MAX_BYTES} bytes"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecipeValidationError(
            "recipe input must be UTF-8 JSON"
        ) from error
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_mapping_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (
        json.JSONDecodeError,
        RecursionError,
        RecipeValidationError,
        ValueError,
    ) as error:
        if isinstance(error, RecipeValidationError):
            raise
        raise RecipeValidationError(
            "recipe input must be valid JSON"
        ) from error
    return validate_recipe(raw)


def validate_recipe(raw: object) -> SyntheticMediaRecipe:
    """Validate the numeric-only v1 recipe and return its immutable form."""

    recipe = _require_mapping(raw, "recipe")
    _require_exact_keys(
        recipe,
        {
            "fps",
            "frame_count",
            "height",
            "scene",
            "schema_version",
            "width",
        },
        "recipe",
    )
    schema_version = _require_integer(
        recipe["schema_version"],
        "schema_version",
        minimum=1,
        maximum=1,
    )
    width = _require_integer(
        recipe["width"],
        "width",
        minimum=WIDTH,
        maximum=WIDTH,
    )
    height = _require_integer(
        recipe["height"],
        "height",
        minimum=HEIGHT,
        maximum=HEIGHT,
    )
    fps = _require_integer(
        recipe["fps"],
        "fps",
        minimum=FRAMES_PER_SECOND,
        maximum=FRAMES_PER_SECOND,
    )
    frame_count = _require_integer(
        recipe["frame_count"],
        "frame_count",
        minimum=FRAME_COUNT,
        maximum=FRAME_COUNT,
    )

    raw_scene = _require_mapping(recipe["scene"], "scene")
    _require_exact_keys(
        raw_scene,
        {
            "horizon_y",
            "lane_marks",
            "road_yuv",
            "sky_yuv",
            "vehicles",
        },
        "scene",
    )
    horizon_y = _require_integer(
        raw_scene["horizon_y"],
        "scene.horizon_y",
        minimum=16,
        maximum=height - 24,
    )
    sky = _validate_color(raw_scene["sky_yuv"], "scene.sky_yuv")
    road = _validate_color(raw_scene["road_yuv"], "scene.road_yuv")
    lane_marks = _validate_lane_marks(
        raw_scene["lane_marks"],
        width=width,
        height=height,
        horizon_y=horizon_y,
    )
    vehicles = _validate_vehicles(
        raw_scene["vehicles"],
        width=width,
        height=height,
        frame_count=frame_count,
    )
    return SyntheticMediaRecipe(
        schema_version=schema_version,
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        scene=SyntheticScene(
            horizon_y=horizon_y,
            sky=sky,
            road=road,
            lane_marks=lane_marks,
            vehicles=vehicles,
        ),
    )


def frame_payloads(
    recipe: SyntheticMediaRecipe,
) -> tuple[bytes, ...]:
    """Render exact planar YUV420 frame payloads in Y, U, V order."""

    selected = _normalize_recipe(recipe)
    frames = tuple(
        _render_frame(selected, frame_index)
        for frame_index in range(selected.frame_count)
    )
    if len(frames) != FRAME_COUNT or any(
        len(frame) != FRAME_PAYLOAD_BYTES for frame in frames
    ):
        raise RuntimeError("synthetic media renderer violated its frame bound")
    return frames


def render_y4m(recipe: SyntheticMediaRecipe) -> bytes:
    """Render one bounded YUV4MPEG2 byte stream with no timestamps or metadata."""

    frames = frame_payloads(recipe)
    output = bytearray(Y4M_HEADER)
    for frame in frames:
        output.extend(Y4M_FRAME_MARKER)
        output.extend(frame)
    if len(output) != Y4M_BYTES:
        raise RuntimeError("synthetic media renderer violated its stream bound")
    return bytes(output)


def _normalize_recipe(
    recipe: SyntheticMediaRecipe,
) -> SyntheticMediaRecipe:
    if not isinstance(recipe, SyntheticMediaRecipe):
        raise TypeError("recipe must be a SyntheticMediaRecipe")
    return validate_recipe(recipe.to_json())


def _validate_color(raw: object, field: str) -> YuvColor:
    if not isinstance(raw, list) or len(raw) != 3:
        raise RecipeValidationError(f"{field} must be a three-item array")
    return YuvColor(
        y=_require_integer(raw[0], f"{field}[0]", minimum=16, maximum=235),
        u=_require_integer(raw[1], f"{field}[1]", minimum=16, maximum=240),
        v=_require_integer(raw[2], f"{field}[2]", minimum=16, maximum=240),
    )


def _validate_lane_marks(
    raw: object,
    *,
    width: int,
    height: int,
    horizon_y: int,
) -> tuple[LaneMark, ...]:
    if not isinstance(raw, list):
        raise RecipeValidationError("scene.lane_marks must be an array")
    if not 2 <= len(raw) <= 4:
        raise RecipeValidationError(
            "scene.lane_marks must contain between 2 and 4 shapes"
        )
    marks: list[LaneMark] = []
    for index, item in enumerate(raw):
        field = f"scene.lane_marks[{index}]"
        mark = _require_mapping(item, field)
        _require_exact_keys(
            mark,
            {
                "bottom_width",
                "bottom_x",
                "top_width",
                "top_x",
                "yuv",
            },
            field,
        )
        top_x = _require_integer(
            mark["top_x"],
            f"{field}.top_x",
            minimum=0,
            maximum=width - 1,
        )
        bottom_x = _require_integer(
            mark["bottom_x"],
            f"{field}.bottom_x",
            minimum=0,
            maximum=width - 1,
        )
        top_width = _require_integer(
            mark["top_width"],
            f"{field}.top_width",
            minimum=1,
            maximum=8,
        )
        bottom_width = _require_integer(
            mark["bottom_width"],
            f"{field}.bottom_width",
            minimum=1,
            maximum=16,
        )
        _require_centered_span(top_x, top_width, width, f"{field}.top")
        _require_centered_span(
            bottom_x,
            bottom_width,
            width,
            f"{field}.bottom",
        )
        validated = LaneMark(
            top_x=top_x,
            bottom_x=bottom_x,
            top_width=top_width,
            bottom_width=bottom_width,
            color=_validate_color(mark["yuv"], f"{field}.yuv"),
        )
        interpolation_span = height - 1 - horizon_y
        for offset in range(interpolation_span + 1):
            left, interpolated_width = _interpolated_lane_span(
                validated,
                offset=offset,
                total=interpolation_span,
            )
            if left < 0 or left + interpolated_width > width:
                raise RecipeValidationError(
                    f"{field} interpolated width must fit inside the frame"
                )
        marks.append(validated)
    return tuple(marks)


def _validate_vehicles(
    raw: object,
    *,
    width: int,
    height: int,
    frame_count: int,
) -> tuple[VehicleShape, ...]:
    if not isinstance(raw, list):
        raise RecipeValidationError("scene.vehicles must be an array")
    if not 1 <= len(raw) <= 4:
        raise RecipeValidationError(
            "scene.vehicles must contain between 1 and 4 shapes"
        )
    vehicles: list[VehicleShape] = []
    for index, item in enumerate(raw):
        field = f"scene.vehicles[{index}]"
        vehicle = _require_mapping(item, field)
        _require_exact_keys(
            vehicle,
            {
                "delta_x",
                "delta_y",
                "height",
                "start_x",
                "start_y",
                "width",
                "yuv",
            },
            field,
        )
        shape_width = _require_integer(
            vehicle["width"],
            f"{field}.width",
            minimum=12,
            maximum=48,
        )
        shape_height = _require_integer(
            vehicle["height"],
            f"{field}.height",
            minimum=10,
            maximum=32,
        )
        start_x = _require_integer(
            vehicle["start_x"],
            f"{field}.start_x",
            minimum=0,
            maximum=width - shape_width,
        )
        start_y = _require_integer(
            vehicle["start_y"],
            f"{field}.start_y",
            minimum=0,
            maximum=height - shape_height,
        )
        delta_x = _require_integer(
            vehicle["delta_x"],
            f"{field}.delta_x",
            minimum=-8,
            maximum=8,
        )
        delta_y = _require_integer(
            vehicle["delta_y"],
            f"{field}.delta_y",
            minimum=-4,
            maximum=4,
        )
        final_x = start_x + delta_x * (frame_count - 1)
        final_y = start_y + delta_y * (frame_count - 1)
        if not 0 <= final_x <= width - shape_width:
            raise RecipeValidationError(
                f"{field} horizontal motion must remain inside the frame"
            )
        if not 0 <= final_y <= height - shape_height:
            raise RecipeValidationError(
                f"{field} vertical motion must remain inside the frame"
            )
        vehicles.append(
            VehicleShape(
                start_x=start_x,
                start_y=start_y,
                width=shape_width,
                height=shape_height,
                delta_x=delta_x,
                delta_y=delta_y,
                color=_validate_color(vehicle["yuv"], f"{field}.yuv"),
            )
        )
    return tuple(vehicles)


def _render_frame(
    recipe: SyntheticMediaRecipe,
    frame_index: int,
) -> bytes:
    width = recipe.width
    height = recipe.height
    pixel_count = width * height
    y_plane = bytearray([recipe.scene.sky.y]) * pixel_count
    u_full = bytearray([recipe.scene.sky.u]) * pixel_count
    v_full = bytearray([recipe.scene.sky.v]) * pixel_count

    _fill_rectangle(
        y_plane,
        u_full,
        v_full,
        width=width,
        height=height,
        left=0,
        top=recipe.scene.horizon_y,
        rectangle_width=width,
        rectangle_height=height - recipe.scene.horizon_y,
        color=recipe.scene.road,
    )
    for mark in recipe.scene.lane_marks:
        _draw_lane_mark(
            y_plane,
            u_full,
            v_full,
            width=width,
            height=height,
            horizon_y=recipe.scene.horizon_y,
            mark=mark,
        )
    for vehicle in recipe.scene.vehicles:
        _draw_vehicle(
            y_plane,
            u_full,
            v_full,
            width=width,
            height=height,
            frame_index=frame_index,
            vehicle=vehicle,
        )

    return bytes(y_plane) + _subsample_420(u_full, width, height) + _subsample_420(
        v_full,
        width,
        height,
    )


def _draw_lane_mark(
    y_plane: bytearray,
    u_plane: bytearray,
    v_plane: bytearray,
    *,
    width: int,
    height: int,
    horizon_y: int,
    mark: LaneMark,
) -> None:
    span = height - 1 - horizon_y
    for y in range(horizon_y, height):
        offset = y - horizon_y
        left, mark_width = _interpolated_lane_span(
            mark,
            offset=offset,
            total=span,
        )
        _fill_rectangle(
            y_plane,
            u_plane,
            v_plane,
            width=width,
            height=height,
            left=left,
            top=y,
            rectangle_width=mark_width,
            rectangle_height=1,
            color=mark.color,
        )


def _interpolated_lane_span(
    mark: LaneMark,
    *,
    offset: int,
    total: int,
) -> tuple[int, int]:
    center = mark.top_x + (
        (mark.bottom_x - mark.top_x) * offset
    ) // total
    width = mark.top_width + (
        (mark.bottom_width - mark.top_width) * offset
    ) // total
    return center - width // 2, width


def _draw_vehicle(
    y_plane: bytearray,
    u_plane: bytearray,
    v_plane: bytearray,
    *,
    width: int,
    height: int,
    frame_index: int,
    vehicle: VehicleShape,
) -> None:
    left = vehicle.start_x + vehicle.delta_x * frame_index
    top = vehicle.start_y + vehicle.delta_y * frame_index
    body_top = top + vehicle.height // 3
    _fill_rectangle(
        y_plane,
        u_plane,
        v_plane,
        width=width,
        height=height,
        left=left,
        top=body_top,
        rectangle_width=vehicle.width,
        rectangle_height=vehicle.height - (body_top - top),
        color=vehicle.color,
    )

    roof_width = max(6, vehicle.width // 2)
    roof_left = left + (vehicle.width - roof_width) // 2
    _fill_rectangle(
        y_plane,
        u_plane,
        v_plane,
        width=width,
        height=height,
        left=roof_left,
        top=top,
        rectangle_width=roof_width,
        rectangle_height=body_top - top + 1,
        color=vehicle.color,
    )

    glass = YuvColor(
        y=max(16, vehicle.color.y - 34),
        u=(vehicle.color.u + 128) // 2,
        v=(vehicle.color.v + 128) // 2,
    )
    inset = max(1, roof_width // 6)
    _fill_rectangle(
        y_plane,
        u_plane,
        v_plane,
        width=width,
        height=height,
        left=roof_left + inset,
        top=top + 2,
        rectangle_width=max(2, roof_width - 2 * inset),
        rectangle_height=max(2, body_top - top - 2),
        color=glass,
    )

    wheel_width = max(3, vehicle.width // 6)
    wheel_height = max(2, vehicle.height // 6)
    wheel_top = top + vehicle.height - wheel_height
    wheel_color = YuvColor(20, 128, 128)
    for wheel_left in (
        left + max(1, vehicle.width // 8),
        left + vehicle.width - max(1, vehicle.width // 8) - wheel_width,
    ):
        _fill_rectangle(
            y_plane,
            u_plane,
            v_plane,
            width=width,
            height=height,
            left=wheel_left,
            top=wheel_top,
            rectangle_width=wheel_width,
            rectangle_height=wheel_height,
            color=wheel_color,
        )


def _fill_rectangle(
    y_plane: bytearray,
    u_plane: bytearray,
    v_plane: bytearray,
    *,
    width: int,
    height: int,
    left: int,
    top: int,
    rectangle_width: int,
    rectangle_height: int,
    color: YuvColor,
) -> None:
    if (
        left < 0
        or top < 0
        or rectangle_width <= 0
        or rectangle_height <= 0
        or left + rectangle_width > width
        or top + rectangle_height > height
    ):
        raise RuntimeError("validated geometry escaped the synthetic frame")
    y_row = bytes([color.y]) * rectangle_width
    u_row = bytes([color.u]) * rectangle_width
    v_row = bytes([color.v]) * rectangle_width
    for row in range(top, top + rectangle_height):
        start = row * width + left
        end = start + rectangle_width
        y_plane[start:end] = y_row
        u_plane[start:end] = u_row
        v_plane[start:end] = v_row


def _subsample_420(
    full_plane: bytearray,
    width: int,
    height: int,
) -> bytes:
    output = bytearray((width // 2) * (height // 2))
    destination = 0
    for y in range(0, height, 2):
        top = y * width
        bottom = (y + 1) * width
        for x in range(0, width, 2):
            total = (
                full_plane[top + x]
                + full_plane[top + x + 1]
                + full_plane[bottom + x]
                + full_plane[bottom + x + 1]
            )
            output[destination] = (total + 2) // 4
            destination += 1
    return bytes(output)


def _require_centered_span(
    center: int,
    span: int,
    frame_width: int,
    field: str,
) -> None:
    left = center - span // 2
    if left < 0 or left + span > frame_width:
        raise RecipeValidationError(f"{field} width must fit inside the frame")


def _require_mapping(raw: object, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RecipeValidationError(f"{field} must be an object")
    if any(type(key) is not str for key in raw):
        raise RecipeValidationError(f"{field} keys must be text")
    return raw


def _require_exact_keys(
    mapping: dict[str, Any],
    expected: set[str],
    field: str,
) -> None:
    actual = set(mapping)
    if actual != expected:
        missing = len(expected - actual)
        unknown = len(actual - expected)
        raise RecipeValidationError(
            f"{field} fields are invalid (missing={missing}, unknown={unknown})"
        )


def _require_integer(
    raw: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(raw) is not int:
        raise RecipeValidationError(f"{field} must be an integer")
    if not minimum <= raw <= maximum:
        raise RecipeValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return raw


def _mapping_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecipeValidationError(
                "recipe JSON contains duplicate keys"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise RecipeValidationError(
        "recipe JSON non-finite numbers are not allowed"
    )


def _absolute_recipe_parts(path: str | Path) -> tuple[str, ...]:
    try:
        requested = Path(path).expanduser()
        absolute = Path(os.path.abspath(os.fspath(requested)))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RecipeValidationError("recipe path is invalid") from error
    parts = absolute.parts
    if not parts or parts[0] != "/" or len(parts) == 1:
        raise RecipeValidationError("recipe path must name a file")
    relative_parts = parts[1:]
    if any(part in {"", ".", ".."} for part in relative_parts):
        raise RecipeValidationError("recipe path is invalid")
    return relative_parts


def _required_open_flags(base: int, *names: str) -> int:
    flags = base
    for name in names:
        value = getattr(os, name, None)
        if value is None:
            raise RecipeValidationError(
                "safe recipe reading requires POSIX open flags"
            )
        flags |= value
    return flags


def _read_bounded_recipe(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    while consumed <= RECIPE_MAX_BYTES:
        try:
            chunk = os.read(
                descriptor,
                min(8192, RECIPE_MAX_BYTES + 1 - consumed),
            )
        except OSError as error:
            raise RecipeValidationError(
                "recipe input could not be read"
            ) from error
        if not chunk:
            break
        chunks.append(chunk)
        consumed += len(chunk)
    if consumed > RECIPE_MAX_BYTES:
        raise RecipeValidationError(
            f"recipe input exceeds {RECIPE_MAX_BYTES} bytes"
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
        raise RecipeValidationError(
            "recipe path changed while being read"
        ) from error
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if (
        stat.S_IFMT(visible.st_mode) != expected_type
        or _entry_identity(visible) != expected
    ):
        raise RecipeValidationError(
            "recipe path changed while being read"
        )


def _close_descriptors(
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
