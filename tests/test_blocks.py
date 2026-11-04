import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from spark_freeze.block_app import create_block_app
from spark_freeze.block_vision import detect_blue
from spark_freeze.blocks import BlockEngine, BlockError, Cell, Layout, Telemetry, layout_from_text


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def ready(engine, **overrides):
    engine.operator_time = engine.clock()
    data = dict(bridge_id='bridge-0001', ready=True, dry_run=False, stopped=True,
                pose=[0, 0, 0], camera_age=.05, scan_age=.05, odom_age=.05, clearance=1,
                slots=['1-1-0', '1-1-1', '1-1-2'], sources=3)
    data.update(overrides)
    return engine.telemetry(Telemetry(**data))


def live(tmp_path):
    clock = Clock()
    e = BlockEngine(tmp_path, clock)
    e.control('mode', e.revision, 'robot')
    ready(e)
    e.control('record_home', e.revision)
    e.control('start', e.revision)
    return e, clock


def finish_command(e, ok=True):
    command = e.command['id']
    e.result(command, e.owner, ok, 'sequence complete')
    return command


def test_tower_plan_is_bottom_up_and_demo_is_explicit(tmp_path):
    clock = Clock()
    e = BlockEngine(tmp_path, clock)
    e.save_layout(Layout(cells=[Cell(row=1, col=1, layer=i) for i in [2, 0, 1]]))
    e.control('start', e.revision)
    assert [p['cell']['layer'] for p in e.plan] == [0, 1, 2]
    for _ in range(18):
        clock.now += .8
        e.tick()
    assert e.phase == 'completed'
    assert len(e.placed) == 3
    assert all(p['verification'] == 'simulated' for p in e.placed)
    assert e.command is None and not e.armed


@pytest.mark.parametrize('cells', [
    [dict(row=1, col=1, layer=1)],
    [dict(row=1, col=1), dict(row=1, col=1)],
    [dict(row=3, col=0)],
    [dict(row=True, col=0)],
])
def test_invalid_layouts_are_rejected(cells):
    with pytest.raises(ValidationError):
        Layout(cells=cells)


def test_live_actions_require_manual_evidence_confirmation(tmp_path):
    e, _ = live(tmp_path)
    for index in range(3):
        assert e.phase == 'navigate_pick'
        finish_command(e)
        finish_command(e)
        last = finish_command(e)
        assert e.phase == 'awaiting_pick' and e.command is None
        assert len(e.placed) == index
        with pytest.raises(BlockError):
            e.result(last, e.owner, True, 'duplicate')
        with pytest.raises(BlockError):
            e.control('confirm_place', e.revision)
        e.control('confirm_pick', e.revision)
        assert e.phase == 'navigate_home'
        finish_command(e)
        finish_command(e)
        finish_command(e)
        assert e.phase == 'awaiting_place' and len(e.placed) == index
        e.control('confirm_place', e.revision)
    assert e.phase == 'completed' and len(e.placed) == 3
    assert len(e.checks) == 6
    assert all(c['verification'] == 'operator_confirmed' for c in e.checks)
    # A reset after completion also waits for the robot to process disarming.
    with pytest.raises(BlockError, match='本次停止'):
        e.control('reset', e.revision)
    ready(e, stop_ack=e.stop_id)
    e.control('reset', e.revision)
    assert e.phase == 'idle' and e.stop_id is None


@pytest.mark.parametrize('fault', ['camera_age', 'scan_age', 'odom_age', 'clearance', 'bridge_id'])
def test_sensor_or_bridge_changes_latch_stop(tmp_path, fault):
    e, _ = live(tmp_path)
    update = {fault: .8 if fault.endswith('age') else .2 if fault == 'clearance' else 'new-bridge-0002'}
    ready(e, **update)
    assert e.phase == 'estop' and not e.armed and e.command is None
    ready(e)
    assert e.phase == 'estop'
    with pytest.raises(BlockError):
        e.control('start', e.revision)


def test_lease_timeout_and_manual_recovery_never_resume_old_command(tmp_path):
    e, clock = live(tmp_path)
    old_id = e.command['id']
    old_revision = e.revision
    clock.now += .9
    e.tick()
    assert e.phase == 'estop'
    with pytest.raises(BlockError):
        e.control('recover', old_revision)
    with pytest.raises(BlockError):
        e.control('reset', e.revision)
    ready(e, stopped=False)
    with pytest.raises(BlockError):
        e.control('recover', e.revision)
    ready(e, stop_ack=e.stop_id)
    e.control('recover', e.revision)
    assert e.phase == 'idle' and not e.command
    with pytest.raises(BlockError):
        e.result(old_id, e.owner, True, 'late pickup')
    e.control('start', e.revision)
    assert e.command['id'] != old_id


