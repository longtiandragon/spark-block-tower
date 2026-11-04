"""Cached speech assets; synthesis never runs on the match clock thread."""
import hashlib
import json
import os
from pathlib import Path

PHRASES = {
    "ready": "各位选手请准备，站好自己的赛道。",
    "count": "一，二，三。",
    "freeze": "木头人！请保持静止。",
    "paused": "比赛暂停，请等待裁判。",
    "finished": "本场比赛结束，请查看比赛记录。",
    "estop": "已紧急停止，请勿靠近机器人。",
    "test": "我是 SPARK 裁判。语音播放测试完成。",
}
PHASE_CLIPS = {"countdown": "ready", "green": "count", "turning_back": "freeze",
               "paused": "paused", "finished": "finished", "estop": "estop"}


class SpeechAssets:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv("SPARK_SPEECH_DIR", str(
            Path(__file__).resolve().parents[2] / "assets/speech")))
        self.clips = {}
        self.error = None
        try:
            manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
            for key, text in PHRASES.items():
                item = manifest["clips"][key]
                filename = item["filename"]
                if Path(filename).name != filename or not filename.endswith(".wav"):
                    raise ValueError("invalid filename")
                path = self.directory / filename
                data = path.read_bytes()
                if not 44 < len(data) <= 2_000_000 or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise ValueError("audio checksum mismatch")
                duration = float(item["duration"])
                if not 0.1 <= duration <= 15 or item["text"] != text:
                    raise ValueError("invalid phrase")
                self.clips[key] = dict(item, url="/api/speech/assets/" + filename)
        except (OSError, ValueError, KeyError, TypeError):
            self.clips = {}
            self.error = "语音资源未准备好，请运行 scripts/prepare_speech.py 后重启服务"

    def status(self):
        return {"ready": len(self.clips) == len(PHRASES), "error": self.error,
                "clips": self.clips}

    def path(self, filename):
        if filename not in {c["filename"] for c in self.clips.values()}:
            return None
        return self.directory / filename
