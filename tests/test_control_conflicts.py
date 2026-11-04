import json

import pytest
from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.runtime import Runtime


@pytest.mark.parametrize('operation', ['reset', 'config', 'clear_estop'])
def test_archive_failure_keeps_old_session_available_for_export(tmp_path, operation):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('occupied')
    runtime = Runtime(blocked)
    client = TestClient(create_app(runtime))  # No device/clock workers.
    engine = runtime.engine
    if operation == 'clear_estop':
        engine.estop()
    original = engine.session_id
    original_config = engine.config.model_dump()
    engine.log('info', 'Must remain exportable after a failed reset')
    if operation == 'config':
        result = client.post('/api/config', json={'players': 1, 'expected_session_id': original})
    else:
        result = client.post('/api/control', json={
            'action': operation, 'expected_session_id': original, 'expected_revision': engine.revision})
    assert result.status_code == 409
    assert engine.session_id == original
    assert engine.config.model_dump() == original_config
    assert client.get('/api/report').json()['events'][-1]['text'].startswith('Must remain')
    # The stop operation must remain available even with an unwritable disk.
    assert client.post('/api/control', json={'action': 'estop'}).status_code == 200


def test_late_resume_does_not_resume_a_later_pause(tmp_path):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    engine = runtime.engine
    engine.camera['ready'] = True
    engine.start()
    engine.pause()
    request = {'action': 'resume', 'expected_session_id': engine.session_id,
               'expected_revision': engine.revision}
    assert client.post('/api/control', json=request).status_code == 200
    assert client.post('/api/control', json={'action': 'pause'}).status_code == 200
    assert client.post('/api/control', json=request).status_code == 409
    assert engine.phase == 'paused'


def test_stale_confirmation_after_another_action_is_rejected(tmp_path):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    engine = runtime.engine
    engine.camera['ready'] = True
    engine.start()
    revision = engine.revision
    engine.pause()
    result = client.post('/api/control', json={'action': 'declare_winner', 'player_id': 1,
        'expected_session_id': engine.session_id, 'expected_revision': revision})
    assert result.status_code == 409
    assert engine.players[0]['status'] == 'active'


def test_missing_resume_version_cannot_bypass_conflict_check(tmp_path):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    runtime.engine.camera['ready'] = True
    runtime.engine.start()
    runtime.engine.pause()
    assert client.post('/api/control', json={'action': 'resume'}).status_code == 409


def test_reset_recovers_after_storage_is_repaired(tmp_path):
    blocked = tmp_path / 'blocked'
    blocked.write_text('occupied')
    runtime = Runtime(blocked)
    client = TestClient(create_app(runtime))
    original = runtime.engine.session_id
    runtime.engine.log('info', 'Preserve this game')
    assert client.post('/api/control', json={'action': 'reset'}).status_code == 409
    runtime.data_dir = tmp_path / 'working-directory'
    result = client.post('/api/control', json={'action': 'reset', 'expected_session_id': original})
    assert result.status_code == 200
    assert result.json()['session_id'] != original
    report = json.loads((runtime.data_dir / 'sessions' / original / 'report.json').read_text(encoding='utf-8'))
    assert report['events'][-1]['text'] == 'Preserve this game'


def test_current_version_can_confirm_winner(tmp_path):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    runtime.engine.camera['ready'] = True
    runtime.engine.start()
    result = client.post('/api/control', json={'action': 'declare_winner', 'player_id': 1,
        'expected_session_id': runtime.engine.session_id, 'expected_revision': runtime.engine.revision})
    assert result.status_code == 200
    assert result.json()['phase'] == 'finished'
    assert result.json()['players'][0]['status'] == 'winner'
