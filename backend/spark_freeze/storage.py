"""Serialized report writes without holding the game-state lock."""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from concurrent.futures import Future


class ReportWriter:
    """One daemon writer; pending snapshots of the same session are coalesced.

    A Future acknowledges the snapshot actually written. Archive callers must
    recheck game session/revision after waiting, since controls remain live.
    The thread exits when idle; blocked OS writes cannot block process exit.
    """

    def __init__(self, completed, capacity=32):
        self.completed = completed
        self.capacity = capacity
        self.lock = threading.Lock()
        self.pending = OrderedDict()
        self.thread = None

    def submit(self, directory, report, sequence):
        with self.lock:
            if directory in self.pending:
                future = self.pending[directory][3]
            elif len(self.pending) >= self.capacity:
                future = Future()
                future.set_result(OSError('report queue full'))
                return future
            else:
                future = Future()
            self.pending[directory] = (report, sequence, directory, future)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name='report-writer')
                self.thread.start()
            return future

    def _run(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.thread = None
                    return
                _, (report, sequence, directory, future) = self.pending.popitem(last=False)
            error = None
            try:
                directory.mkdir(parents=True, exist_ok=True)
                temp = directory / 'report.tmp'
                temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(directory / 'report.json')
            except Exception as exc:
                error = exc
            # No writer lock is held when acquiring the runtime lock here.
            try:
                self.completed(report['session_id'], sequence, error)
            finally:
                future.set_result(error)
