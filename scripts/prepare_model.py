"""Download and check the official pose model before an offline demonstration."""
from pathlib import Path

from ultralytics import YOLO

root = Path(__file__).resolve().parents[1]
target = root / "models" / "yolov8n-pose.pt"
target.parent.mkdir(parents=True, exist_ok=True)
model = YOLO(str(target))
assert model.task == "pose"
print(f"Pose model ready: {target}")
