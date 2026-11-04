"""Actual model smoke test using Ultralytics' bundled sample, not a webcam.

Run: .venv/Scripts/python.exe scripts/smoke_model.py
This checks inference integration only, not game accuracy on real players.
"""
import json
import time
from pathlib import Path

import cv2
import numpy as np
from ultralytics.utils import ASSETS

from spark_freeze.vision import VisionPipeline

root = Path(__file__).resolve().parents[1]
pipeline = VisionPipeline(str(root / "models" / "yolov8n-pose.pt"), lane_count=1, device="cpu")
pipeline.load()
source = cv2.imread(str(ASSETS / "bus.jpg"))
assert source is not None, "Ultralytics bundled bus.jpg is missing"
poses = pipeline._predict(source)
assert poses, "Actual model did not detect people in the bundled sample"
pose = max(poses, key=lambda p: (p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1]))
x1, y1, x2, y2 = pose.bbox.astype(int)
crop = source[max(0, y1 - 10):min(source.shape[0], y2 + 10),
              max(0, x1 - 10):min(source.shape[1], x2 + 10)]
crop = cv2.resize(crop, (int(crop.shape[1] * 440 / crop.shape[0]), 440))
frame = np.full((540, 960, 3), 70, dtype=np.uint8)
left = (960 - crop.shape[1]) // 2
frame[50:490, left:left + crop.shape[1]] = crop
pipeline.reset_baseline()
results = []
started = time.perf_counter()
for i in range(6):
    result = pipeline.process(frame, i * .2)
    results.append(result)
elapsed = time.perf_counter() - started
last = results[-1]
obs = last.observations[0]
assert last.stable and obs["visible"] and not obs["ambiguous"], obs
assert obs["motion"] < .015, obs
shifted = np.full_like(frame, 70)
shifted[50:490, left + 35:left + 35 + crop.shape[1]] = crop
moved = pipeline.process(shifted, 1.2)
assert moved.stable and moved.observations[0]["motion"] > .045, moved.observations
directory = root / "test-results"
directory.mkdir(exist_ok=True)
cv2.imwrite(str(directory / "model-smoke.jpg"), last.annotated)
cv2.imwrite(str(directory / "model-smoke-movement.jpg"), moved.annotated)
summary = {"test": "actual YOLO pose inference on bundled sample", "people_in_source": len(poses),
           "stable_sample": obs, "translated_sample": moved.observations[0],
           "seconds_for_six_frames": round(elapsed, 3),
           "webcam_validated": False, "robot_validated": False}
(directory / "model-smoke.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
