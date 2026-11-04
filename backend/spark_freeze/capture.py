"""Continuously drain a webcam into a single latest-frame slot.

OpenCV's driver buffer-size setting is only a hint. Capture must keep running
while the separate vision worker performs inference. This module never opens a
device until ``start`` and can be tested with an injected capture factory.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any, Callable

import cv2
import numpy as np


logger = logging.getLogger(__name__)


class WebcamCapture:
    """A nonblocking consumer API over a reconnecting camera worker.

    ``read_latest(after_seq)`` returns ``(seq, frame, captured_at, error)``.
    ``frame`` is an owned copy only when a newer valid frame exists; otherwise
    it is ``None``. ``captured_at`` is the local monotonic time when ``read``
    completed, not a sensor hardware timestamp. Errors invalidate the last
    frame and persist until capture succeeds. Sequence numbers never rewind,
    including across stop/start on the same instance.

    The factory has the same two-argument signature as
    ``cv2.VideoCapture(camera_index, backend)``. Optional retry bounds support
    deterministic, short device-failure tests without touching real hardware.
    """

    def __init__(
        self,
        camera_index: int,
        *,
        capture_factory: Callable[[int, int], Any] | None = None,
        retry_initial: float = 0.5,
        retry_max: float = 5.0,
    ) -> None:
        if isinstance(camera_index, bool) or not isinstance(camera_index, int) or camera_index < 0:
            raise ValueError("摄像头编号必须是非负整数")
        if not (math.isfinite(retry_initial) and math.isfinite(retry_max)
                and 0 < retry_initial <= retry_max):
            raise ValueError("重连等待时间必须有限、为正且上限不小于初始值")
        self.camera_index = camera_index
        self._factory = capture_factory or cv2.VideoCapture
        self._backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._lock = threading.Lock()
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._seq = 0
        self._recovery_epoch = 0
        self._frame: np.ndarray | None = None
        self._captured_at = 0.0
        self._error: str | None = "摄像头尚未启动"

    def start(self) -> None:
        """Start once, or restart after stop; repeated starts are harmless."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._frame = None
            self._captured_at = 0.0
            self._error = "正在连接摄像头"
            thread = threading.Thread(
                target=self._run, args=(generation, stop_event),
                name=f"webcam-{self.camera_index}-{generation}", daemon=True,
            )
            self._thread = thread
            thread.start()

    def read_latest(self, after_seq: int = 0) -> tuple[int, np.ndarray | None, float, str | None]:
        """Never wait for hardware, inference, reconnection, or another frame."""
        with self._lock:
            frame = self._frame.copy() if self._frame is not None and self._seq > after_seq else None
            return self._seq, frame, self._captured_at, self._error

    @property
    def recovery_epoch(self) -> int:
        """Failures stay observable even if recovery occurred during inference."""
        with self._lock:
            return self._recovery_epoch

    def stop(self) -> None:
        """Invalidate immediately; do not wait indefinitely on a blocked driver.

        Releasing a camera concurrently with a native ``read`` can be unsafe.
        Only its owning worker releases it when that call returns. A stuck
        worker is a daemon and cannot publish into this or a restarted session.
        """
        with self._lock:
            self._generation += 1
            if self._stop_event is not None:
                self._stop_event.set()
            thread = self._thread
            self._thread = None
            self._frame = None
            self._captured_at = 0.0
            self._error = "摄像头已停止"
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _publish(self, generation: int, stop_event: threading.Event,
                 frame: np.ndarray, captured_at: float) -> bool:
        with self._lock:
            if generation != self._generation or stop_event.is_set():
                return False
            self._seq += 1
            # Some capture providers reuse their array on the next read.
            self._frame = frame.copy()
            self._captured_at = captured_at
            self._error = None
            return True

    def _invalidate(self, generation: int, stop_event: threading.Event, message: str) -> None:
        with self._lock:
            if generation != self._generation or stop_event.is_set():
                return
            self._recovery_epoch += 1
            self._frame = None
            self._captured_at = 0.0
            self._error = message

    def _run(self, generation: int, stop_event: threading.Event) -> None:
        delay = self._retry_initial
        while not stop_event.is_set():
            capture = None
            try:
                capture = self._factory(self.camera_index, self._backend)
                if not capture.isOpened():
                    raise RuntimeError(f"无法打开 {self.camera_index} 号摄像头")
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, 960)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 540)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                while not stop_event.is_set():
                    ok, frame = capture.read()
                    captured_at = time.monotonic()
                    if (not ok or not isinstance(frame, np.ndarray) or frame.ndim != 3
                            or frame.shape[2] != 3 or frame.size == 0 or frame.dtype != np.uint8):
                        raise RuntimeError("未收到有效摄像头画面，请检查设备连接或占用")
                    if not self._publish(generation, stop_event, frame, captured_at):
                        return
                    delay = self._retry_initial
            except Exception as exc:
                self._invalidate(generation, stop_event,
                                 f"{type(exc).__name__}: {exc}；{delay:g} 秒后自动重试")
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        logger.warning("Failed to release camera %s", self.camera_index, exc_info=True)
            if stop_event.wait(delay):
                return
            delay = min(self._retry_max, delay * 2)
