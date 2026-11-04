"""Deterministic pose/scene tests; do not download weights or access a camera."""

import types

import numpy as np
import pytest

from spark_freeze.vision import VisionPipeline, _CameraGuard, _Pose


class _Boxes:
    def __init__(self, boxes):
        self.xyxy = np.asarray(boxes, dtype=float).reshape(-1, 4)
        self.conf = np.full(len(boxes), 0.95)

    def __len__(self):
        return len(self.xyxy)


def _pose(center=160.0, offset=0.0):
    points = np.array([
        [center, 70], [center - 5, 65], [center + 5, 65], [center - 10, 70], [center + 10, 70],
        [center - 20, 105], [center + 20, 105], [center - 30, 145], [center + 30, 145],
        [center - 35, 180], [center + 35, 180], [center - 16, 190], [center + 16, 190],
        [center - 17, 240], [center + 17, 240], [center - 18, 290], [center + 18, 290],
    ], dtype=float)
    points[:, 0] += offset
    return [center - 50 + offset, 50, center + 50 + offset, 310], points


class _Model:
    def __init__(self):
        self.poses = [_pose()]
        self.scores = None
        self.confidence = 0.95

    def predict(self, frame, **kwargs):
        count = len(self.poses)
        scores = self.scores if self.scores is not None else np.full((count, 17), 0.95)
        boxes = _Boxes([p[0] for p in self.poses])
        boxes.conf[:] = self.confidence
        return [types.SimpleNamespace(
            boxes=boxes,
            keypoints=types.SimpleNamespace(xy=np.array([p[1] for p in self.poses]), conf=scores),
        )]


@pytest.fixture
def pipeline():
    pipeline = VisionPipeline(lane_count=2)
    pipeline.model = _Model()
    return pipeline


@pytest.fixture
def blank():
    return np.full((360, 640, 3), 120, dtype=np.uint8)


def _warm(pipeline, frame):
    pipeline.process(frame, 0.0)
    return pipeline.process(frame, 0.5)


def test_real_detector_is_not_silently_replaced(blank):
    with pytest.raises(RuntimeError, match="尚未加载"):
        VisionPipeline().process(blank, 0.0)


def test_still_person_is_zero_and_absent_lane_not_judged(pipeline, blank):
    result = _warm(pipeline, blank)
    assert result.stable
    assert result.baseline_created == [1]
    assert result.observations[0]["motion"] == 0
    assert not result.observations[0]["ambiguous"]
    assert result.observations[1]["visible"] is False
    assert result.observations[1]["ambiguous"] is True


def test_sustained_translation_against_fixed_baseline(pipeline, blank):
    _warm(pipeline, blank)
    pipeline.model.poses = [_pose(offset=25)]
    first = pipeline.process(blank, 0.6).observations[0]
    second = pipeline.process(blank, 0.7).observations[0]
    assert first["motion"] > 0.08
    assert first["motion"] == second["motion"]
    assert not first["ambiguous"]


def test_one_bad_joint_and_bbox_jitter_do_not_accuse(pipeline, blank):
    _warm(pipeline, blank)
    box, points = _pose()
    points[9, 0] += 80
    box[2] += 80
    pipeline.model.poses = [(box, points)]
    result = pipeline.process(blank, 0.6).observations[0]
    assert result["motion"] == 0


def test_two_arm_points_are_detected(pipeline, blank):
    _warm(pipeline, blank)
    box, points = _pose()
    points[[7, 9], 0] += 30
    pipeline.model.poses = [(box, points)]
    assert pipeline.process(blank, 0.6).observations[0]["motion"] > 0.10


def test_low_confidence_keypoints_do_not_trigger_penalty(pipeline, blank):
    _warm(pipeline, blank)
    box, points = _pose()
    points[7:11, 0] += 100
    scores = np.full((1, 17), 0.95)
    scores[0, 7:11] = 0.1
    pipeline.model.poses, pipeline.model.scores = [(box, points)], scores
    result = pipeline.process(blank, 0.6).observations[0]
    assert result["motion"] == 0


def test_occlusion_locks_lane_until_new_baseline(pipeline, blank):
    _warm(pipeline, blank)
    pipeline.model.poses = []
    assert not pipeline.process(blank, 0.6).observations[0]["visible"]
    pipeline.model.poses = [_pose(offset=35)]
    result = pipeline.process(blank, 0.7).observations[0]
    assert result["ambiguous"]
    assert result["motion"] == 0
    pipeline.reset_baseline()
    result = pipeline.process(blank, 0.8)
    assert not result.observations[0]["ambiguous"]
    assert result.baseline_created == [1]


def test_two_people_in_one_lane_is_ambiguous(pipeline, blank):
    _warm(pipeline, blank)
    pipeline.model.poses = [_pose(), _pose(center=240)]
    result = pipeline.process(blank, 0.6).observations[0]
    assert result["ambiguous"] and result["reason"] == "multiple_people"


