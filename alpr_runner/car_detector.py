from __future__ import annotations

from pathlib import Path

from PIL import Image

from .zoom import Box


VEHICLE_CLASS_IDS = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


class OptionalYoloCarDetector:
    def __init__(self, model_path: str | Path | None, confidence: float = 0.30) -> None:
        self.model_path = Path(model_path).expanduser().resolve() if model_path else None
        self.confidence = confidence
        self.session = None
        self.input_name = None
        self.np = None
        if self.model_path and self.model_path.exists():
            import numpy as np
            import onnxruntime as ort

            self.np = np
            self.session = ort.InferenceSession(str(self.model_path), providers=["CPUExecutionProvider"])
            self.input_name = self.session.get_inputs()[0].name

    @property
    def enabled(self) -> bool:
        return self.session is not None

    def detect(self, image: Image.Image) -> list[Box]:
        if self.session is None or self.input_name is None or self.np is None:
            return []
        np = self.np
        canvas, scale, pad_x, pad_y = self._letterbox(image)
        array = np.asarray(canvas, dtype=np.float32) / 255.0
        input_tensor = np.transpose(array, (2, 0, 1))[None, ...]
        output = self.session.run(None, {self.input_name: input_tensor})[0]
        output = output[0]
        rows = output.T if output.shape[0] <= 128 else output
        boxes = []
        for row in rows:
            if len(row) < 8:
                continue
            class_scores = row[4:]
            class_id = int(np.argmax(class_scores))
            if class_id not in VEHICLE_CLASS_IDS:
                continue
            score = float(class_scores[class_id])
            if score < self.confidence:
                continue
            cx, cy, width, height = map(float, row[:4])
            x1 = (cx - width * 0.5 - pad_x) / scale / image.width
            y1 = (cy - height * 0.5 - pad_y) / scale / image.height
            x2 = (cx + width * 0.5 - pad_x) / scale / image.width
            y2 = (cy + height * 0.5 - pad_y) / scale / image.height
            boxes.append(Box(x1, y1, x2, y2, score, VEHICLE_CLASS_IDS[class_id]).clamp())
        return self._nms(sorted(boxes, key=lambda box: box.score, reverse=True))

    @staticmethod
    def _letterbox(image: Image.Image, size: int = 640):
        scale = min(size / image.width, size / image.height)
        resized_width = max(1, round(image.width * scale))
        resized_height = max(1, round(image.height * scale))
        resized = image.resize((resized_width, resized_height), Image.Resampling.BILINEAR)
        canvas = Image.new("RGB", (size, size), (114, 114, 114))
        pad_x = (size - resized_width) // 2
        pad_y = (size - resized_height) // 2
        canvas.paste(resized, (pad_x, pad_y))
        return canvas, scale, pad_x, pad_y

    @staticmethod
    def _iou(a: Box, b: Box) -> float:
        ix1 = max(a.left, b.left)
        iy1 = max(a.top, b.top)
        ix2 = min(a.right, b.right)
        iy2 = min(a.bottom, b.bottom)
        intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        union = a.area + b.area - intersection
        return intersection / union if union > 0 else 0.0

    def _nms(self, boxes: list[Box]) -> list[Box]:
        kept: list[Box] = []
        for box in boxes:
            if all(self._iou(box, other) <= 0.45 for other in kept):
                kept.append(box)
            if len(kept) >= 12:
                break
        return kept
