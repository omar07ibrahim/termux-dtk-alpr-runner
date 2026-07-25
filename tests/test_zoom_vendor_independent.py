from __future__ import annotations

import subprocess
import sys
import unittest

from alpr_runner.dtk import Plate
from alpr_runner.zoom import Box, ZoomController, plate_to_target


class ZoomGeometryTests(unittest.TestCase):
    def test_plate_geometry_drives_bounded_incremental_zoom(self) -> None:
        controller = ZoomController(max_zoom=2.0, max_step=0.25)
        plate = Plate(
            text="SYNTH-01",
            country="synthetic-event",
            confidence=0,
            x=480,
            y=400,
            width=120,
            height=20,
        )
        target = plate_to_target(plate, 1280, 720)
        command = controller.next([target])

        self.assertAlmostEqual(command.zoom_ratio, 1.25)
        self.assertGreaterEqual(command.crop.left, 0)
        self.assertGreaterEqual(command.crop.top, 0)
        self.assertLessEqual(command.crop.right, 1)
        self.assertLessEqual(command.crop.bottom, 1)
        self.assertIs(command.target, target)

    def test_target_selection_prefers_the_weighted_best_box(self) -> None:
        controller = ZoomController()
        weak = Box(0.1, 0.1, 0.2, 0.2, 0.1, "weak")
        strong = Box(0.3, 0.3, 0.7, 0.7, 0.9, "strong")

        command = controller.next([weak, strong])

        self.assertIs(command.target, strong)
        self.assertEqual(command.reason, "zoom target strong")


class VendorIndependentImportTests(unittest.TestCase):
    def test_zoom_imports_without_pillow_and_crop_fails_only_when_called(
        self,
    ) -> None:
        script = r"""
import builtins

real_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == "PIL" or name.startswith("PIL."):
        raise ModuleNotFoundError("blocked Pillow", name=name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
from alpr_runner.zoom import Box, ZoomCommand, ZoomController

command = ZoomCommand(
    zoom_ratio=1.0,
    crop=Box(0.0, 0.0, 1.0, 1.0, 1.0, "crop"),
    target=None,
    reason="test",
    pan_error_x=0.0,
    pan_error_y=0.0,
)
try:
    ZoomController().crop_image(object(), command)
except RuntimeError as error:
    assert str(error) == "Pillow is required only when cropping an image"
else:
    raise AssertionError("crop_image did not require Pillow")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )
