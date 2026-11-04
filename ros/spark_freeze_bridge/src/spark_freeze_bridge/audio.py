"""Independent, bounded robot audio worker. Python 3.8; no ROS dependency.

Only checksum-verified fixed WAV assets from the configured backend are played.
This worker never owns the velocity lock, and disconnect cancels playback.
"""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

import requests


class AudioWorker:
    def __init__(self, base_url, headers, cache_dir, device="", enabled=False):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.cache_dir = Path(cache_dir).expanduser()
        self.device = device
        if not isinstance(enabled, bool):raise ValueError("audio_enabled must be a boolean")
        self.enabled = enabled
        self.owner = uuid.uuid4().hex
        self.done = threading.Event()
        self.process = None
        self.playback_lock = threading.RLock()
        self.current = None
        self.paths = {}
        self.session = requests.Session()
        self.session.trust_env = False

    def json_request(self, path, payload=None):
        response = self.session.request("POST" if payload is not None else "GET",
            self.base_url + path, json=payload, headers=self.headers,
            timeout=(0.3, 0.7), allow_redirects=False)
        response.raise_for_status()
        return response.json()

    def prepare(self):
        if not shutil.which("aplay"):raise RuntimeError("aplay_not_installed")
        catalog = self.json_request("/api/speech/catalog")
        if not catalog.get("ready"):raise RuntimeError("backend_audio_not_ready")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for key, item in catalog["clips"].items():
            name, digest = item["filename"], item["sha256"]
            if not re.fullmatch(r"[a-z]+-[a-f0-9]{16}\.wav", name):raise ValueError("bad_asset_name")
            path = self.cache_dir / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                with self.session.get(self.base_url + "/api/speech/assets/" + name,
                    timeout=(0.5, 2), stream=True, allow_redirects=False) as response:
                    response.raise_for_status()
                    data = bytearray()
                    began = time.monotonic()
                    for chunk in response.iter_content(16384):
                        if self.done.is_set():raise RuntimeError("shutdown")
                        data.extend(chunk)
                        if len(data) > 2_000_000 or time.monotonic()-began > 10:raise ValueError("asset_too_large_or_slow")
                if hashlib.sha256(data).hexdigest() != digest:raise ValueError("asset_checksum_mismatch")
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                temp.replace(path)
            paths[key] = path
        self.paths = paths

    def stop_playback(self):
        with self.playback_lock:self._stop_playback()

    def _stop_playback(self):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.4)

    def ack(self, speech, status, error=None):
        return self.json_request("/api/robot/speech/ack", dict(speech_id=speech["id"],
            session_id=speech["session_id"], status=status, owner=self.owner, error=error))

    def handle(self, speech):
        with self.playback_lock:
            if self.done.is_set():return
            self._handle(speech)

    def _handle(self, speech):
        if not speech or speech["target"] != "robot":
            self.stop_playback()
            self.current = None
            return
        if speech["status"] in {"done", "failed"}:
            self.stop_playback()
            self.current = speech["id"]
            return
        if speech["id"] != self.current:
            self.stop_playback()
            self.current = speech["id"]
            if speech["status"] != "pending":return
            self.ack(speech, "claimed")
            try:
                path = self.paths[speech["clip"]]
                command = ["aplay", "-q"] + (["-D", self.device] if self.device else []) + [str(path)]
                # Acknowledgement is recorded before spawning; a very short clip
                # still cannot report done without a playing transition.
                self.ack(speech, "playing")
                self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.playback_started = time.monotonic()
            except Exception:
                self.stop_playback()
                self.ack(speech, "failed", "无法启动音频播放，请检查 ALSA 输出设备")
        elif self.process is not None:
            result = self.process.poll()
            if result is not None:
                self.ack(speech, "done" if result == 0 else "failed", None if result == 0 else "ALSA 播放失败")
                self.process = None
            elif time.monotonic() - self.playback_started > 17:
                self.stop_playback()
                self.ack(speech, "failed", "音频播放超时")

    def run(self):
        while not self.done.is_set():
            try:
                if self.enabled and not self.paths:self.prepare()
                packet = self.json_request("/api/robot/speech/poll", {
                    "ready": self.enabled and bool(self.paths),
                    "error": None if self.enabled else "请启用 audio_enabled"})
                if self.enabled:self.handle(packet.get("speech"))
            except Exception as error:
                self.stop_playback()
                try:self.json_request("/api/robot/speech/poll", {"ready":False,"error":"音频模块异常："+type(error).__name__})
                except Exception:pass
                # Do not replay an interrupted id after reconnect. Its missing
                # completion acknowledgement makes the match pause on timeout.
                self.done.wait(1)
            self.done.wait(0.15)
        self.stop_playback()
        self.session.close()

    def close(self):
        self.done.set()
        self.stop_playback()
