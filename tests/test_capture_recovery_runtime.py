import time
from types import SimpleNamespace

import numpy as np

from spark_freeze.runtime import Runtime


def test_fast_disconnect_and_reconnect_during_inference_is_not_missed(tmp_path, monkeypatch):
    import spark_freeze.capture as capture_module
    import spark_freeze.vision as vision_module

    class Camera:
        recovery_epoch = 0
        def __init__(self, *_):
            pass
        def start(self):
            pass
        def read_latest(self, seq):
            return seq+1, np.zeros((80, 80, 3), np.uint8), time.monotonic(), None
        def stop(self):
            pass

    camera = Camera()

    class Pipeline:
        def __init__(self, **_):
            pass
        def load(self):
            pass
        def reset_baseline(self):
            pass
        def process(self, frame, now):
            if camera.recovery_epoch == 0:
                camera.recovery_epoch = 1  # Device failed and recovered during this inference.
            return SimpleNamespace(annotated=frame, observations=[], stable=True)

    monkeypatch.setattr(capture_module, 'WebcamCapture', lambda *_: camera)
    monkeypatch.setattr(vision_module, 'VisionPipeline', Pipeline)
    runtime = Runtime(tmp_path)
    runtime.engine.mode = 'webcam'
    runtime.engine.camera['ready'] = True
    runtime.engine.game_deadline = time.monotonic() + 60
    runtime.engine.transition('red', 30)
    runtime.frame_time = time.monotonic()
    runtime.start()
    try:
        deadline = time.monotonic() + 1
        while runtime.engine.phase == 'red' and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime.engine.phase == 'paused'
        assert '中断' in runtime.engine.message or '输入异常' in runtime.engine.message
    finally:
        runtime.stop()
