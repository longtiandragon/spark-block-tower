"""Fake devices verify capture behavior without webcam access or model loads."""

import queue
import threading
import time

import numpy as np
import pytest

from spark_freeze.capture import WebcamCapture


class FakeCapture:
    def __init__(self, *, opened=True):
        self.opened = opened
        self.frames = queue.Queue()
        self.reading = threading.Event()
        self.released = threading.Event()
        self.settings = {}

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.settings[prop] = value
        return True

    def read(self):
        self.reading.set()
        item = self.frames.get(timeout=5)
        if isinstance(item, Exception):
            raise item
        return (False, None) if item is None else (True, item)

    def release(self):
        self.released.set()

    def put(self, value):
        self.frames.put(np.full((20, 30, 3), value, dtype=np.uint8))


def wait_for(check, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError("capture condition timed out")


def shutdown(camera, *devices):
    # Allow pending read() to return before normal cleanup, except in the
    # explicit blocked-read test below.
    for device in devices:
        device.frames.put(None)
    camera.stop()


def test_latest_frame_only_and_copies_owned_by_consumer():
    device = FakeCapture()
    camera = WebcamCapture(0, capture_factory=lambda *_: device)
    try:
        camera.start()
        for value in range(1, 21):
            device.put(value)
        wait_for(lambda: camera.read_latest()[0] == 20)
        seq, frame, stamp, error = camera.read_latest()
        assert seq == 20 and np.all(frame == 20) and stamp > 0 and error is None
        frame[:] = 255
        assert np.all(camera.read_latest()[1] == 20)
        assert camera.read_latest(after_seq=seq)[1] is None
        assert len(device.settings) == 3
    finally:
        shutdown(camera, device)


def test_disconnect_invalidates_old_frame_and_reopens_device():
    first, second = FakeCapture(), FakeCapture()
    devices = iter([first, second])
    camera = WebcamCapture(0, capture_factory=lambda *_: next(devices),
                           retry_initial=.05, retry_max=.1)
    try:
        camera.start()
        first.put(1)
        wait_for(lambda: camera.read_latest()[0] == 1)
        first.frames.put(None)
        wait_for(first.released.is_set)
        seq, frame, stamp, error = camera.read_latest()
        assert seq == 1 and frame is None and stamp == 0 and "自动重试" in error
        wait_for(second.reading.is_set)
        second.put(2)
        wait_for(lambda: camera.read_latest()[0] == 2)
        assert np.all(camera.read_latest()[1] == 2)
        assert camera.read_latest()[3] is None
    finally:
        shutdown(camera, first, second)


def test_failed_open_and_read_exception_recover_without_busy_loop():
    closed, broken, recovered = FakeCapture(opened=False), FakeCapture(), FakeCapture()
    devices = iter([closed, broken, recovered])
    open_times = []

    def factory(*_):
        open_times.append(time.monotonic())
        return next(devices)

    broken.frames.put(RuntimeError("device disconnected"))
    camera = WebcamCapture(0, capture_factory=factory, retry_initial=.03, retry_max=.06)
    try:
        camera.start()
        wait_for(recovered.reading.is_set)
        assert closed.released.is_set() and broken.released.is_set()
        assert open_times[1] - open_times[0] >= .025
        assert open_times[2] - open_times[1] >= .055
        recovered.put(3)
        wait_for(lambda: camera.read_latest()[0] == 1)
        assert camera.read_latest()[3] is None
    finally:
        shutdown(camera, closed, broken, recovered)


def test_stop_is_bounded_and_old_worker_cannot_publish_after_restart():
    old, current = FakeCapture(), FakeCapture()
    devices = iter([old, current])
    camera = WebcamCapture(0, capture_factory=lambda *_: next(devices))
    camera.start()
    assert old.reading.wait(1)
    started = time.monotonic()
    camera.stop()
    assert time.monotonic() - started < 1.5
    assert camera.read_latest()[1] is None
    camera.start()
    camera.start()  # idempotent: must not create a third worker
    try:
        assert current.reading.wait(1)
        current.put(8)
        wait_for(lambda: camera.read_latest()[0] == 1)
        old.put(99)  # late result from the disconnected generation
        wait_for(old.released.is_set)
        assert camera.read_latest()[0] == 1
        assert np.all(camera.read_latest()[1] == 8)
    finally:
        shutdown(camera, old, current)
    assert camera.read_latest()[1] is None
    assert camera.read_latest()[2] == 0


def test_read_latest_never_waits_for_a_new_device_frame():
    device = FakeCapture()
    camera = WebcamCapture(0, capture_factory=lambda *_: device)
    try:
        camera.start()
        assert device.reading.wait(1)
        started = time.monotonic()
        for _ in range(50):
            assert camera.read_latest()[1] is None
        assert time.monotonic() - started < .1
    finally:
        shutdown(camera, device)


def test_recovery_counter_remembers_failure_after_error_message_is_cleared():
    first, second = FakeCapture(), FakeCapture()
    devices = iter([first, second])
    camera = WebcamCapture(0, capture_factory=lambda *_: next(devices),
                           retry_initial=.01, retry_max=.02)
    try:
        camera.start()
        first.put(1)
        wait_for(lambda: camera.read_latest()[0] == 1)
        epoch = camera.recovery_epoch
        first.frames.put(None)
        wait_for(second.reading.is_set)
        second.put(2)
        wait_for(lambda: camera.read_latest()[0] == 2)
        assert camera.read_latest()[3] is None
        assert camera.recovery_epoch > epoch
    finally:
        shutdown(camera, first, second)


@pytest.mark.parametrize("kwargs", [
    {"camera_index": -1}, {"camera_index": 0.5},
    {"camera_index": 0, "retry_initial": 0},
    {"camera_index": 0, "retry_initial": 1, "retry_max": .5},
    {"camera_index": 0, "retry_max": float("inf")},
])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        WebcamCapture(**kwargs)
