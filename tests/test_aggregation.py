from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from alpr_runner.aggregation import (
    PlateRegistry,
    local_time,
    normalize_plate_text,
)


@dataclass(frozen=True)
class FakePlate:
    text: str
    country: str = "test"
    confidence: int = 0
    vehicle_make: str = ""
    vehicle_model: str = ""
    vehicle_confidence: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


class TimestampTests(unittest.TestCase):
    def test_normalization_preserves_the_production_registry_key(self) -> None:
        self.assertEqual(normalize_plate_text(" ab-12.cd "), "AB12CD")
        self.assertEqual(normalize_plate_text("---"), "")

    def test_local_time_accepts_epoch_zero_and_an_injected_clock(self) -> None:
        self.assertEqual(
            local_time(0, converter=time.gmtime),
            "1970-01-01 00:00:00",
        )
        self.assertEqual(
            local_time(clock=lambda: 0, converter=time.gmtime),
            "1970-01-01 00:00:00",
        )


class PlateRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = {"label": "target"}
        self.zoom = {"zoom_ratio": 1.25}
        self.frame = {"height": 720, "width": 1280}

    def test_cross_camera_counts_and_best_metadata_are_preserved(self) -> None:
        clock = MutableClock(10)
        registry = PlateRegistry(
            print_every=2,
            print_min_seconds=0,
            clock=clock,
            timestamp_formatter=lambda value: f"T{value:.0f}",
        )

        first, first_print = registry.record(
            "cam-a",
            FakePlate("ab-12", confidence=30, vehicle_make="old"),
            self.target,
            self.zoom,
            self.frame,
        )
        clock.value = 11
        second, second_print = registry.record(
            "cam-b",
            FakePlate(
                "AB 12",
                country="new",
                confidence=80,
                vehicle_make="updated",
            ),
            self.target,
            self.zoom,
            self.frame,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertTrue(first_print)
        self.assertTrue(second_print)
        self.assertTrue(first["is_new"])
        self.assertFalse(second["is_new"])
        self.assertEqual(second["cameras"], {"cam-a": 1, "cam-b": 1})

        snapshot = registry.snapshot()
        self.assertEqual(snapshot["total_unique_plates"], 1)
        self.assertEqual(snapshot["total_recognitions"], 2)
        entry = snapshot["plates"][0]
        self.assertEqual(entry["key"], "AB12")
        self.assertEqual(entry["text"], "AB 12")
        self.assertEqual(entry["best_confidence"], 80)
        self.assertEqual(entry["vehicle_make"], "updated")
        self.assertEqual(entry["first_seen"], "T10")
        self.assertEqual(entry["last_seen"], "T11")

    def test_elapsed_print_threshold_uses_the_injected_clock(self) -> None:
        clock = MutableClock(100)
        registry = PlateRegistry(
            print_every=0,
            print_min_seconds=5,
            clock=clock,
            timestamp_formatter=lambda value: f"T{value:.0f}",
        )

        _, first_print = registry.record(
            "cam-a",
            FakePlate("SAME"),
            self.target,
            self.zoom,
            self.frame,
        )
        clock.value = 104.99
        _, early_print = registry.record(
            "cam-a",
            FakePlate("SAME"),
            self.target,
            self.zoom,
            self.frame,
        )
        clock.value = 105
        _, threshold_print = registry.record(
            "cam-a",
            FakePlate("SAME"),
            self.target,
            self.zoom,
            self.frame,
        )

        self.assertTrue(first_print)
        self.assertFalse(early_print)
        self.assertTrue(threshold_print)

    def test_empty_normalized_value_is_ignored_without_reading_time(self) -> None:
        clock = MutableClock(100)
        registry = PlateRegistry(0, 0, clock=clock)

        event, should_print = registry.record(
            "cam-a",
            FakePlate(" -- "),
            self.target,
            self.zoom,
            self.frame,
        )

        self.assertIsNone(event)
        self.assertFalse(should_print)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(registry.snapshot()["total_recognitions"], 0)

    def test_snapshot_order_is_count_then_normalized_key(self) -> None:
        registry = PlateRegistry(
            0,
            0,
            clock=lambda: 0,
            timestamp_formatter=lambda _value: "T0",
        )
        for token in ("B-2", "A-1", "B-2", "C-3"):
            registry.record(
                "cam-a",
                FakePlate(token),
                self.target,
                self.zoom,
                self.frame,
            )

        self.assertEqual(
            [entry["key"] for entry in registry.snapshot()["plates"]],
            ["B2", "A1", "C3"],
        )

    def test_concurrent_records_are_not_lost(self) -> None:
        registry = PlateRegistry(
            0,
            0,
            clock=lambda: 0,
            timestamp_formatter=lambda _value: "T0",
        )

        def record(index: int) -> None:
            registry.record(
                f"cam-{index % 4}",
                FakePlate("THREAD-01"),
                self.target,
                self.zoom,
                self.frame,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(200)))

        snapshot = registry.snapshot()
        self.assertEqual(snapshot["total_recognitions"], 200)
        self.assertEqual(
            snapshot["plates"][0]["cameras"],
            {"cam-0": 50, "cam-1": 50, "cam-2": 50, "cam-3": 50},
        )

    def test_inputs_events_and_snapshots_cannot_mutate_registry_state(
        self,
    ) -> None:
        registry = PlateRegistry(
            0,
            0,
            clock=lambda: 0,
            timestamp_formatter=lambda _value: "T0",
        )
        target = {"nested": {"label": "original"}}
        zoom = {"history": [1.25]}
        frame = {"height": 720, "width": 1280}

        event, _ = registry.record(
            "cam-a",
            FakePlate("SAFE-01"),
            target,
            zoom,
            frame,
        )
        assert event is not None
        target["nested"]["label"] = "mutated"
        zoom["history"].append(4.0)
        frame["width"] = 1
        event["target"]["nested"]["label"] = "returned-event-mutation"
        first_snapshot = registry.snapshot()
        first_snapshot["plates"][0]["last_target"]["nested"]["label"] = (
            "snapshot-mutation"
        )
        first_snapshot["last_event"]["zoom"]["history"].append(9.0)

        fresh = registry.snapshot()
        self.assertEqual(
            fresh["plates"][0]["last_target"],
            {"nested": {"label": "original"}},
        )
        self.assertEqual(fresh["plates"][0]["last_zoom"], {"history": [1.25]})
        self.assertEqual(
            fresh["plates"][0]["last_frame_size"],
            {"height": 720, "width": 1280},
        )
        self.assertEqual(
            fresh["last_event"]["target"],
            {"nested": {"label": "original"}},
        )
        self.assertEqual(fresh["last_event"]["zoom"], {"history": [1.25]})
