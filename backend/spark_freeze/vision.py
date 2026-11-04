"""Local YOLO pose observations for a fixed-lane red-light/green-light game.

This module deliberately does not claim person re-identification. A player keeps
their lane for the round. Occlusion, shared lanes and crossing make that lane
unjudgeable until ``reset_baseline``. Calling code owns game state and penalties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np


@dataclass
class FrameAnalysis:
    observations: list[dict[str, Any]]
    stable: bool
    annotated: np.ndarray
    width: int
    height: int
    baseline_created: list[int] = field(default_factory=list)
    baseline_reset: bool = False
    stability_reason: str = ""


@dataclass
class _Pose:
    bbox: np.ndarray
    points: np.ndarray
    scores: np.ndarray
    confidence: float


@dataclass
class _Baseline:
    pose: _Pose
    height: float


def _numpy(value: Any) -> np.ndarray:
    """Accept actual torch tensors and plain arrays used by offline tests."""
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


class _CameraGuard:
    """Check background motion, excluding detected people in both images.

    Features are checked against the previous frame AND a fixed view reference,
    so a slow camera drift cannot evade the movement gate. A low-texture scene
    uses background brightness difference as a weaker, explicitly labelled
    fallback. A scene with too little visible background is not judgeable.
    """

    def __init__(self) -> None:
        self.previous: np.ndarray | None = None
        self.previous_mask: np.ndarray | None = None
        self.reference: np.ndarray | None = None
        self.reference_mask: np.ndarray | None = None
        self.quiet_since: float | None = None
        self.last_now: float | None = None
        self.settle_seconds = 0.35

    def reset_reference(self) -> None:
        self.reference = None
        self.reference_mask = None

    @staticmethod
    def _compare(old: np.ndarray, new: np.ndarray, mask: np.ndarray) -> tuple[bool, str]:
        usable = int(np.count_nonzero(mask))
        if usable < max(300, int(mask.size * 0.10)):
            return False, "insufficient_background"
        features = cv2.goodFeaturesToTrack(
            old, maxCorners=160, qualityLevel=0.02, minDistance=7, mask=mask
        )
        if features is not None and len(features) >= 8:
            shifted, status, _ = cv2.calcOpticalFlowPyrLK(
                old, new, features, None,
                winSize=(21, 21), maxLevel=2,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
            )
            if shifted is not None and status is not None:
                keep = status.reshape(-1).astype(bool)
                old_points = features.reshape(-1, 2)[keep]
                new_points = shifted.reshape(-1, 2)[keep]
                if len(old_points) >= 8:
                    # Reject tracks that landed inside a current player mask.
                    xy = np.rint(new_points).astype(int)
                    inside = ((xy[:, 0] >= 0) & (xy[:, 0] < mask.shape[1])
                              & (xy[:, 1] >= 0) & (xy[:, 1] < mask.shape[0]))
                    valid = np.zeros(len(xy), dtype=bool)
                    valid[inside] = mask[xy[inside, 1], xy[inside, 0]] > 0
                    old_points, new_points = old_points[valid], new_points[valid]
                    if len(old_points) >= 8:
                        matrix, inliers = cv2.estimateAffinePartial2D(
                            old_points, new_points, method=cv2.RANSAC,
                            ransacReprojThreshold=1.5,
                        )
                        if matrix is not None and inliers is not None:
                            inlier_ratio = float(np.mean(inliers))
                            if inlier_ratio < 0.55:
                                return False, "background_changed"
                            h, w = mask.shape
                            corners = np.array([[0, 0, 1], [w, 0, 1],
                                                [0, h, 1], [w, h, 1]], dtype=float)
                            moved = corners @ matrix.T
                            shift = np.linalg.norm(moved - corners[:, :2], axis=1)
                            # Approximately 3 native pixels at 960x540.
                            if float(np.max(shift)) > max(1.0, min(h, w) * 0.006):
                                return False, "camera_motion"
                            return True, "background_features_stable"
        # Quiet white walls must not permanently disable the game. This fallback
        # is less informative than geometry and never certifies moving cameras.
        differences = cv2.absdiff(old, new)[mask > 0]
        if (float(np.mean(differences)) > 3.0
                or float(np.percentile(differences, 95)) > 12.0):
            return False, "background_changed"
        return True, "low_texture_fallback"

    def update(self, frame: np.ndarray, poses: list[_Pose], now: float) -> tuple[bool, bool, str]:
        h, w = frame.shape[:2]
        scale = min(1.0, 320 / w)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (max(1, round(w * scale)), max(1, round(h * scale))))
        small = cv2.GaussianBlur(small, (5, 5), 0)
        mask = np.full(small.shape, 255, dtype=np.uint8)
        for pose in poses:
            x1, y1, x2, y2 = pose.bbox * scale
            padding = max(5, round((x2 - x1) * 0.15))
            cv2.rectangle(mask, (max(0, int(x1) - padding), max(0, int(y1) - padding)),
                          (min(mask.shape[1] - 1, int(x2) + padding),
                           min(mask.shape[0] - 1, int(y2) + padding)), 0, -1)

        reset = False
        discontinuity = (self.last_now is not None
                         and (now <= self.last_now or now - self.last_now > 1.0))
        if (self.previous is None or self.previous.shape != small.shape or discontinuity):
            self.previous, self.previous_mask = small, mask
            self.reference, self.reference_mask = small.copy(), mask.copy()
            self.quiet_since = now
            self.last_now = now
            return False, True, "warming_up" if not discontinuity else "frame_gap"

        overlap = cv2.bitwise_and(mask, self.previous_mask)
        quiet, reason = self._compare(self.previous, small, overlap)
        if quiet and self.reference is not None:
            overlap_ref = cv2.bitwise_and(mask, self.reference_mask)
            quiet, reason = self._compare(self.reference, small, overlap_ref)

        if not quiet:
            reset = True
            self.quiet_since = None
            self.reference, self.reference_mask = small.copy(), mask.copy()
        elif self.quiet_since is None:
            self.quiet_since = now
        if quiet and self.reference is None:
            self.reference, self.reference_mask = small.copy(), mask.copy()

        stable = quiet and self.quiet_since is not None and now - self.quiet_since >= self.settle_seconds
        self.previous, self.previous_mask = small, mask
        self.last_now = now
        return stable, reset, reason if stable or not quiet else "settling"


class VisionPipeline:
    """YOLOv8 pose + fixed lanes + a conservative motion observation gate.

    ``load`` may download the chosen official weights on first use. An offline
    installation should supply a local ``weights`` path. No heuristic detector
    silently replaces YOLO if loading fails.
    """

    SKELETON = ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11),
                (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16))

    def __init__(self, weights: str = "yolov8n-pose.pt", lane_count: int = 3,
                 device: str | None = None) -> None:
        if not 1 <= lane_count <= 4:
            raise ValueError("玩家赛道数必须在 1 到 4 之间")
        self.weights = weights
        self.lane_count = lane_count
        self.device = device
        self.model: Any = None
        self.keypoint_confidence = 0.5
        self._baselines: dict[int, _Baseline] = {}
        self._blocked: set[int] = set()
        self._guard = _CameraGuard()

    def load(self) -> None:
        if self.model is not None:
            return
        try:
            from ultralytics import YOLO
        except ImportError as error:
            raise RuntimeError("尚未安装 AI 视觉依赖，请先安装项目的 vision 可选依赖（ultralytics）。") from error
        try:
            model = YOLO(self.weights)
            if getattr(model, "task", "pose") != "pose":
                raise ValueError("需要姿态模型，例如 yolov8n-pose.pt，不能使用普通检测模型")
            self.model = model
        except Exception as error:
            raise RuntimeError(
                f"AI 姿态模型加载失败：{error}。首次使用需要下载权重；离线时请提供本地模型文件。"
            ) from error

    def reset_baseline(self) -> None:
        """Start a new judging interval; never carry a green-light pose into red."""
        self._baselines.clear()
        self._blocked.clear()
        self._guard.reset_reference()

    def _predict(self, frame: np.ndarray) -> list[_Pose]:
        if self.model is None:
            raise RuntimeError("AI 模型尚未加载，请先调用 VisionPipeline.load()")
        options: dict[str, Any] = {"verbose": False, "conf": 0.45, "imgsz": 640}
        if self.device is not None:
            options["device"] = self.device
        try:
            result = self.model.predict(frame, **options)[0]
        except Exception as error:
            raise RuntimeError(f"AI 姿态推理失败：{error}") from error
        boxes = getattr(result, "boxes", None)
        points = getattr(result, "keypoints", None)
        if boxes is None or points is None or len(boxes) == 0:
            return []
        xyxy = _numpy(boxes.xyxy)
        confidence = _numpy(boxes.conf)
        keypoints = _numpy(points.xy)
        scores = getattr(points, "conf", None)
        if scores is None:
            # A pose without visibility scores cannot be used as evidence.
            # Keep the person box for occlusion checks, but pass it through the
            # same finite/size/bounds checks as ordinary model output.
            keypoints = np.zeros((len(xyxy), 17, 2))
            scores = np.zeros((len(xyxy), 17))
        else:
            scores = _numpy(scores)
        h, w = frame.shape[:2]
        poses = []
        for box, conf, xy, score in zip(xyxy, confidence, keypoints, scores):
            if (not np.all(np.isfinite(box)) or not np.isfinite(conf) or not 0 <= conf <= 1
                    or np.asarray(xy).shape != (17, 2) or np.asarray(score).shape != (17,)):
                continue
            box = np.asarray(box, dtype=float).copy()
            box[[0, 2]] = np.clip(box[[0, 2]], 0, w - 1)
            box[[1, 3]] = np.clip(box[[1, 3]], 0, h - 1)
            if box[2] - box[0] < 12 or box[3] - box[1] < 40:
                continue
            xy = np.nan_to_num(np.asarray(xy, dtype=float), nan=0, posinf=0, neginf=0)
            score = np.asarray(score, dtype=float).copy()
            # Infinity must not become a very large "confident" score. Invalid
            # probabilities carry no usable evidence, even if XY looks valid.
            score[~np.isfinite(score) | (score < 0) | (score > 1)] = 0
            score[(xy[:, 0] <= 0) | (xy[:, 0] >= w) | (xy[:, 1] <= 0) | (xy[:, 1] >= h)] = 0
            poses.append(_Pose(box, xy, score, float(conf)))
        return poses

    def _usable(self, pose: _Pose) -> bool:
        visible = pose.scores >= self.keypoint_confidence
        # Ignore facial jitter; require a torso and enough body landmarks.
        return bool(np.count_nonzero(visible[5:]) >= 6
                    and np.count_nonzero(visible[[5, 6, 11, 12]]) >= 3)

    def _motion(self, pose: _Pose, baseline: _Baseline) -> tuple[float, bool]:
        old = baseline.pose
        common = ((pose.scores >= self.keypoint_confidence)
                  & (old.scores >= self.keypoint_confidence))
        common[:5] = False
        if np.count_nonzero(common) < 6 or np.count_nonzero(common[[5, 6, 11, 12]]) < 3:
            return 0.0, False
        displacement = np.linalg.norm(pose.points[common] - old.points[common], axis=1)
        # Top-three median sees an arm/leg move while resisting one bad landmark.
        pose_shift = float(np.median(np.sort(displacement)[-3:])) / baseline.height
        torso = np.array([5, 6, 11, 12])
        torso = torso[common[torso]]
        torso_shift = float(np.linalg.norm(np.median(pose.points[torso] - old.points[torso], axis=0))) / baseline.height
        box_old_center = (old.bbox[:2] + old.bbox[2:]) / 2
        box_center = (pose.bbox[:2] + pose.bbox[2:]) / 2
        box_shift = float(np.linalg.norm(box_center - box_old_center)) / baseline.height
        # A bounding-box change alone is not body movement. Require matching
        # torso displacement, so a waving hand changing box width cannot double it.
        translation = min(box_shift, torso_shift)
        # 0.6% of body height is a small deadband for detector noise.
        return max(0.0, max(pose_shift, translation) - 0.006), True

    def process(self, frame: np.ndarray, now: float) -> FrameAnalysis:
        if (not isinstance(frame, np.ndarray) or frame.ndim != 3
                or frame.shape[2] != 3 or frame.size == 0 or frame.dtype != np.uint8):
            raise ValueError("需要非空 uint8 BGR 摄像头图像")
        if not np.isfinite(now):
            raise ValueError("帧时间必须为有限的单调时钟值")
        height, width = frame.shape[:2]
        poses = self._predict(frame)
        stable, reset, stability_reason = self._guard.update(frame, poses, now)
        previously_tracked = set(self._baselines)
        if reset:
            self._baselines.clear()
            # Camera settling can replace pose references, but cannot resolve
            # player identity after an occlusion or lane crossing. Only the
            # explicit next judging interval may release those blocked lanes.
        lane_width = width / self.lane_count
        assigned: dict[int, list[_Pose]] = {i: [] for i in range(1, self.lane_count + 1)}
        crossing: set[int] = set()
        for pose in poses:
            center = float((pose.bbox[0] + pose.bbox[2]) / 2)
            lane = min(self.lane_count, max(1, int(center / lane_width) + 1))
            assigned[lane].append(pose)
            # Hands may protrude slightly; a substantial boundary overlap blocks
            # BOTH lanes so an entering person cannot become the adjacent player.
            margin = min(12.0, lane_width * 0.04)
            for boundary_id in range(1, self.lane_count):
                border = boundary_id * lane_width
                if pose.bbox[0] < border - margin and pose.bbox[2] > border + margin:
                    crossing.update((boundary_id, boundary_id + 1))

        observations: list[dict[str, Any]] = []
        created: list[int] = []
        for lane, candidates in assigned.items():
            obs: dict[str, Any] = {
                "player_id": lane, "visible": bool(candidates), "confidence": 0.0,
                "motion": 0.0, "ambiguous": True, "bbox": [], "reason": "not_visible",
            }
            if candidates:
                pose = max(candidates, key=lambda item: item.confidence)
                obs.update(confidence=round(pose.confidence, 4), bbox=pose.bbox.round(1).tolist())
            else:
                if lane in previously_tracked:
                    self._blocked.add(lane)
                observations.append(obs)
                continue
            reason = ""
            if lane in crossing:
                reason = "lane_crossing"
            elif len(candidates) != 1:
                reason = "multiple_people"
            elif pose.confidence < 0.5:
                reason = "low_detection_confidence"
            elif not self._usable(pose):
                reason = "pose_occluded"
            elif lane in self._blocked:
                reason = "identity_unconfirmed"
            if reason:
                if lane in previously_tracked:
                    self._blocked.add(lane)
                obs["reason"] = reason
            elif not stable:
                obs["reason"] = "camera_unstable"
            else:
                obs.update(ambiguous=False, reason="tracking")
                if lane not in self._baselines:
                    self._baselines[lane] = _Baseline(
                        _Pose(pose.bbox.copy(), pose.points.copy(), pose.scores.copy(), pose.confidence),
                        max(40.0, float(pose.bbox[3] - pose.bbox[1])),
                    )
                    created.append(lane)
                    obs["reason"] = "baseline_created"
                else:
                    motion, valid = self._motion(pose, self._baselines[lane])
                    obs["motion"] = round(motion, 5)
                    if not valid:
                        obs.update(ambiguous=True, reason="insufficient_shared_pose")
                        self._blocked.add(lane)
            observations.append(obs)

        annotated = frame.copy()
        for i in range(1, self.lane_count):
            x = round(i * lane_width)
            cv2.line(annotated, (x, 0), (x, height), (120, 160, 140), 1)
        for pose in poses:
            for a, b in self.SKELETON:
                if min(pose.scores[a], pose.scores[b]) >= self.keypoint_confidence:
                    cv2.line(annotated, tuple(pose.points[a].astype(int)),
                             tuple(pose.points[b].astype(int)), (70, 230, 190), 2)
            for point, score in zip(pose.points[5:], pose.scores[5:]):
                if score >= self.keypoint_confidence:
                    cv2.circle(annotated, tuple(point.astype(int)), 3, (90, 240, 210), -1)
        for obs in observations:
            lane = obs["player_id"]
            color = (65, 215, 160) if not obs["ambiguous"] else (30, 190, 245)
            if obs["bbox"]:
                x1, y1, x2, y2 = map(int, obs["bbox"])
                cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
                label = f"P{lane:02d}  {obs['motion']:.3f}"
                if obs["ambiguous"]:
                    label += "  CHECK"
                cv2.putText(annotated, label, (x1, max(22, y1 - 9)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
            cv2.putText(annotated, f"LANE {lane:02d}", (round((lane - 1) * lane_width) + 12, height - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (225, 235, 225), 1)
        cv2.putText(annotated, "YOLO POSE / " + ("STABLE" if stable else "WAIT - CAMERA SETTLING"),
                    (14, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (65, 215, 160) if stable else (30, 190, 245), 2)
        return FrameAnalysis(observations, stable, annotated, width, height,
                             created, reset, stability_reason)