def test_operator_loss_stops_even_when_robot_ticks(tmp_path):
    e, clock = live(tmp_path)
    clock.now += 2.1
    e.bridge_time = clock.now
    e.tick()
    assert e.phase == 'estop'
    assert '操作台' in e.events[-1]['message']


@pytest.mark.parametrize('gap', [.3, 2.1])
def test_operator_heartbeat_renews_only_unexpired_operator_lease(tmp_path, monkeypatch, gap):
    monkeypatch.setenv('SPARK_CONTROL_TOKEN', 'test-controller-token')
    app = create_block_app(tmp_path)
    e = app.state.engine
    clock = Clock()
    e.clock = clock
    e.control('mode', e.revision, 'robot')
    ready(e)
    e.control('record_home', e.revision)
    e.control('start', e.revision)
    clock.now += gap
    # Isolate the operator gap: robot observations are still current.
    e.bridge_time = clock.now
    # No lifespan watchdog: reproduce the heartbeat arriving before its next tick.
    client = TestClient(app)
    try:
        response = client.post('/api/blocks/operator/heartbeat', json={},
                               headers={'X-Control-Token': 'test-controller-token'})
        assert response.status_code == 200
        if gap > 2:
            assert e.phase == 'estop' and not e.armed and e.command is None
            assert '操作台' in e.events[-1]['message']
        else:
            assert e.phase == 'navigate_pick' and e.armed and e.command
        assert e.operator_time == clock.now
    finally:
        client.close()


@pytest.mark.parametrize('overrides', [dict(dry_run=True), dict(sources=2), dict(slots=['1-1-0']), dict(ready=False)])
def test_live_start_requires_complete_calibration(tmp_path, overrides):
    e = BlockEngine(tmp_path)
    e.control('mode', e.revision, 'robot')
    ready(e, **overrides)
    e.control('record_home', e.revision)
    with pytest.raises(BlockError):
        e.control('start', e.revision)
    assert not e.armed


def test_restart_preserves_layout_but_never_resumes_motion(tmp_path):
    e = BlockEngine(tmp_path)
    e.save_layout(layout_from_text('搭两层塔'))
    e.control('start', e.revision)
    restarted = BlockEngine(tmp_path)
    assert len(restarted.layout.cells) == 2
    assert restarted.phase == 'idle' and restarted.mode == 'demo' and not restarted.armed
    assert json.loads((tmp_path/'last-run.json').read_text(encoding='utf-8'))['phase'] == 'navigate_pick'


@pytest.mark.parametrize('text', ['搭四层塔', '不要搭三层塔', '搭红色积木', '随便弄一个', '六块排一行'])
def test_language_does_not_guess_unsupported_requests(text):
    with pytest.raises(BlockError):
        layout_from_text(text)


def test_blue_vision_does_not_claim_block_count():
    frame = np.zeros((480, 640, 3), np.uint8)
    cv2.rectangle(frame, (200, 100), (300, 350), (255, 0, 0), -1)
    cv2.rectangle(frame, (400, 100), (500, 200), (0, 0, 255), -1)
    _, boxes = detect_blue(frame)
    assert len(boxes) == 1
    assert boxes[0]['height'] > 200


def test_record_failure_cancels_command_before_bridge_can_fetch_it(tmp_path, monkeypatch):
    e, _ = live(tmp_path)
    def fail_write(*args):
        raise BlockError('disk full')
    monkeypatch.setattr(e, 'write', fail_write)
    with pytest.raises(BlockError):
        finish_command(e)
    assert e.phase == 'estop' and not e.armed and e.command is None
    assert ready(e)['command'] is None


def test_pause_cannot_downgrade_emergency_stop(tmp_path):
    e = BlockEngine(tmp_path)
    e.control('estop', 0)
    e.control('pause', 0)
    assert e.phase == 'estop'


