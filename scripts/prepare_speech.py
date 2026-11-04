"""Generate fixed phrases once using Edge TTS; matches use only cached WAV files."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

from spark_freeze.speech import PHRASES


async def prepare(output, voice):
    import edge_tts
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to convert downloaded speech to PCM WAV")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"provider": "edge-tts (online synthesis, offline playback)", "voice": voice, "clips": {}}
    with tempfile.TemporaryDirectory(prefix="spark-speech-") as temp:
        for key, text in PHRASES.items():
            mp3 = Path(temp) / (key + ".mp3")
            wav = Path(temp) / (key + ".wav")
            await asyncio.wait_for(edge_tts.Communicate(text, voice).save(str(mp3)), 45)
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(mp3),
                            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, timeout=20)
            data = wav.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            filename = key + "-" + digest[:16] + ".wav"
            with wave.open(str(wav)) as f:duration = f.getnframes() / f.getframerate()
            if not 0.1 <= duration <= 15:raise ValueError("phrase duration out of bounds")
            (output / filename).write_bytes(data)
            manifest["clips"][key] = dict(filename=filename, sha256=digest, text=text, duration=round(duration, 3))
            print(key, round(duration, 2), "seconds", flush=True)
    temporary = output / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output / "manifest.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "assets/speech")
    parser.add_argument("--voice", default="zh-CN-YunxiNeural")
    args = parser.parse_args()
    asyncio.run(prepare(args.output, args.voice))