def test_crossing_blocks_both_lanes(pipeline, blank):
    pipeline.model.poses = [_pose(center=300), _pose(center=470)]
    result = _warm(pipeline, blank)
    assert all(o["ambiguous"] for o in result.observations)
    assert result.observations[1]["reason"] == "lane_crossing"


def test_scene_change_resets_baseline(pipeline, blank):
    _warm(pipeline, blank)
    result = pipeline.process(np.full_like(blank, 190), 0.6)
    assert not result.stable and result.baseline_reset
    assert result.observations[0]["motion"] == 0
    pipeline.process(np.full_like(blank, 190), 0.7)
    result = pipeline.process(np.full_like(blank, 190), 1.1)
    assert result.stable and result.baseline_created == [1]


def test_textured_camera_shift_is_not_body_motion():
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, (360, 640, 3), dtype=np.uint8)
    guard = _CameraGuard()
    guard.update(frame, [], 0.0)
    assert guard.update(frame, [], 0.5)[0]
    shifted = np.roll(frame, 15, axis=1)
    stable, reset, reason = guard.update(shifted, [], 0.6)
    assert not stable and reset
    assert reason in {"camera_motion", "background_changed"}


def test_frame_gap_and_invalid_frames_are_not_judged(pipeline, blank):
    _warm(pipeline, blank)
    result = pipeline.process(blank, 2.0)
    assert not result.stable and result.baseline_reset
    with pytest.raises(ValueError):
        pipeline.process(np.zeros((0, 0, 3), dtype=np.uint8), 2.1)
    with pytest.raises(ValueError):
        pipeline.process(blank, float("nan"))


def test_empty_detection_frame_and_small_person(blank):
    pipeline = VisionPipeline(lane_count=1)
    pipeline.model = _Model()
    pipeline.model.poses = []
    assert _warm(pipeline, blank).observations[0]["visible"] is False
    box, points = _pose()
    pipeline.model.poses = [([1, 1, 8, 20], points)]
    assert pipeline.process(blank, 0.6).observations[0]["visible"] is False


def test_lane_count_validation():
    with pytest.raises(ValueError):
        VisionPipeline(lane_count=0)
    with pytest.raises(ValueError):
        VisionPipeline(lane_count=5)


def test_low_detection_confidence_cannot_seed_a_penalty_baseline(pipeline, blank):
    pipeline.model.confidence = 0.46
    result = _warm(pipeline, blank)
    assert result.observations[0]["ambiguous"]
    assert result.baseline_created == []
    # A later, reliable detection must establish its own reference, not be
    # compared against the uncertain earlier detection's coordinates.
    pipeline.model.confidence = 0.95
    pipeline.model.poses = [_pose(offset=25)]
    result = pipeline.process(blank, 0.6)
    assert result.baseline_created == [1]
    assert result.observations[0]["motion"] == 0


def test_invalid_keypoint_scores_do_not_create_evidence(pipeline, blank):
    _warm(pipeline, blank)
    box, points = _pose()
    points[[7, 9], 0] += 50
    scores = np.full((1, 17), 0.95)
    scores[0, [7, 9]] = [np.inf, 2.0]
    pipeline.model.poses, pipeline.model.scores = [(box, points)], scores
    result = pipeline.process(blank, 0.6).observations[0]
    assert result["motion"] == 0


def test_missing_pose_confidence_still_validates_boxes(blank):
    pipeline = VisionPipeline(lane_count=1)
    _, points = _pose()
    pipeline.model = types.SimpleNamespace(predict=lambda *args, **kwargs: [types.SimpleNamespace(
        boxes=_Boxes([[np.nan, 50, 210, 310], [-100, -100, 1000, 1000]]),
        keypoints=types.SimpleNamespace(xy=np.array([points, points]), conf=None),
    )])
    result = _warm(pipeline, blank)
    assert result.observations[0]["ambiguous"]
    assert result.observations[0]["bbox"] == [0.0, 0.0, 639.0, 359.0]


def test_camera_change_does_not_unlock_a_previously_occluded_lane(pipeline, blank):
    _warm(pipeline, blank)
    pipeline.model.poses = []
    pipeline.process(blank, 0.6)
    pipeline.model.poses = [_pose(offset=35)]
    changed = np.full_like(blank, 190)
    pipeline.process(changed, 0.7)
    pipeline.process(changed, 0.8)
    result = pipeline.process(changed, 1.2)
    assert result.stable
    assert result.observations[0]["reason"] == "identity_unconfirmed"
    assert result.observations[0]["ambiguous"]
    pipeline.reset_baseline()
    assert not pipeline.process(changed, 1.3).observations[0]["ambiguous"]
