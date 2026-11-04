import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spark_freeze.app import create_app
from spark_freeze.runtime import Runtime


def test_repeated_explicit_stop_invalidates_an_older_clear_request(tmp_path):
    runtime = Runtime(tmp_path)
    client = TestClient(create_app(runtime))
    stopped = client.post('/api/control', json={'action': 'estop'}).json()
    old_clear = dict(action='clear_estop', expected_session_id=stopped['session_id'],
                     expected_revision=stopped['revision'])
    client.post('/api/control', json={'action': 'estop'})
    result = client.post('/api/control', json=old_clear)
    try:
        assert result.status_code == 409
        assert runtime.engine.phase == 'estop'
        assert runtime.engine.session_id == stopped['session_id']
        latest = client.get('/api/state').json()
        fresh_clear = {**old_clear, 'expected_revision': latest['revision']}
        assert client.post('/api/control', json=fresh_clear).status_code == 200
    finally:
        runtime.save_report()


def test_stop_during_clear_archive_keeps_emergency_latched(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime.engine.estop()
    client = TestClient(create_app(runtime))
    entered, release = threading.Event(), threading.Event()
    original = Path.write_text

    def write(path, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    session = runtime.engine.session_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        clearing = pool.submit(client.post, '/api/control', json=dict(action='clear_estop',
            expected_session_id=session, expected_revision=runtime.engine.revision))
        try:
            assert entered.wait(1)
            assert client.post('/api/control', json={'action': 'estop'}).status_code == 200
        finally:
            release.set()
        result = clearing.result(timeout=3)
    try:
        assert result.status_code == 409
        assert runtime.engine.phase == 'estop'
        assert runtime.engine.session_id == session
    finally:
        runtime.save_report()


def test_shutdown_waits_for_report_of_photos_completed_during_join(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.transition('red', 10)
    runtime.engine.last_observed = time.monotonic() - .1
    runtime.engine.streaks[1] = time.monotonic() - 1
    photo_entered, photo_release = threading.Event(), threading.Event()
    initial_report, final_report, final_release = threading.Event(), threading.Event(), threading.Event()
    write_bytes, write_text = Path.write_bytes, Path.write_text
    report_count = 0

    def photo(path, data):
        photo_entered.set()
        assert photo_release.wait(3)
        return write_bytes(path, data)

    def report(path, *args, **kwargs):
        nonlocal report_count
        report_count += 1
        if report_count == 1:
            initial_report.set()
        else:
            final_report.set()
            assert final_release.wait(3)
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_bytes', photo)
    monkeypatch.setattr(Path, 'write_text', report)
    capture = threading.Thread(target=runtime._publish, args=(np.zeros((80, 80, 3), np.uint8),
        [dict(player_id=1, visible=True, confidence=1, motion=.2)], True, 0, {1: b'baseline'}))
    runtime.threads = [capture]
    capture.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            assert photo_entered.wait(1)
            stopping = pool.submit(runtime.stop)
            assert initial_report.wait(1)
            photo_release.set()
            assert final_report.wait(1)
            # stop() must not return while the final report is still unwritten.
            with pytest.raises(TimeoutError):
                stopping.result(timeout=.15)
        finally:
            photo_release.set()
            final_release.set()
            capture.join(timeout=2)
        stopping.result(timeout=3)
    stored = json.loads((tmp_path / 'sessions' / runtime.engine.session_id / 'report.json').read_text(encoding='utf-8'))
    assert stored['evidence'][0]['saving'] is False
    assert stored['evidence'][0]['after_url']
    assert stored['phase'] == 'paused'


def test_shutdown_remains_bounded_if_report_writer_never_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr('spark_freeze.runtime.ARCHIVE_TIMEOUT', .05)
    entered, release = threading.Event(), threading.Event()
    original = Path.write_text

    def write(path, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    runtime = Runtime(tmp_path)
    runtime.engine.camera['ready'] = True
    runtime.engine.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(runtime.stop)
        try:
            assert entered.wait(1)
            stopping.result(timeout=1)
            assert runtime.stop_event.is_set()
            assert runtime.engine.phase == 'paused'
            assert not runtime.engine.robot['armed']
            assert '超时' in runtime.engine.storage['error']
        finally:
            release.set()
    runtime.save_report()
