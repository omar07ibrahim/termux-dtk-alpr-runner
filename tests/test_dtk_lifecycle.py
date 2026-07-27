from __future__ import annotations

import ctypes
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpr_runner.dtk import DtkLicenseError, DtkLpr


def _fake_native_library(*, license_state: int) -> SimpleNamespace:
    names = (
        "LPRParams_set_Countries",
        "LPRParams_set_MinPlateWidth",
        "LPRParams_set_MaxPlateWidth",
        "LPRParams_set_FormatPlateText",
        "LPRParams_set_RecognizeMakeModel",
        "LPRParams_set_NumThreads",
        "LPRParams_set_FPSLimit",
        "LPRParams_set_ResultConfirmationsCount",
        "LPRParams_set_ResultAccumulationTime",
        "LPRParams_set_ResultDuplicatesTimeout",
        "LPRParams_set_ResultSelectionMethod",
    )
    values = {name: Mock() for name in names}
    values.update(
        {
            "LPRParams_Create": Mock(return_value=101),
            "LPREngine_Create": Mock(return_value=202),
            "LPREngine_IsLicensed": Mock(return_value=license_state),
            "LPREngine_Destroy": Mock(),
            "LPRParams_Destroy": Mock(),
        }
    )
    return SimpleNamespace(**values)


class DtkLifecycleTests(unittest.TestCase):
    def test_license_failure_destroys_engine_then_params(self) -> None:
        library = _fake_native_library(license_state=1)
        events: list[str] = []
        library.LPREngine_Destroy.side_effect = (
            lambda _engine: events.append("engine")
        )
        library.LPRParams_Destroy.side_effect = (
            lambda _params: events.append("params")
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(ctypes, "CDLL", return_value=library),
            patch("alpr_runner.dtk.os.chdir"),
            patch.object(DtkLpr, "_bind_api"),
            self.assertRaises(DtkLicenseError),
        ):
            DtkLpr("synthetic-sdk")

        library.LPREngine_Destroy.assert_called_once_with(202)
        library.LPRParams_Destroy.assert_called_once_with(101)
        self.assertEqual(events, ["engine", "params"])


if __name__ == "__main__":
    unittest.main()
