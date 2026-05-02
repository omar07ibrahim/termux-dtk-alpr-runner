from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable

from PIL import Image

from .dtk import Plate


@dataclass
class Box:
    left: float
    top: float
    right: float
    bottom: float
    score: float
    label: str

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_x(self) -> float:
        return (self.left + self.right) * 0.5

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) * 0.5

    def clamp(self) -> "Box":
        return Box(
            left=min(max(self.left, 0.0), 1.0),
            top=min(max(self.top, 0.0), 1.0),
            right=min(max(self.right, 0.0), 1.0),
            bottom=min(max(self.bottom, 0.0), 1.0),
            score=self.score,
            label=self.label,
        )

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class ZoomCommand:
    zoom_ratio: float
    crop: Box
    target: Box | None
    reason: str
    pan_error_x: float
    pan_error_y: float

    def to_json(self) -> dict:
        data = asdict(self)
        data["crop"] = self.crop.to_json()
        data["target"] = self.target.to_json() if self.target else None
        data["motor_hint"] = {
            "pan_left_right": self.pan_error_x,
            "tilt_up_down": self.pan_error_y,
        }
        return data


class ZoomController:
    def __init__(
        self,
        min_zoom: float = 1.0,
        max_zoom: float = 4.0,
        target_height: float = 0.42,
        max_step: float = 0.28,
    ) -> None:
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom
        self.target_height = target_height
        self.max_step = max_step
        self.zoom_ratio = min_zoom
        self.center_x = 0.5
        self.center_y = 0.5

    def next(self, targets: Iterable[Box]) -> ZoomCommand:
        target = self._select_target(list(targets))
        if target is None:
            desired_zoom = self.min_zoom
            reason = "wide scan"
            target_center_x = 0.5
            target_center_y = 0.5
        else:
            desired_zoom = self.zoom_ratio * (self.target_height / max(target.height, 0.04))
            desired_zoom = min(max(desired_zoom, self.min_zoom), self.max_zoom)
            reason = f"zoom target {target.label}"
            target_center_x = target.center_x
            target_center_y = target.center_y

        delta = min(max(desired_zoom - self.zoom_ratio, -self.max_step), self.max_step)
        self.zoom_ratio = min(max(self.zoom_ratio + delta, self.min_zoom), self.max_zoom)
        self.center_x = self.center_x * 0.72 + target_center_x * 0.28
        self.center_y = self.center_y * 0.72 + target_center_y * 0.28
        crop = self._crop_box()
        return ZoomCommand(
            zoom_ratio=self.zoom_ratio,
            crop=crop,
            target=target,
            reason=reason,
            pan_error_x=(target_center_x - 0.5) if target else 0.0,
            pan_error_y=(target_center_y - 0.5) if target else 0.0,
        )

    def manual(self, targets: Iterable[Box], delta: float) -> ZoomCommand:
        target = self._select_target(list(targets))
        self.zoom_ratio = min(max(self.zoom_ratio + delta, self.min_zoom), self.max_zoom)
        if target is None:
            reason = "manual zoom"
            target_center_x = self.center_x
            target_center_y = self.center_y
        else:
            reason = f"manual zoom target {target.label}"
            target_center_x = target.center_x
            target_center_y = target.center_y
            self.center_x = self.center_x * 0.72 + target_center_x * 0.28
            self.center_y = self.center_y * 0.72 + target_center_y * 0.28

        return ZoomCommand(
            zoom_ratio=self.zoom_ratio,
            crop=self._crop_box(),
            target=target,
            reason=reason,
            pan_error_x=(target_center_x - 0.5) if target else 0.0,
            pan_error_y=(target_center_y - 0.5) if target else 0.0,
        )

    def crop_image(self, image: Image.Image, command: ZoomCommand) -> Image.Image:
        crop = command.crop
        left = int(crop.left * image.width)
        top = int(crop.top * image.height)
        right = max(left + 1, int(crop.right * image.width))
        bottom = max(top + 1, int(crop.bottom * image.height))
        return image.crop((left, top, right, bottom)).resize(image.size, Image.Resampling.BILINEAR)

    def _select_target(self, targets: list[Box]) -> Box | None:
        if not targets:
            return None
        return max(
            targets,
            key=lambda box: (
                box.score * 0.45
                + box.area * 1.8
                + (1.0 - abs(box.center_x - 0.5)) * 0.12
                + (1.0 - abs(box.center_y - 0.52)) * 0.08
            ),
        )

    def _crop_box(self) -> Box:
        crop_width = 1.0 / max(self.zoom_ratio, 1.0)
        crop_height = crop_width
        left = self.center_x - crop_width * 0.5
        top = self.center_y - crop_height * 0.5
        if left < 0:
            left = 0.0
        if top < 0:
            top = 0.0
        if left + crop_width > 1:
            left = 1.0 - crop_width
        if top + crop_height > 1:
            top = 1.0 - crop_height
        return Box(left, top, left + crop_width, top + crop_height, self.zoom_ratio, "software-zoom").clamp()


def plate_to_target(plate: Plate, image_width: int, image_height: int) -> Box:
    left = plate.x / image_width
    top = plate.y / image_height
    right = (plate.x + plate.width) / image_width
    bottom = (plate.y + plate.height) / image_height
    width = right - left
    height = bottom - top
    cx = (left + right) * 0.5
    # Estimate a vehicle-sized ROI around the plate. This is intentionally wide
    # so a later motor controller aims at the car, not only the plate.
    return Box(
        left=cx - width * 2.3,
        top=top - height * 5.0,
        right=cx + width * 2.3,
        bottom=bottom + height * 2.0,
        score=max(0.1, plate.confidence / 100.0),
        label=f"plate:{plate.text or 'unknown'}",
    ).clamp()
