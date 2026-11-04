import time
import threading

import numpy as np
from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.runtime import Runtime


def test_estop_requires_explicit_current_acknowledgement(tmp_path):
    runtime = Runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        old = client.get('/api/state').json()
        stopped = client.post('/api/control', json={'action': 'estop'}).json()
        assert client.post('/api/control', json={'action': 'reset'}).status_code == 409
        assert client.post('/api/control', json={'action': 'clear_estop',
            'expected_session_id': old['session_id'], 'expected_revision': old['revision']}).status_code == 409
        assert client.post('/api/control', json={'action': 'clear_estop',
            'expected_session_id': stopped['session_id'], 'expected_revision': stopped['revision']}).status_code == 200
        assert client.post('/api/config', json={'expected_session_id': old['session_id'], 'players': 1}).status_code == 409


def test_invalid_input_returns_client_errors(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_BRIDGE_TOKEN', 'valid-bridge-token-123')
    runtime = Runtime(tmp_path)
    # No lifespan: no physical camera or model loading.
    with TestClient(create_app(runtime)) as client:
        assert client.post('/api/config', json={'mode': []}).status_code == 422
        assert client.post('/api/control', json={'action': 'demo_move', 'player_id': 0}).status_code == 422
        with runtime.lock:
            runtime.engine.mode = 'ros'
        assert client.post('/api/robot/frame', content=b'', headers={
            'X-Bridge-Token': 'valid-bridge-token-123'}).status_code == 422


def test_disk_error_keeps_clock_and_controls_alive(tmp_path):
    blocked = tmp_path / 'file-not-directory'
    blocked.write_text('occupied')
    runtime = Runtime(blocked)
    with TestClient(create_app(runtime)) as client:
        assert client.post('/api/control', json={'action': 'estop'}).status_code == 200
        runtime.save_report()  # Wait for the asynchronous disk failure notification.
        assert client.get('/api/state').json()['storage']['error']
        assert client.get('/api/report').status_code == 200
        assert all(t.is_alive() for t in runtime.threads)


def test_failed_evidence_never_auto_eliminates(tmp_path):
    blocked = tmp_path / 'file-not-directory'
    blocked.write_text('occupied')
    runtime = Runtime(blocked)
    runtime.engine.config.auto_eliminate = True
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.transition('red', 10)
    runtime.engine.streaks[1] = time.monotonic() - 1
    runtime.engine.last_observed = time.monotonic() - .1
    runtime._publish(np.zeros((100, 100, 3), np.uint8),
        [dict(player_id=1, visible=True, confidence=1, motion=.2)], True, 0, {})
    evidence = runtime.engine.evidence[0]
    assert evidence['before_url'] is None and evidence['after_url'] is None
    assert evidence['status'] == 'pending'
    assert runtime.engine.players[0]['status'] == 'pending'
    assert runtime.engine.phase == 'paused'


def test_stale_inference_does_not_refresh_camera(tmp_path):
    runtime = Runtime(tmp_path)
    runtime._publish(np.zeros((100, 100, 3), np.uint8), [], True, 0, {},
                     captured_at=time.monotonic()-2)
    assert not runtime.engine.camera['ready']
    assert runtime.frame_time == 0


def test_sensor_age_includes_elapsed_time_since_tick(tmp_path):
    runtime = Runtime(tmp_path)
    now = time.monotonic()
    runtime.robot_time = now - .3
    runtime.odom_age = .3
    assert '里程计' in runtime.robot_unready(now)


def test_capture_error_clears_streak_and_visibility(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.engine.camera['ready'] = True
    runtime.engine.streaks[1] = time.monotonic()
    runtime.engine.players[0]['visible'] = True
    epoch = runtime.engine.baseline_epoch
    runtime._capture_error('lost frame', 0)
    assert not runtime.engine.streaks
    assert not runtime.engine.players[0]['visible']
    assert runtime.engine.baseline_epoch > epoch
