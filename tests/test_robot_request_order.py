import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.runtime import Runtime


@pytest.fixture
def robot_api(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_CONTROL_TOKEN', 'control-token-for-testing')
    monkeypatch.setenv('SPARK_BRIDGE_TOKEN', 'bridge-token-for-testing')
    runtime = Runtime(tmp_path)
    runtime.engine.mode = 'ros'
    runtime.robot_time = runtime.operator_time = runtime.frame_time = time.monotonic()
    runtime.odom_age = runtime.scan_age = 0
    runtime.clearance = 1
    runtime.engine.camera.update(ready=True, error=None)
    # No lifespan or physical devices. Readiness is refreshed for each operation.
    return runtime, TestClient(create_app(runtime))


CONTROL = {'X-Control-Token': 'control-token-for-testing'}
BRIDGE = {'X-Bridge-Token': 'bridge-token-for-testing'}


@pytest.mark.parametrize('initially_armed', [False, True])
def test_disarm_invalidates_an_earlier_enable_request(robot_api, initially_armed):
    runtime, client = robot_api
    engine = runtime.engine
    engine.robot['armed'] = initially_armed
    old_request = dict(armed=True, expected_session_id=engine.session_id,
                       expected_revision=engine.revision)
    assert client.post('/api/robot/arm', headers=CONTROL, json={'armed': False}).status_code == 200
    response = client.post('/api/robot/arm', headers=CONTROL, json=old_request)
    assert response.status_code == 409
    assert not engine.robot['armed']
    # A deliberate new request with the latest state is still allowed.
    current_request = {**old_request, 'expected_revision': engine.revision}
    assert client.post('/api/robot/arm', headers=CONTROL, json=current_request).status_code == 200
    assert engine.robot['armed']


@pytest.mark.parametrize('missing', ['expected_session_id', 'expected_revision'])
def test_arm_requires_current_session_and_revision(robot_api, missing):
    runtime, client = robot_api
    request = dict(armed=True, expected_session_id=runtime.engine.session_id,
                   expected_revision=runtime.engine.revision)
    del request[missing]
    assert client.post('/api/robot/arm', headers=CONTROL, json=request).status_code == 409
    assert not runtime.engine.robot['armed']


def test_camera_error_cannot_be_armed_with_recent_old_frame(robot_api):
    runtime, client = robot_api
    runtime._capture_error('inference failed', runtime.generation)
    engine = runtime.engine
    response = client.post('/api/robot/arm', headers=CONTROL, json=dict(
        armed=True, expected_session_id=engine.session_id, expected_revision=engine.revision))
    assert response.status_code == 409
    assert not engine.robot['armed']


def test_late_frame_decode_cannot_overwrite_newer_upload(robot_api, monkeypatch):
    runtime, client = robot_api
    entered, release = threading.Event(), threading.Event()

    def decode(body):
        if body == b'old':
            entered.set()
            assert release.wait(3)
        return np.full((8, 8, 3), 10 if body == b'old' else 20, dtype=np.uint8)

    monkeypatch.setattr(importlib.import_module('spark_freeze.app'), 'decode_robot_frame', decode)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(client.post, '/api/robot/frame', headers=BRIDGE, content=b'old')
        try:
            assert entered.wait(2)
            assert client.post('/api/robot/frame', headers=BRIDGE, content=b'new').status_code == 200
            newest_time = runtime.ros_frame_time
        finally:
            release.set()
        assert old.result(timeout=3).status_code == 409
    assert runtime.ros_frame_seq == 1
    assert runtime.ros_frame_time == newest_time
    assert np.all(runtime.ros_frame == 20)
