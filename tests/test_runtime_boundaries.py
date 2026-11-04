import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.engine import GameConfig, GameEngine
from spark_freeze.runtime import Runtime


def ready_red(runtime, players=1):
    runtime.engine = GameEngine(GameConfig(players=players, auto_eliminate=True))
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.transition('red', 10)
    runtime.engine.last_observed = time.monotonic() - .1
    runtime.engine.streaks[1] = time.monotonic() - 1


def test_last_players_failed_photo_does_not_end_game_as_eliminated(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    ready_red(runtime)
    monkeypatch.setattr(Path, 'write_bytes', lambda *_: (_ for _ in ()).throw(OSError('full')))
    runtime._publish(np.zeros((100, 100, 3), np.uint8),
        [dict(player_id=1, visible=True, confidence=1, motion=.2)], True, 0, {})
    assert runtime.engine.phase == 'paused'
    assert runtime.engine.evidence[0]['status'] == 'pending'
    assert runtime.engine.storage['error']
    assert not any('均已淘汰' in e['text'] for e in runtime.engine.events)
    # JSON saving succeeds but must not erase the failed-photo warning.
    assert runtime.save_report()['storage']['error']


def test_missing_baseline_photo_never_auto_eliminates(tmp_path):
    runtime = Runtime(tmp_path)
    ready_red(runtime)
    runtime._publish(np.zeros((100, 100, 3), np.uint8),
        [dict(player_id=1, visible=True, confidence=1, motion=.2)], True, 0, {})
    evidence = runtime.engine.evidence[0]
    assert evidence['status'] == 'pending'
    assert evidence['before_url'] is None
    assert evidence['after_url'] is not None
    assert evidence['storage_error']
    assert runtime.engine.phase == 'paused'
    assert runtime.engine.players[0]['status'] == 'pending'


def test_stale_result_from_old_epoch_cannot_break_new_camera(tmp_path):
    runtime = Runtime(tmp_path)
    old_epoch = runtime.engine.baseline_epoch
    runtime.engine.transition('red', 10)
    runtime.engine.camera['ready'] = True
    epoch = runtime.engine.baseline_epoch
    runtime._publish(np.zeros((100, 100, 3), np.uint8), [], True, 0, {},
        expected_epoch=old_epoch, captured_at=time.monotonic()-2)
    assert runtime.engine.camera['ready']
    assert runtime.engine.baseline_epoch == epoch


def test_buffered_frame_from_before_red_is_not_a_new_baseline(tmp_path):
    runtime = Runtime(tmp_path)
    captured = time.monotonic() - .01
    runtime.engine.transition('red', 10)
    before_frames = {}
    runtime._publish(np.zeros((100, 100, 3), np.uint8),
        [dict(player_id=1, visible=True, confidence=1, motion=0)], True, 0,
        before_frames, baseline_created=[1], expected_epoch=runtime.engine.baseline_epoch,
        captured_at=captured)
    assert before_frames == {}
    assert runtime.frame_time == 0


@pytest.mark.parametrize('phase,action', [('lobby', 'start'), ('paused', 'resume')])
def test_stale_camera_cannot_start_or_resume(tmp_path, phase, action):
    runtime = Runtime(tmp_path)
    runtime.engine.mode = 'webcam'
    runtime.engine.phase = phase
    runtime.engine.camera['ready'] = True
    runtime.engine.players[0]['visible'] = True
    runtime.frame_time = time.monotonic() - 5
    # No lifespan: deliberately no clock or real camera worker.
    client = TestClient(create_app(runtime))
    response = client.post('/api/control', json={'action': action,
        'expected_session_id': runtime.engine.session_id, 'expected_revision': runtime.engine.revision})
    assert response.status_code == 409
    assert not runtime.engine.camera['ready']


def test_brief_capture_failure_pauses_without_waiting_for_clock(tmp_path):
    runtime = Runtime(tmp_path)
    ready_red(runtime)
    runtime.engine.mode = 'webcam'
    runtime._capture_error('read failed', 0)
    assert runtime.engine.phase == 'paused'


@pytest.mark.parametrize('mode', ['webcam', 'ros'])
def test_real_capture_routes_feed_pipeline_with_source_time(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    import spark_freeze.capture as capture_module
    import spark_freeze.vision as vision_module
    frames_seen = []
    stopped = []

    class Camera:
        recovery_epoch = 0
        def __init__(self, *_):
            pass
        def start(self):
            pass
        def read_latest(self, seq):
            return seq+1, np.zeros((100, 100, 3), np.uint8), time.monotonic(), None
        def stop(self):
            stopped.append(True)

    class Pipeline:
        def __init__(self, **_):
            pass
        def load(self):
            pass
        def reset_baseline(self):
            pass
        def process(self, frame, now):
            frames_seen.append(now)
            return SimpleNamespace(annotated=frame, observations=[], stable=True)

    monkeypatch.setattr(capture_module, 'WebcamCapture', Camera)
    monkeypatch.setattr(vision_module, 'VisionPipeline', Pipeline)
    runtime = Runtime(tmp_path)
    runtime.engine.mode = mode
    source_time = time.monotonic()
    runtime.ros_frame = np.zeros((100, 100, 3), np.uint8)
    runtime.ros_frame_time = source_time
    runtime.ros_frame_seq = 1
    runtime.start()
    try:
        deadline = time.monotonic() + 2
        while not runtime.engine.camera['ready'] and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime.engine.camera['ready'], runtime.engine.camera
        assert runtime.engine.camera['error'] is None
        assert frames_seen
        if mode == 'ros':
            assert frames_seen == [source_time]
    finally:
        runtime.stop()
    assert bool(stopped) == (mode == 'webcam')
