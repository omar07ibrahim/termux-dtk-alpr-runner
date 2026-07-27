from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import alpr_runner.synthetic_media as media
from alpr_runner.synthetic_media import (
    CHROMA_PLANE_BYTES,
    FRAME_COUNT,
    FRAME_PAYLOAD_BYTES,
    FRAMES_PER_SECOND,
    HEIGHT,
    RECIPE_MAX_BYTES,
    WIDTH,
    Y4M_BYTES,
    Y4M_FRAME_MARKER,
    Y4M_HEADER,
    Y_PLANE_BYTES,
    RecipeValidationError,
    canonical_recipe_bytes,
    decode_recipe,
    default_recipe,
    frame_payloads,
    load_recipe,
    render_y4m,
    validate_recipe,
)

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / "examples" / "synthetic-media-v1.json"


class CanonicalSyntheticMediaTests(unittest.TestCase):
    def test_committed_fixture_is_the_exact_canonical_recipe(self) -> None:
        expected = canonical_recipe_bytes()

        self.assertEqual(FIXTURE.read_bytes(), expected)
        self.assertEqual(load_recipe(FIXTURE), default_recipe())
        self.assertEqual(canonical_recipe_bytes(load_recipe(FIXTURE)), expected)
        self.assertEqual(
            hashlib.sha256(expected).hexdigest(),
            "ab8d7a7518d3d952d725ee96f0de9b6ecfdef7102f4b78ddcb125cab062466f6",
        )

    def test_recipe_is_numeric_only_and_contains_no_free_text_channel(
        self,
    ) -> None:
        raw = default_recipe().to_json()

        def assert_closed(value: object) -> None:
            if isinstance(value, dict):
                self.assertTrue(all(type(key) is str for key in value))
                for child in value.values():
                    assert_closed(child)
            elif isinstance(value, list):
                for child in value:
                    assert_closed(child)
            else:
                self.assertIs(type(value), int)

        assert_closed(raw)
        encoded = canonical_recipe_bytes().lower()
        for forbidden in (
            b"caption",
            b"label",
            b"name",
            b"plate",
            b"person",
            b"recognition",
            b"timestamp",
            b"url",
            b"path",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_y4m_stream_has_exact_header_size_count_and_digest(self) -> None:
        recipe = default_recipe()
        frames = frame_payloads(recipe)
        first = render_y4m(recipe)
        second = render_y4m(recipe)

        self.assertEqual(first, second)
        self.assertEqual(len(first), Y4M_BYTES)
        self.assertTrue(first.startswith(Y4M_HEADER))
        self.assertEqual(len(frames), FRAME_COUNT)
        self.assertTrue(
            all(len(frame) == FRAME_PAYLOAD_BYTES for frame in frames)
        )

        cursor = len(Y4M_HEADER)
        parsed: list[bytes] = []
        for _ in range(FRAME_COUNT):
            self.assertEqual(
                first[cursor : cursor + len(Y4M_FRAME_MARKER)],
                Y4M_FRAME_MARKER,
            )
            cursor += len(Y4M_FRAME_MARKER)
            parsed.append(first[cursor : cursor + FRAME_PAYLOAD_BYTES])
            cursor += FRAME_PAYLOAD_BYTES
        self.assertEqual(cursor, len(first))
        self.assertEqual(tuple(parsed), frames)
        self.assertEqual(
            hashlib.sha256(first).hexdigest(),
            "7614365d1342f1786ab82bb0d3fe07f1d5215cb6159bba40674234346ca7cfeb",
        )

    def test_frame_planes_are_exact_yuv420_and_motion_is_deterministic(
        self,
    ) -> None:
        frames = frame_payloads(default_recipe())
        expected_hashes = {
            0: "d03563d3e00344ce474d2ba8511c8799305d8175e6ae160d132f54fcce977c85",
            8: "7b8e435faee6d819aefdfb09a2c7a74a3bb99d32eca8682176cc076d7e1ebef9",
            17: "69e7621d278b559b4d3a1f07b05629793e355dfb8e22b39548f1245f56af6152",
        }

        self.assertEqual(len(set(frames)), FRAME_COUNT)
        for index, expected in expected_hashes.items():
            self.assertEqual(
                hashlib.sha256(frames[index]).hexdigest(),
                expected,
            )

        for frame in frames:
            y_plane = frame[:Y_PLANE_BYTES]
            u_plane = frame[
                Y_PLANE_BYTES : Y_PLANE_BYTES + CHROMA_PLANE_BYTES
            ]
            v_plane = frame[Y_PLANE_BYTES + CHROMA_PLANE_BYTES :]
            self.assertEqual(len(y_plane), WIDTH * HEIGHT)
            self.assertEqual(len(u_plane), (WIDTH // 2) * (HEIGHT // 2))
            self.assertEqual(len(v_plane), (WIDTH // 2) * (HEIGHT // 2))
            self.assertTrue(all(16 <= value <= 235 for value in y_plane))
            self.assertTrue(all(16 <= value <= 240 for value in u_plane))
            self.assertTrue(all(16 <= value <= 240 for value in v_plane))

    def test_contract_dimensions_and_duration_are_fixed(self) -> None:
        recipe = default_recipe()

        self.assertEqual(
            (
                recipe.width,
                recipe.height,
                recipe.fps,
                recipe.frame_count,
            ),
            (WIDTH, HEIGHT, FRAMES_PER_SECOND, FRAME_COUNT),
        )
        self.assertEqual(FRAME_COUNT / FRAMES_PER_SECOND, 3)
        self.assertEqual(
            FRAME_PAYLOAD_BYTES,
            WIDTH * HEIGHT * 3 // 2,
        )


class RecipeValidationTests(unittest.TestCase):
    def test_strict_validated_fixture_round_trips(self) -> None:
        recipe = default_recipe()

        self.assertEqual(
            decode_recipe(canonical_recipe_bytes(recipe)),
            recipe,
        )
        self.assertEqual(validate_recipe(recipe.to_json()), recipe)

    def test_unknown_boolean_and_fixed_contract_fields_are_rejected(
        self,
    ) -> None:
        baseline = default_recipe().to_json()
        cases: list[tuple[str, dict[str, object]]] = []

        unknown = copy.deepcopy(baseline)
        unknown["caption"] = "private text"
        cases.append(("fields are invalid", unknown))

        boolean = copy.deepcopy(baseline)
        boolean["frame_count"] = True
        cases.append(("must be an integer", boolean))

        width = copy.deepcopy(baseline)
        width["width"] = WIDTH + 2
        cases.append((f"between {WIDTH} and {WIDTH}", width))

        fps = copy.deepcopy(baseline)
        fps["fps"] = FRAMES_PER_SECOND + 1
        cases.append(
            (
                f"between {FRAMES_PER_SECOND} and {FRAMES_PER_SECOND}",
                fps,
            )
        )

        color = copy.deepcopy(baseline)
        color["scene"]["road_yuv"][0] = 255  # type: ignore[index]
        cases.append(("between 16 and 235", color))

        for expected, raw in cases:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(RecipeValidationError, expected),
            ):
                validate_recipe(raw)

    def test_lane_and_vehicle_collections_are_bounded(self) -> None:
        baseline = default_recipe().to_json()

        one_lane = copy.deepcopy(baseline)
        one_lane["scene"]["lane_marks"] = one_lane["scene"][  # type: ignore[index]
            "lane_marks"
        ][:1]
        with self.assertRaisesRegex(
            RecipeValidationError,
            "between 2 and 4",
        ):
            validate_recipe(one_lane)

        many_vehicles = copy.deepcopy(baseline)
        vehicles = many_vehicles["scene"]["vehicles"]  # type: ignore[index]
        many_vehicles["scene"]["vehicles"] = vehicles * 3  # type: ignore[index]
        with self.assertRaisesRegex(
            RecipeValidationError,
            "between 1 and 4",
        ):
            validate_recipe(many_vehicles)

    def test_all_vehicle_motion_must_remain_inside_the_frame(self) -> None:
        horizontal = default_recipe().to_json()
        horizontal["scene"]["vehicles"][0]["delta_x"] = 8  # type: ignore[index]
        with self.assertRaisesRegex(
            RecipeValidationError,
            "horizontal motion",
        ):
            validate_recipe(horizontal)

        vertical = default_recipe().to_json()
        vertical["scene"]["vehicles"][1]["delta_y"] = 4  # type: ignore[index]
        with self.assertRaisesRegex(
            RecipeValidationError,
            "vertical motion",
        ):
            validate_recipe(vertical)

    def test_lane_endpoints_must_fit_inside_the_frame(self) -> None:
        raw = default_recipe().to_json()
        raw["scene"]["lane_marks"][0]["bottom_x"] = 0  # type: ignore[index]
        raw["scene"]["lane_marks"][0]["bottom_width"] = 5  # type: ignore[index]

        with self.assertRaisesRegex(
            RecipeValidationError,
            "fit inside",
        ):
            validate_recipe(raw)

    def test_duplicate_keys_nonfinite_numbers_and_invalid_utf8_are_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(RecipeValidationError, "duplicate"):
            decode_recipe(
                b'{"schema_version":1,"schema_version":1}'
            )
        with self.assertRaisesRegex(RecipeValidationError, "non-finite"):
            decode_recipe(b'{"schema_version":NaN}')
        with self.assertRaisesRegex(RecipeValidationError, "UTF-8"):
            decode_recipe(b"\xff")

    def test_parser_and_payload_limits_fail_closed(self) -> None:
        with self.assertRaisesRegex(RecipeValidationError, "exceeds"):
            decode_recipe(b" " * (RECIPE_MAX_BYTES + 1))

        huge_integer = b'{"schema_version":' + (b"9" * 5000) + b"}"
        with self.assertRaisesRegex(RecipeValidationError, "valid JSON"):
            decode_recipe(huge_integer)

        nested = (b"[" * 10_000) + b"0" + (b"]" * 10_000)
        with self.assertRaisesRegex(RecipeValidationError, "valid JSON"):
            decode_recipe(nested)

    def test_render_revalidates_manually_constructed_recipe(self) -> None:
        invalid = replace(default_recipe(), width=WIDTH + 2)

        with self.assertRaisesRegex(RecipeValidationError, "width"):
            frame_payloads(invalid)
        with self.assertRaises(TypeError):
            render_y4m(object())  # type: ignore[arg-type]


class SecureRecipeLoadTests(unittest.TestCase):
    def _temporary_directory(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(
            prefix=".synthetic-media-test-",
            dir=REPOSITORY,
        )

    def test_regular_canonical_file_loads(self) -> None:
        with self._temporary_directory() as temporary:
            path = Path(temporary) / "recipe.json"
            path.write_bytes(canonical_recipe_bytes())

            self.assertEqual(load_recipe(path), default_recipe())

    def test_symlink_leaf_and_symlinked_ancestor_are_rejected(self) -> None:
        with self._temporary_directory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            recipe = real / "recipe.json"
            recipe.write_bytes(canonical_recipe_bytes())
            leaf_link = root / "leaf.json"
            leaf_link.symlink_to(recipe)
            directory_link = root / "linked"
            directory_link.symlink_to(real, target_is_directory=True)

            for path in (leaf_link, directory_link / recipe.name):
                with (
                    self.subTest(path=path.name),
                    self.assertRaisesRegex(
                        RecipeValidationError,
                        "opened safely",
                    ),
                ):
                    load_recipe(path)

    def test_fifo_and_oversized_regular_file_are_rejected(self) -> None:
        with self._temporary_directory() as temporary:
            root = Path(temporary)
            fifo = root / "recipe.pipe"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(
                RecipeValidationError,
                "regular file",
            ):
                load_recipe(fifo)

            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (RECIPE_MAX_BYTES + 1))
            with self.assertRaisesRegex(
                RecipeValidationError,
                "exceeds",
            ):
                load_recipe(oversized)

    def test_in_place_change_during_read_is_detected(self) -> None:
        with self._temporary_directory() as temporary:
            path = Path(temporary) / "recipe.json"
            path.write_bytes(canonical_recipe_bytes())
            real_read = os.read
            changed = False

            def changing_read(descriptor: int, size: int) -> bytes:
                nonlocal changed
                payload = real_read(descriptor, size)
                if payload and not changed:
                    changed = True
                    path.write_bytes(payload + b" ")
                return payload

            with (
                mock.patch.object(media.os, "read", side_effect=changing_read),
                self.assertRaisesRegex(
                    RecipeValidationError,
                    "changed while being read",
                ),
            ):
                load_recipe(path)

    def test_directory_replacement_is_detected(self) -> None:
        with self._temporary_directory() as temporary:
            root = Path(temporary)
            directory = root / "input"
            directory.mkdir()
            path = directory / "recipe.json"
            path.write_bytes(canonical_recipe_bytes())
            moved = root / "input-original"
            real_read = os.read
            replaced = False

            def replacing_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                payload = real_read(descriptor, size)
                if payload and not replaced:
                    replaced = True
                    os.replace(directory, moved)
                    directory.mkdir()
                    (directory / "recipe.json").write_bytes(payload)
                return payload

            with (
                mock.patch.object(media.os, "read", side_effect=replacing_read),
                self.assertRaisesRegex(
                    RecipeValidationError,
                    "changed while being read",
                ),
            ):
                load_recipe(path)


class StandardLibraryIsolationTests(unittest.TestCase):
    def test_module_import_and_render_work_under_python_s(self) -> None:
        script = (
            "import hashlib,sys;"
            "from alpr_runner.synthetic_media import default_recipe,render_y4m;"
            "payload=render_y4m(default_recipe());"
            "assert len(payload)==414869;"
            "assert hashlib.sha256(payload).hexdigest()=="
            "'7614365d1342f1786ab82bb0d3fe07f1d5215cb6159bba40674234346ca7cfeb';"
            "assert not ({'PIL','numpy','cv2'} & set(sys.modules))"
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=REPOSITORY,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            text=True,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
