import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.engine import GameConfig, GameEngine
from spark_freeze.runtime import Runtime


@pytest.fixture
def slow_report(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == 'report.tmp':
            entered.set()
            assert release.wait(5), 'test did not release storage'
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    yield entered, release
    release.set()


def test_slow_report_does_not_block_stop_state_or_export(tmp_path, slow_report):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    runtime.engine.camera['ready'] = True
    entered, release = slow_report
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(client.post, '/api/control', json={'action': 'start'})
        try:
            assert entered.wait(2)
            stopped = pool.submit(client.post, '/api/control', json={'action': 'estop'})
            assert stopped.result(timeout=1).json()['phase'] == 'estop'
            assert client.get('/api/state').json()['phase'] == 'estop'
            assert client.get('/api/report').json()['phase'] == 'estop'
        finally:
            release.set()
        assert start.result(timeout=2).status_code == 200
    runtime.save_report()


@pytest.mark.parametrize('operation', ['reset', 'config'])
def test_estop_during_archive_prevents_session_replacement(tmp_path, slow_report, operation):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    original = runtime.engine.session_id
    entered, release = slow_report
    with ThreadPoolExecutor(max_workers=2) as pool:
        replacing = pool.submit(client.post, '/api/config' if operation == 'config' else '/api/control',
                                json={'players': 1} if operation == 'config' else {'action': 'reset'})
        try:
            assert entered.wait(2)
            stop = pool.submit(client.post, '/api/control', json={'action': 'estop'})
            assert stop.result(timeout=1).json()['phase'] == 'estop'
        finally:
            release.set()
        assert replacing.result(timeout=3).status_code == 409
    assert runtime.engine.session_id == original
    assert runtime.engine.phase == 'estop'
    runtime.save_report()


def test_slow_photo_does_not_block_stop_or_allow_premature_review(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime.engine = GameEngine(GameConfig(players=1, auto_eliminate=True))
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.transition('red', 10)
    runtime.engine.last_observed = time.monotonic() - .1
    runtime.engine.streaks[1] = time.monotonic() - 1
    client = TestClient(create_app(runtime))
    entered, release = threading.Event(), threading.Event()
    original = Path.write_bytes

    def write(path, data):
        entered.set()
        assert release.wait(5)
        return original(path, data)

    monkeypatch.setattr(Path, 'write_bytes', write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publish = pool.submit(runtime._publish, np.zeros((80, 80, 3), np.uint8),
                              [dict(player_id=1, visible=True, confidence=1, motion=.2)],
                              True, 0, {1: b'baseline'})
        try:
            assert entered.wait(2)
            stop = pool.submit(client.post, '/api/control', json={'action': 'estop'})
            state = stop.result(timeout=1).json()
            evidence = state['evidence'][0]
            assert evidence['status'] == 'pending'
            assert evidence['saving'] is True
            assert evidence['before_url'] is None and evidence['after_url'] is None
            assert client.post(f"/api/evidence/{evidence['id']}/review",
                               json={'decision': 'confirm'}).status_code == 409
            assert client.post('/api/control', json={'action': 'clear_estop',
                'expected_session_id': state['session_id'], 'expected_revision': state['revision']}).status_code == 409
        finally:
            release.set()
        publish.result(timeout=3)
    assert runtime.engine.phase == 'estop'
    assert runtime.engine.evidence[0]['status'] == 'pending'
    assert runtime.engine.evidence[0]['saving'] is False
    runtime.save_report()


def test_export_does_not_write_to_disk(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))

    def forbidden(*args, **kwargs):
        raise AssertionError('Export should read memory only')

    monkeypatch.setattr(Path, 'write_text', forbidden)
    response = client.get('/api/report')
    assert response.status_code == 200
    assert response.json()['session_id'] == runtime.engine.session_id


def test_clock_keeps_expiring_game_while_report_write_is_blocked(tmp_path, slow_report):
    runtime = Runtime(tmp_path)
    runtime.engine.camera['ready'] = True
    runtime.engine.start()
    runtime.engine.game_deadline = time.monotonic() + .15
    entered, release = slow_report
    # Use the same public control path, without starting a real capture device.
    client = TestClient(create_app(runtime))
    clock = threading.Thread(target=runtime._clock_loop)
    runtime.queue_report()
    clock.start()
    try:
        assert entered.wait(1)
        deadline = time.monotonic() + 1
        while runtime.engine.phase != 'finished' and time.monotonic() < deadline:
            time.sleep(.01)
        assert client.get('/api/state').json()['phase'] == 'finished'
        assert not release.is_set()
    finally:
        runtime.stop_event.set()
        release.set()
        clock.join(timeout=1)
        runtime.save_report()


def test_archive_timeout_keeps_session_and_recovers_after_disk_returns(tmp_path, slow_report, monkeypatch):
    monkeypatch.setattr('spark_freeze.runtime.ARCHIVE_TIMEOUT', .05)
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    original = runtime.engine.session_id
    entered, release = slow_report
    try:
        response = client.post('/api/control', json={'action': 'reset'})
        assert entered.is_set()
        assert response.status_code == 409
        assert runtime.engine.session_id == original
        assert '超时' in runtime.engine.storage['error']
        assert client.get('/api/report').json()['session_id'] == original
    finally:
        release.set()
    runtime.save_report()
    assert runtime.engine.storage['error'] is None
    assert client.post('/api/control', json={'action': 'reset'}).status_code == 200
    runtime.save_report()


def test_queued_reports_are_coalesced_and_latest_snapshot_wins(tmp_path, slow_report):
    runtime = Runtime(tmp_path)
    entered, release = slow_report
    first = runtime.queue_report()
    try:
        assert entered.wait(1)
        for i in range(100):
            with runtime.lock:
                runtime.engine.log('info', f'latest-{i}')
                runtime.queue_report()
        assert len(runtime.report_writer.pending) == 1
    finally:
        release.set()
    runtime.save_report()
    stored = json.loads((tmp_path / 'sessions' / first[0] / 'report.json').read_text(encoding='utf-8'))
    assert stored['events'][-1]['text'] == 'latest-99'
    assert stored['storage']['pending'] is False


def test_successful_photo_storage_still_allows_automatic_elimination(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.engine = GameEngine(GameConfig(players=1, auto_eliminate=True))
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.transition('red', 10)
    runtime.engine.last_observed = time.monotonic() - .1
    runtime.engine.streaks[1] = time.monotonic() - 1
    runtime._publish(np.zeros((80, 80, 3), np.uint8),
        [dict(player_id=1, visible=True, confidence=1, motion=.2)], True, 0, {1: b'baseline'})
    assert runtime.engine.evidence[0]['status'] == 'confirmed'
    assert runtime.engine.evidence[0]['saving'] is False
    assert runtime.engine.phase == 'finished'
    runtime.save_report()


def test_pause_and_resume_during_photo_write_keeps_decision_pending(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime.engine.config.auto_eliminate = True
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.round = 1
    runtime.engine.transition('red', 10)
    runtime.engine.last_observed = time.monotonic() - .1
    runtime.engine.streaks[1] = time.monotonic() - 1
    entered, release = threading.Event(), threading.Event()
    original = Path.write_bytes

    def write(path, data):
        entered.set()
        assert release.wait(3)
        return original(path, data)

    monkeypatch.setattr(Path, 'write_bytes', write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        publish = pool.submit(runtime._publish, np.zeros((80, 80, 3), np.uint8),
                              [dict(player_id=1, visible=True, confidence=1, motion=.2)],
                              True, 0, {1: b'baseline'})
        try:
            assert entered.wait(1)
            with runtime.lock:
                runtime.engine.pause()
                runtime.engine.resume()
        finally:
            release.set()
        publish.result(timeout=2)
    assert runtime.engine.phase == 'turning_back'
    assert runtime.engine.evidence[0]['status'] == 'pending'
    runtime.save_report()
