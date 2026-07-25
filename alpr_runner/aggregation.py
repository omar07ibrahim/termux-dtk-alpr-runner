from __future__ import annotations

import copy
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol


class PlateRecord(Protocol):
    """The vendor-neutral plate fields consumed by the aggregation layer."""

    @property
    def text(self) -> str:
        ...

    @property
    def country(self) -> str:
        ...

    @property
    def confidence(self) -> int:
        ...

    @property
    def vehicle_make(self) -> str:
        ...

    @property
    def vehicle_model(self) -> str:
        ...

    @property
    def vehicle_confidence(self) -> int:
        ...

    def to_json(self) -> dict[str, Any]:
        ...


Clock = Callable[[], float]
TimestampFormatter = Callable[[float], str]


def normalize_plate_text(value: str) -> str:
    """Return the stable registry key used by the production runner."""

    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def local_time(
    timestamp: float | None = None,
    *,
    clock: Clock | None = None,
    converter: Callable[[float], time.struct_time] | None = None,
) -> str:
    """Format a local timestamp while allowing deterministic clocks in tests."""

    current_clock = clock if clock is not None else time.time
    local_converter = converter if converter is not None else time.localtime
    instant = current_clock() if timestamp is None else timestamp
    return time.strftime("%Y-%m-%d %H:%M:%S", local_converter(instant))


class PlateRegistry:
    """Thread-safe cross-camera aggregation shared by real and synthetic inputs."""

    def __init__(
        self,
        print_every: int,
        print_min_seconds: float,
        *,
        clock: Clock | None = None,
        timestamp_formatter: TimestampFormatter | None = None,
    ) -> None:
        self.print_every = max(0, print_every)
        self.print_min_seconds = max(0.0, print_min_seconds)
        self._clock = clock if clock is not None else time.time
        self._timestamp_formatter = (
            timestamp_formatter
            if timestamp_formatter is not None
            else local_time
        )
        self.lock = threading.RLock()
        self.entries: dict[str, dict[str, Any]] = {}
        self.last_event: dict[str, Any] | None = None
        self.last_print: dict[str, dict[str, float | int]] = {}

    def record(
        self,
        camera_id: str,
        plate: PlateRecord,
        target: dict[str, Any],
        zoom: dict[str, Any],
        frame_size: dict[str, int],
    ) -> tuple[dict[str, Any] | None, bool]:
        key = normalize_plate_text(plate.text)
        if not key:
            return None, False

        now = self._clock()
        now_text = self._timestamp_formatter(now)
        target_snapshot = copy.deepcopy(target)
        zoom_snapshot = copy.deepcopy(zoom)
        frame_snapshot = copy.deepcopy(frame_size)
        plate_snapshot = copy.deepcopy(plate.to_json())
        with self.lock:
            entry = self.entries.get(key)
            is_new = entry is None
            if entry is None:
                entry = {
                    "key": key,
                    "text": plate.text,
                    "country": plate.country,
                    "count": 0,
                    "first_seen": now_text,
                    "last_seen": now_text,
                    "last_camera": camera_id,
                    "cameras": {},
                    "best_confidence": plate.confidence,
                    "vehicle_make": plate.vehicle_make,
                    "vehicle_model": plate.vehicle_model,
                    "vehicle_confidence": plate.vehicle_confidence,
                    "last_target": copy.deepcopy(target_snapshot),
                    "last_zoom": copy.deepcopy(zoom_snapshot),
                    "last_frame_size": copy.deepcopy(frame_snapshot),
                }
                self.entries[key] = entry

            entry["count"] += 1
            entry["last_seen"] = now_text
            entry["last_camera"] = camera_id
            entry["cameras"][camera_id] = (
                entry["cameras"].get(camera_id, 0) + 1
            )
            entry["last_target"] = copy.deepcopy(target_snapshot)
            entry["last_zoom"] = copy.deepcopy(zoom_snapshot)
            entry["last_frame_size"] = copy.deepcopy(frame_snapshot)

            if plate.confidence >= int(entry.get("best_confidence", 0)):
                entry["text"] = plate.text
                entry["country"] = plate.country
                entry["best_confidence"] = plate.confidence
                entry["vehicle_make"] = plate.vehicle_make
                entry["vehicle_model"] = plate.vehicle_model
                entry["vehicle_confidence"] = plate.vehicle_confidence

            event = {
                "time": now_text,
                "camera": camera_id,
                "key": key,
                "is_new": is_new,
                "count": entry["count"],
                "plate": plate_snapshot,
                "target": target_snapshot,
                "zoom": zoom_snapshot,
                "frame_size": frame_snapshot,
                "cameras": dict(entry["cameras"]),
            }
            self.last_event = copy.deepcopy(event)
            should_print = self._should_print_locked(
                key,
                int(entry["count"]),
                now,
                is_new,
            )
            if should_print:
                self.last_print[key] = {
                    "count": int(entry["count"]),
                    "time": now,
                }
            return copy.deepcopy(event), should_print

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        plates = sorted(
            (copy.deepcopy(entry) for entry in self.entries.values()),
            key=lambda item: (-int(item["count"]), str(item["key"])),
        )
        return {
            "total_unique_plates": len(plates),
            "total_recognitions": sum(int(item["count"]) for item in plates),
            "last_event": copy.deepcopy(self.last_event),
            "plates": plates,
        }

    def _should_print_locked(
        self,
        key: str,
        count: int,
        now: float,
        is_new: bool,
    ) -> bool:
        if is_new:
            return True
        if self.print_every > 0 and count % self.print_every == 0:
            return True
        previous = self.last_print.get(key)
        return bool(
            previous
            and self.print_min_seconds > 0
            and now - float(previous["time"]) >= self.print_min_seconds
        )