def test_blocks_api_auth_proposal_and_estop(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_CONTROL_TOKEN', 'test-controller-token')
    monkeypatch.setenv('SPARK_AI_DISABLED', '1')
    app = create_block_app(tmp_path)
    with TestClient(app) as c:
        assert c.get('/api/health').json()['project'] == 'spark-blocks'
        assert c.post('/api/blocks/control', json=dict(action='estop', revision=0)).status_code == 401
        c.headers['X-Control-Token'] = 'test-controller-token'
        before = c.get('/api/blocks/state').json()
        proposal = c.post('/api/blocks/propose', json={'text': '搭两层塔'}).json()
        assert len(proposal['layout']['cells']) == 2 and not proposal['executed']
        assert c.get('/api/blocks/state').json()['revision'] == before['revision']
        assert c.post('/api/blocks/control', json=dict(action='start', session=before['session'], revision=before['revision'])).status_code == 200
        assert c.put('/api/blocks/layout', json=dict(session=before['session'], revision=1, layout=proposal['layout'])).status_code == 409
        stopped = c.post('/api/blocks/control', json=dict(action='estop', revision=0)).json()
        assert stopped['phase'] == 'estop'
        reply = c.post('/api/blocks/assistant', json=dict(text='开始搭建', use_cloud=True)).json()
        assert reply['source'] == 'local_facts'
        assert c.get('/api/blocks/state').json()['phase'] == 'estop'


def test_robot_evidence_needs_new_frame_and_current_command(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_BRIDGE_TOKEN', 'test-bridge-token-001')
    app = create_block_app(tmp_path)
    e = app.state.engine
    e.control('mode', e.revision, 'robot')
    ready(e)
    e.control('record_home', e.revision)
    e.control('start', e.revision)
    finish_command(e)
    finish_command(e)
    command = e.command['id']
    payload = dict(command_id=command, bridge_id=e.owner, ok=True, observation='fresh photo')
    with TestClient(app) as c:
        c.headers.update({'X-Bridge-Token': 'test-bridge-token-001', 'X-Bridge-Id': e.owner,
                          'X-Task-Session': e.session, 'X-Frame-Sequence': '1'})
        assert c.post('/api/blocks/robot/result', json=payload).status_code == 409
        jpeg = cv2.imencode('.jpg', np.zeros((100, 100, 3), np.uint8))[1].tobytes()
        assert c.post('/api/blocks/robot/frame', content=jpeg).status_code == 200
        # Preview frames cannot substitute for the specific inspection upload.
        assert c.post('/api/blocks/robot/result', json=payload).status_code == 409
        inspection = c.post('/api/blocks/robot/frame', content=jpeg, headers={'X-Command-Id': command}).json()
        payload['frame_id'] = inspection['frame_id']
        other = cv2.imencode('.jpg', np.full((100, 100, 3), 255, np.uint8))[1].tobytes()
        assert c.post('/api/blocks/robot/frame', content=other, headers={'X-Frame-Sequence': '2'}).status_code == 200
        # A later preview upload cannot change the photo paired with this result.
        assert c.post('/api/blocks/robot/result', json=payload).status_code == 200
        image = c.get(f'/api/blocks/evidence/{command}.jpg')
        assert image.status_code == 200
        assert cv2.imdecode(np.frombuffer(image.content, np.uint8), cv2.IMREAD_COLOR).mean() < 10
        assert e.evidence['frame_id'] == payload['frame_id']
        assert c.post('/api/blocks/robot/frame', content=jpeg).status_code == 409
        assert c.post('/api/blocks/robot/result', json=payload).status_code == 409
        assert c.post('/api/blocks/robot/frame', content=b'not jpeg').status_code == 422
        c.headers['X-Bridge-Id'] = 'another-bridge'
        assert c.post('/api/blocks/robot/frame', content=jpeg).status_code == 409


def test_camera_cannot_cross_task_sessions_or_reorder_source_frames(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_BRIDGE_TOKEN', 'test-bridge-token-001')
    app = create_block_app(tmp_path)
    e = app.state.engine
    ready(e)
    jpeg = cv2.imencode('.jpg', np.zeros((100, 100, 3), np.uint8))[1].tobytes()
    with TestClient(app) as c:
        c.headers.update({'X-Bridge-Token': 'test-bridge-token-001', 'X-Bridge-Id': e.bridge.bridge_id,
                          'X-Task-Session': e.session, 'X-Frame-Sequence': '5'})
        assert c.post('/api/blocks/robot/frame', content=jpeg).status_code == 200
        assert c.get('/api/blocks/state').json()['camera']['fresh']
        assert c.post('/api/blocks/robot/frame', content=jpeg, headers={'X-Frame-Sequence': '4'}).status_code == 409
        assert c.post('/api/blocks/robot/frame', content=jpeg, headers={'X-Command-Id': 'a'*32}).status_code == 409
        e.control('reset', e.revision)
        assert not c.get('/api/blocks/state').json()['camera']['fresh']
        assert c.get('/api/blocks/frame.jpg').status_code == 404
        assert c.post('/api/blocks/robot/frame', content=jpeg).status_code == 409
        c.headers['X-Task-Session'] = e.session
        assert c.post('/api/blocks/robot/frame', content=jpeg, headers={'X-Frame-Sequence': '6'}).status_code == 200


def test_stale_stopped_telemetry_does_not_acknowledge_new_stop(tmp_path):
    e, _ = live(tmp_path)
    e.control('estop', e.revision)
    first_stop = e.stop_id
    with pytest.raises(BlockError, match='本次停止'):
        e.control('recover', e.revision)
    ready(e, stop_ack=first_stop)
    e.control('estop', e.revision)
    assert e.stop_id != first_stop
    ready(e, stop_ack=first_stop)
    with pytest.raises(BlockError, match='本次停止'):
        e.control('recover', e.revision)
    ready(e, stop_ack=e.stop_id)
    e.control('recover', e.revision)
    assert e.phase == 'idle' and e.stop_id is None and not e.armed


def test_bridge_replacement_invalidates_home_even_while_idle(tmp_path):
    e = BlockEngine(tmp_path)
    e.control('mode', e.revision, 'robot')
    ready(e)
    e.control('record_home', e.revision)
    ready(e, bridge_id='replacement-bridge')
    assert e.home is None
    with pytest.raises(BlockError, match='起始位置'):
        e.control('start', e.revision)


def test_late_heartbeat_cannot_hide_a_sensor_gap(tmp_path):
    e, clock = live(tmp_path)
    clock.now += .6
    ready(e)
    assert e.phase == 'estop' and e.command is None


def test_operator_must_wait_for_stopped_observation_before_confirmation(tmp_path):
    e, _ = live(tmp_path)
    for _ in range(3):
        finish_command(e)
    ready(e, stopped=False)
    with pytest.raises(BlockError, match='停止'):
        e.control('confirm_pick', e.revision)
    assert e.phase == 'awaiting_pick'


@pytest.mark.parametrize('text', ['搭12层塔', '10块排一行', '搭0层塔', '搭十二层塔', '两块积木搭三层塔'])
def test_language_counts_are_not_truncated_or_silently_defaulted(text):
    with pytest.raises(BlockError):
        layout_from_text(text)


def test_one_corner_means_three_blocks_not_one_block():
    result = layout_from_text('搭一个拐角')
    assert len(result.cells) == 3


def test_old_browser_session_cannot_mutate_restarted_service(tmp_path):
    original = create_block_app(tmp_path)
    old_session = original.state.engine.session
    restarted = create_block_app(tmp_path)
    with TestClient(restarted) as c:
        state = c.get('/api/blocks/state').json()
        assert state['revision'] == original.state.engine.revision == 0
        for action in ('start', 'mode', 'reset'):
            assert c.post('/api/blocks/control', json=dict(action=action, revision=0, session=old_session, mode='robot')).status_code == 409
        assert c.put('/api/blocks/layout', json=dict(session=old_session, revision=0, layout=state['layout'])).status_code == 409
        assert c.get('/api/blocks/state').json()['phase'] == 'idle'
        # Stop still works from a stale tab.
        assert c.post('/api/blocks/control', json=dict(action='estop', revision=0, session=old_session)).json()['phase'] == 'estop'


def test_reset_keeps_completed_report_when_archive_cannot_be_written(tmp_path, monkeypatch):
    clock = Clock()
    e = BlockEngine(tmp_path, clock)
    e.control('start', e.revision)
    for _ in range(18):
        clock.now += .8
        e.tick()
    previous = e.snapshot()
    original_write = e.write
    def fail_archive(filename, value):
        if filename.startswith('run-'):
            raise BlockError('archive disk full')
        return original_write(filename, value)
    monkeypatch.setattr(e, 'write', fail_archive)
    with pytest.raises(BlockError, match='archive disk full'):
        e.control('reset', e.revision)
    assert e.snapshot() == previous


def test_reset_archives_previous_run_before_last_run_is_replaced(tmp_path):
    e = BlockEngine(tmp_path)
    e.control('start', e.revision)
    e.control('estop', e.revision)
    previous = e.snapshot()
    e.control('recover', e.revision)
    assert e.session != previous['session'] and e.phase == 'idle'
    archived = json.loads((tmp_path / f"run-{previous['session']}.json").read_text(encoding='utf-8'))
    assert archived == previous
    assert json.loads((tmp_path / 'last-run.json').read_text(encoding='utf-8'))['session'] == e.session


def test_stationary_workspace_needs_real_sensors_but_no_fake_map_pose(tmp_path):
    e = BlockEngine(tmp_path, Clock())
    e.control('mode', e.revision, 'robot')
    ready(e, operation_mode='stationary', pose=None)
    e.control('record_home', e.revision)
    assert e.home is None and e.snapshot()['workspace_recorded']
    e.control('start', e.revision)
    assert e.command['operation_mode'] == 'stationary' and e.command['home'] is None
    assert e.snapshot()['phase_label'] == '准备固定取料区'
    ready(e, operation_mode='stationary', pose=None, odom_age=10)
    assert e.phase == 'estop'


def test_changing_workspace_mode_invalidates_recording_and_stops(tmp_path):
    e = BlockEngine(tmp_path, Clock())
    e.control('mode', e.revision, 'robot')
    ready(e, operation_mode='stationary', pose=None)
    e.control('record_home', e.revision)
    e.control('start', e.revision)
    ready(e, operation_mode='mobile')
    assert e.phase == 'estop' and not e.armed and e.home_owner is None
