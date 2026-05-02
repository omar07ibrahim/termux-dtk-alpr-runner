from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


class DtkError(RuntimeError):
    pass


class DtkLicenseError(DtkError):
    pass


@dataclass
class Plate:
    text: str
    country: str
    confidence: int
    x: int
    y: int
    width: int
    height: int
    vehicle_make: str = ""
    vehicle_model: str = ""
    vehicle_view: int = 0
    vehicle_confidence: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class DtkLpr:
    def __init__(
        self,
        lib_dir: str | Path,
        countries: str = "",
        min_plate_width: int = 60,
        max_plate_width: int = 900,
        recognize_make_model: bool = True,
        require_license: bool = True,
        video: bool = False,
        plate_callback: Any | None = None,
        num_threads: int = 0,
        fps_limit: int = 0,
        result_confirmations: int = 1,
        result_accumulation_ms: int = 0,
        duplicate_timeout_ms: int = 1000,
    ) -> None:
        self.lib_dir = Path(lib_dir).expanduser().resolve()
        self.lib_path = self.lib_dir / "libDTKLPR.so"
        if not self.lib_path.exists():
            raise DtkError(f"Missing {self.lib_path}")

        # DTK ships all dependent .so files side-by-side and sets RPATH=$ORIGIN.
        # chdir also helps the engine find DTKLPR.dat in the same directory.
        os.chdir(self.lib_dir)
        self.lib = ctypes.CDLL(str(self.lib_path), mode=ctypes.RTLD_GLOBAL)
        self._bind_api()

        self.params = self.lib.LPRParams_Create()
        if not self.params:
            raise DtkError("LPRParams_Create failed")
        self.lib.LPRParams_set_Countries(self.params, countries.encode("utf-8"))
        self.lib.LPRParams_set_MinPlateWidth(self.params, int(min_plate_width))
        self.lib.LPRParams_set_MaxPlateWidth(self.params, int(max_plate_width))
        self.lib.LPRParams_set_FormatPlateText(self.params, True)
        self.lib.LPRParams_set_RecognizeMakeModel(self.params, bool(recognize_make_model))
        self.lib.LPRParams_set_NumThreads(self.params, int(num_threads))
        self.lib.LPRParams_set_FPSLimit(self.params, int(fps_limit))
        self.lib.LPRParams_set_ResultConfirmationsCount(self.params, int(result_confirmations))
        self.lib.LPRParams_set_ResultAccumulationTime(self.params, int(result_accumulation_ms))
        self.lib.LPRParams_set_ResultDuplicatesTimeout(self.params, int(duplicate_timeout_ms))
        self.lib.LPRParams_set_ResultSelectionMethod(self.params, 1)

        self._plate_callback_ref = plate_callback
        self.engine = self.lib.LPREngine_Create(self.params, bool(video), plate_callback)
        if not self.engine:
            raise DtkError("LPREngine_Create failed")

        license_state = self.lib.LPREngine_IsLicensed(self.engine)
        if require_license and license_state != 0:
            raise DtkLicenseError(
                f"DTK license check failed: LPREngine_IsLicensed()={license_state}. "
                "Activate the official ARM64 SDK license inside Ubuntu/Termux."
            )

    def close(self) -> None:
        engine = getattr(self, "engine", None)
        if engine:
            self.lib.LPREngine_Destroy(engine)
            self.engine = None
        params = getattr(self, "params", None)
        if params:
            self.lib.LPRParams_Destroy(params)
            self.params = None

    def __enter__(self) -> "DtkLpr":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def version(self) -> str:
        return self._string_call(self.lib.LPREngine_GetLibraryVersion, 128)

    def system_id(self) -> str:
        return self._string_call(self.lib.LPREngine_GetSystemID, 128)

    def read_file(self, image_path: str | Path) -> tuple[list[Plate], int]:
        path = str(Path(image_path).expanduser().resolve())
        result = self.lib.LPREngine_ReadFromFile(self.engine, path.encode("utf-8"))
        if not result:
            raise DtkError("LPREngine_ReadFromFile returned null")

        try:
            error = self.lib.LPRResult_GetErrorCode(result)
            if error == 1:
                raise DtkLicenseError("DTK result error: no license")
            if error == 2:
                raise DtkLicenseError("DTK result error: no available channels")
            if error != 0:
                raise DtkError(f"DTK result error code: {error}")

            count = self.lib.LPRResult_GetPlatesCount(result)
            processing_ms = self.lib.LPRResult_GetProcessingTime(result)
            plates = []
            for index in range(count):
                plate_handle = self.lib.LPRResult_GetPlate(result, index)
                if plate_handle:
                    try:
                        plates.append(self._extract_plate(plate_handle))
                    finally:
                        self.lib.LicensePlate_Destroy(plate_handle)
            return plates, processing_ms
        finally:
            self.lib.LPRResult_Destroy(result)

    def _extract_plate(self, plate: ctypes.c_void_p) -> Plate:
        return Plate(
            text=self._plate_string(self.lib.LicensePlate_GetText, plate, 128),
            country=self._plate_string(self.lib.LicensePlate_GetCountryCode, plate, 16),
            confidence=self.lib.LicensePlate_GetConfidence(plate),
            x=self.lib.LicensePlate_GetX(plate),
            y=self.lib.LicensePlate_GetY(plate),
            width=self.lib.LicensePlate_GetWidth(plate),
            height=self.lib.LicensePlate_GetHeight(plate),
            vehicle_make=self._plate_string(self.lib.LicensePlate_GetVehicleMake, plate, 96),
            vehicle_model=self._plate_string(self.lib.LicensePlate_GetVehicleModel, plate, 96),
            vehicle_view=self.lib.LicensePlate_GetVehicleView(plate),
            vehicle_confidence=self.lib.LicensePlate_GetVehicleMakeModelConfidence(plate),
        )

    def _string_call(self, func: Any, limit: int) -> str:
        size = func(None, 0)
        size = max(size, limit)
        buf = ctypes.create_string_buffer(size)
        func(buf, size)
        return buf.value.decode("utf-8", errors="replace")

    @staticmethod
    def _plate_string(func: Any, plate: ctypes.c_void_p, limit: int) -> str:
        buf = ctypes.create_string_buffer(limit)
        func(plate, buf, limit)
        return buf.value.decode("utf-8", errors="replace")

    def _bind_api(self) -> None:
        c_void_p = ctypes.c_void_p
        c_char_p = ctypes.c_char_p
        c_int = ctypes.c_int
        c_bool = ctypes.c_bool

        self.lib.LPRParams_Create.argtypes = []
        self.lib.LPRParams_Create.restype = c_void_p
        self.lib.LPRParams_Destroy.argtypes = [c_void_p]
        self.lib.LPRParams_set_Countries.argtypes = [c_void_p, c_char_p]
        self.lib.LPRParams_set_MinPlateWidth.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_MaxPlateWidth.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_FormatPlateText.argtypes = [c_void_p, c_bool]
        self.lib.LPRParams_set_RecognizeMakeModel.argtypes = [c_void_p, c_bool]
        self.lib.LPRParams_set_NumThreads.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_FPSLimit.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_ResultConfirmationsCount.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_ResultAccumulationTime.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_ResultDuplicatesTimeout.argtypes = [c_void_p, c_int]
        self.lib.LPRParams_set_ResultSelectionMethod.argtypes = [c_void_p, c_int]

        self.lib.LPREngine_Create.argtypes = [c_void_p, c_bool, c_void_p]
        self.lib.LPREngine_Create.restype = c_void_p
        self.lib.LPREngine_Destroy.argtypes = [c_void_p]
        self.lib.LPREngine_SetFrameProcessingCompletedCallback.argtypes = [c_void_p, c_void_p]
        self.lib.LPREngine_IsLicensed.argtypes = [c_void_p]
        self.lib.LPREngine_IsLicensed.restype = c_int
        self.lib.LPREngine_ReadFromFile.argtypes = [c_void_p, c_char_p]
        self.lib.LPREngine_ReadFromFile.restype = c_void_p
        self.lib.LPREngine_PutFrame.argtypes = [c_void_p, c_void_p, ctypes.c_int64]
        self.lib.LPREngine_PutFrame.restype = c_int
        self.lib.LPREngine_GetLibraryVersion.argtypes = [c_char_p, c_int]
        self.lib.LPREngine_GetLibraryVersion.restype = c_int
        self.lib.LPREngine_GetSystemID.argtypes = [c_char_p, c_int]
        self.lib.LPREngine_GetSystemID.restype = c_int

        self.lib.LPRResult_Destroy.argtypes = [c_void_p]
        self.lib.LPRResult_GetErrorCode.argtypes = [c_void_p]
        self.lib.LPRResult_GetErrorCode.restype = c_int
        self.lib.LPRResult_GetPlatesCount.argtypes = [c_void_p]
        self.lib.LPRResult_GetPlatesCount.restype = c_int
        self.lib.LPRResult_GetPlate.argtypes = [c_void_p, c_int]
        self.lib.LPRResult_GetPlate.restype = c_void_p
        self.lib.LPRResult_GetProcessingTime.argtypes = [c_void_p]
        self.lib.LPRResult_GetProcessingTime.restype = c_int

        self.lib.LicensePlate_Destroy.argtypes = [c_void_p]
        self.lib.LicensePlate_GetText.argtypes = [c_void_p, c_char_p, c_int]
        self.lib.LicensePlate_GetCountryCode.argtypes = [c_void_p, c_char_p, c_int]
        self.lib.LicensePlate_GetConfidence.argtypes = [c_void_p]
        self.lib.LicensePlate_GetConfidence.restype = c_int
        self.lib.LicensePlate_GetX.argtypes = [c_void_p]
        self.lib.LicensePlate_GetX.restype = c_int
        self.lib.LicensePlate_GetY.argtypes = [c_void_p]
        self.lib.LicensePlate_GetY.restype = c_int
        self.lib.LicensePlate_GetWidth.argtypes = [c_void_p]
        self.lib.LicensePlate_GetWidth.restype = c_int
        self.lib.LicensePlate_GetHeight.argtypes = [c_void_p]
        self.lib.LicensePlate_GetHeight.restype = c_int
        self.lib.LicensePlate_GetVehicleMake.argtypes = [c_void_p, c_char_p, c_int]
        self.lib.LicensePlate_GetVehicleModel.argtypes = [c_void_p, c_char_p, c_int]
        self.lib.LicensePlate_GetVehicleView.argtypes = [c_void_p]
        self.lib.LicensePlate_GetVehicleView.restype = c_int
        self.lib.LicensePlate_GetVehicleMakeModelConfidence.argtypes = [c_void_p]
        self.lib.LicensePlate_GetVehicleMakeModelConfidence.restype = c_int
