# Optional Models

DTK handles plate recognition. A separate car detector is optional.

If you want YOLO-based car targeting before a plate is readable, place a COCO-style YOLO ONNX model here:

```text
models/car_yolo.onnx
```

The runner expects YOLOv8/YOLO11-style output shaped like `[1, 84, N]` or `[1, N, 84]`.
COCO class ids used as vehicle targets:

- `2`: car
- `3`: motorcycle
- `5`: bus
- `7`: truck

If this model is absent, the runner uses DTK plate boxes as target anchors and estimates a vehicle-sized ROI around each plate.
