"""Capture and inference run outside the game timer and HTTP event loop."""
from __future__ import annotations

import math
import os
import threading
import time
from concurrent.futures import TimeoutError
from pathlib import Path

import cv2
import numpy as np

from .engine import GameEngine, GameError, RUNNING
from .storage import ReportWriter
from .speech import SpeechAssets

ROOT = Path(__file__).resolve().parents[2]
MAX_SOURCE_AGE = 1.0
ARCHIVE_TIMEOUT = 2.0


def jpeg(frame):
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 83])
    if not ok:
        raise ValueError("画面编码失败")
    return encoded.tobytes()


def placeholder(text="Waiting for camera"):
    frame = np.full((540, 960, 3), (30, 39, 32), dtype=np.uint8)
    cv2.putText(frame, "SPARK / FREEZE", (60, 230), cv2.FONT_HERSHEY_DUPLEX, 1.5, (186, 237, 224), 2)
    cv2.putText(frame, text[:68], (60, 285), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (165, 185, 177), 1)
    return frame


class Runtime:
    def __init__(self, data_dir: Path | None = None):
        self.lock = threading.RLock()
        self.engine = GameEngine()
        self.speech_assets = SpeechAssets()
        self.audio_time = 0.0
        self.data_dir = data_dir or Path(os.getenv("SPARK_DATA_DIR", str(ROOT / "data")))
        self.last_frame = jpeg(placeholder("Starting local demo"))
        self.frame_time = 0.0
        self.robot_time = 0.0
        self.operator_time = 0.0
        self.robot_fault = None
        self.odom_age = math.inf
        self.scan_age = math.inf
        self.clearance = None
        self.generation = 0
        self.stop_event = threading.Event()
        self.threads = []
        self.ros_frame = None
        self.ros_frame_time = 0.0
        self.ros_frame_seq = 0
        self.demo_moves = {}
        self.demo_progress = {}
        self.report_sequence = 0
        self.report_completed = 0
        self.report_writer = ReportWriter(self._report_complete)

    def start(self):
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("裁判服务线程仍在运行，不能重复启动")
        self.stop_event.clear()
        self.threads = [threading.Thread(target=self._clock_loop, daemon=True, name="game-clock"),
                        threading.Thread(target=self._capture_loop, daemon=True, name="vision-capture")]
        for thread in self.threads:
            thread.start()

    def stop(self):
        with self.lock:
            if self.engine.phase in RUNNING:
                if self.engine.mode == "ros":
                    self.engine.estop("服务关闭，机器人已停止")
                else:
                    self.engine.pause("服务关闭，正在保存比赛记录")
            self.engine.robot["armed"] = False
            self.stop_event.set()
            self.queue_report()
        for thread in self.threads:
            thread.join(timeout=2)
        # Capture may finish a pending photo and queue newer metadata during
        # join. Flush a snapshot taken AFTER that work, not only the earlier
        # "saving" state. Both joins and the final flush remain bounded.
        self.save_report()

    def changed_source(self):
        """Called under lock. Invalidate frames before asynchronously reopening capture."""
        self.generation += 1
        self.frame_time = 0
        self.ros_frame = None
        self.ros_frame_time = 0.0
        self.demo_moves.clear()
        self.demo_progress.clear()
        self.engine.camera.update(ready=False, error=None, fps=0, stable=False, last_frame_age=None,
                                  model="模拟演示" if self.engine.mode == "demo" else "YOLOv8n Pose · 正在加载")
        self.last_frame = jpeg(placeholder("Loading camera and pose model"))

    def _clock_loop(self):
        while not self.stop_event.wait(0.05):
            with self.lock:
                now = time.monotonic()
                engine = self.engine
                age = now - self.frame_time if self.frame_time else None
                engine.camera["last_frame_age"] = round(age, 2) if age is not None else None
                if engine.mode != "demo" and engine.camera["ready"] and (age is None or age > 2.5):
                    self._capture_error("摄像头画面已过期，请等待恢复", self.generation)
                engine.robot["connected"] = now - self.robot_time < 1.5
                if engine.mode == "ros" and engine.robot["armed"]:
                    fault = self.robot_unready(now)
                    if fault:
                        engine.estop(fault)
                        self.queue_report()
                if engine.phase in RUNNING and engine.mode != "demo":
                    if age is None or age > 2.5 or not engine.camera["ready"]:
                        engine.camera["ready"] = False
                        if engine.mode == "ros":
                            engine.estop("摄像头画面中断，机器人已停止")
                        else:
                            engine.pause("摄像头画面中断，已暂停；恢复后请点击继续")
                        self.queue_report()
                old_phase = engine.phase
                engine.tick()
                if engine.phase == "finished" and old_phase != "finished":
                    self.queue_report()

    def robot_unready(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.robot_time > 1.5:
            return "机器人连接中断，已停止"
        if self.odom_age + max(0, now - self.robot_time) > 0.5:
            return "里程计数据过期，已停止"
        if self.scan_age + max(0, now - self.robot_time) > 0.5:
            return "雷达数据过期，已停止"
        if self.robot_fault:
            return f"机器人保护停止：{self.robot_fault}"
        if self.clearance is None or self.clearance < 0.4:
            return "机器人周围空间不足或未知，已停止"
        if now - self.operator_time > 3:
            return "裁判界面连接中断，已停止"
        if not self.frame_time or now - self.frame_time > 2.5:
            return "机器人画面中断，已停止"
        if not self.engine.camera["ready"] or self.engine.camera["error"]:
            return "机器人视觉未就绪，已停止"
        return None

    def check_camera_freshness(self):
        """Control requests cannot rely on a ready flag from an old frame."""
        if self.engine.mode != "demo" and (not self.frame_time or time.monotonic() - self.frame_time > 2.5):
            self._capture_error("摄像头画面已过期，请等待恢复", self.generation)

    def report_snapshot(self):
        report = self.engine.snapshot()
        report["events"] = list(self.engine.events)
        report["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        report["limitations"] = ["玩家编号代表固定赛道，不是身份识别", "终点由裁判人工确认",
                                  "demo 是合成数据，不能作为真实 AI 或机器人性能测试"]
        return report

    def _evidence_error(self):
        return next((e["storage_error"] for e in self.engine.evidence if e.get("storage_error")), None)

    def _report_complete(self, session_id, sequence, error):
        with self.lock:
            if session_id != self.engine.session_id or sequence < self.report_completed:
                return
            self.report_completed = sequence
            self.engine.storage["pending"] = sequence < self.report_sequence
            self.engine.storage["error"] = (f"比赛记录未保存（{type(error).__name__}），请检查磁盘空间和数据目录权限；仍可导出内存中的记录。"
                                             if error else self._evidence_error())

    def queue_report(self):
        """Snapshot under the state lock, perform all filesystem work elsewhere."""
        with self.lock:
            self.report_sequence += 1
            sequence = self.report_sequence
            session_id = self.engine.session_id
            self.engine.storage["pending"] = True
            report = self.report_snapshot()
            report["storage"] = {"error": self._evidence_error(), "pending": False}
            future = self.report_writer.submit(self.data_dir / "sessions" / session_id, report, sequence)
            if future.done():
                self._report_complete(session_id, sequence, future.result())
            return session_id, self.engine.revision, future

    def _wait_report(self, ticket, *, required=False):
        """Must be called outside runtime.lock so stop/timer remain responsive."""
        session_id, _, future = ticket
        try:
            error = future.result(timeout=ARCHIVE_TIMEOUT)
        except TimeoutError as exc:
            error = exc
            with self.lock:
                if session_id == self.engine.session_id and not future.done():
                    self.engine.storage["error"] = "比赛记录保存超时，仍可停止比赛或导出内存记录；请检查磁盘。"
        if error and required:
            raise GameError("旧场次未能保存，已保留当前记录。请修复磁盘或目录权限后重试；仍可导出内存记录。") from error

    def prepare_archive(self):
        # Called while the API holds the state lock.
        if any(e.get("saving") for e in self.engine.evidence):
            raise GameError("证据图片仍在保存，已保留当前场次；可停止比赛或导出内存记录。")
        return self.queue_report()

    def verify_archive(self, ticket):
        if (self.engine.session_id, self.engine.revision) != ticket[:2]:
            raise GameError("归档期间比赛状态已经改变，已取消重置；请同步后重新操作。")
        if any(e.get("saving") for e in self.engine.evidence):
            raise GameError("证据图片仍在保存，请稍后重试。")

    def save_report(self, *, require_persist=False):
        """Bounded synchronous flush for shutdown/tests; never call under lock."""
        ticket = self.queue_report()
        self._wait_report(ticket, required=require_persist)
        with self.lock:
            return self.report_snapshot()

    def _publish(self, frame, observations, stable, generation, before_frames, baseline_created=(), baseline_reset=False,
                 stability_reason="", model="YOLOv8n Pose", elapsed=0.1, expected_epoch=None, captured_at=None):
        encoded = jpeg(frame)
        with self.lock:
            now = time.monotonic()
            if generation != self.generation or self.stop_event.is_set():
                return
            if expected_epoch is not None and expected_epoch != self.engine.baseline_epoch:
                # A phase/reset/review changed while inference was running. Its
                # old pose baseline must not enter the new judging interval.
                return
            captured_at = now if captured_at is None else captured_at
            if (not math.isfinite(captured_at) or captured_at > now
                    or (self.frame_time and captured_at <= self.frame_time)):
                return
            if captured_at < self.engine.baseline_started_at:
                return
            if now - captured_at > MAX_SOURCE_AGE:
                self._capture_error("视觉处理延迟过高，旧画面已丢弃；请关闭其他高负载程序", generation)
                return
            if baseline_reset:
                before_frames.clear()
            for pid in baseline_created:
                before_frames[pid] = encoded
            self.last_frame = encoded
            self.frame_time = captured_at
            self.engine.camera.update(ready=True, error=None, model=model, fps=round(1 / max(elapsed, 0.001), 1),
                                      width=frame.shape[1], height=frame.shape[0], last_frame_age=round(now-captured_at, 2),
                                      stable=stable, stability_reason=stability_reason)
            new = self.engine.observe(observations, stable, finalize=False, observed_at=captured_at,
                                      defer_evidence=True)
            session_id = self.engine.session_id
            auto_eliminate = self.engine.config.auto_eliminate
            decision_command = self.engine.command_id
            directory = self.data_dir / "sessions" / session_id
            writes = []
            for item in new:
                writes.append((item, before_frames.get(item["player_id"]), item["before_url"], item["after_url"]))
                item.update(saving=True, before_url=None, after_url=None)
        # Image I/O may block. Keep game state and emergency controls available;
        # the pending record is neither reviewable nor automatically eliminated.
        for item, baseline, before_url, after_url in writes:
            evidence_error = None
            try:
                evidence_dir = directory / item["id"]
                evidence_dir.mkdir(parents=True, exist_ok=True)
                if baseline is None:
                    before_url = None
                    evidence_error = "判罚缺少基准画面，前后图像证据不完整，需人工复核。"
                else:
                    (evidence_dir / "before.jpg").write_bytes(baseline)
                (evidence_dir / "after.jpg").write_bytes(encoded)
            except OSError as exc:
                before_url = after_url = None
                evidence_error = f"证据图片未保存（{type(exc).__name__}），不能作为图像证据。"
            with self.lock:
                if session_id != self.engine.session_id:
                    continue
                item.update(saving=False, before_url=before_url, after_url=after_url)
                if evidence_error:
                    item["storage_error"] = evidence_error
                    self.engine.storage["error"] = evidence_error
                    self.engine.log("storage", f"{item['player_id']:02d} 号玩家证据不完整，判罚改为待复核。{evidence_error}")
                    if self.engine.phase in RUNNING:
                        if self.engine.mode == "ros":
                            self.engine.estop("判罚证据不完整，已停止机器人；请检查证据提示")
                        else:
                            self.engine.pause("判罚证据不完整，已暂停比赛；请检查证据提示")
                elif (auto_eliminate and self.engine.phase in RUNNING
                      and self.engine.command_id == decision_command):
                    item["status"] = "confirmed"
                    self.engine.player(item["player_id"])["status"] = "eliminated"
                    self.engine.log("violation", f"{item['player_id']:02d} 号玩家证据已保存，已自动淘汰。")
                else:
                    self.engine.log("storage", f"{item['player_id']:02d} 号玩家证据已保存，等待人工复核。")
                self.engine.finalize_observations()
                self.queue_report()

    def _capture_error(self, message, generation):
        with self.lock:
            if generation != self.generation:
                return
            if self.engine.camera["ready"] or self.engine.camera["error"] != message:
                self.engine.invalidate_baseline()
            self.engine.streaks.clear()
            for player in self.engine.players:
                player.update(visible=False, motion=0.0, confidence=0.0)
            self.engine.camera.update(ready=False, error=message, fps=0, stable=False)
            self.last_frame = jpeg(placeholder("Camera unavailable - see status panel"))
            if self.engine.phase in RUNNING and self.engine.mode != "demo":
                if self.engine.mode == "ros":
                    self.engine.estop("画面输入异常，机器人已停止")
                else:
                    self.engine.pause("画面输入异常，已暂停；恢复画面后请手动继续")
                self.queue_report()

    def _capture_loop(self):
        capture = None
        pipeline = None
        current_generation = -1
        baseline_epoch = -1
        before_frames = {}
        last_ros_seq = -1
        last_camera_seq = 0
        last_camera_recovery = 0
        setup_error = False
        try:
            while not self.stop_event.is_set():
                start = time.monotonic()
                with self.lock:
                    generation = self.generation
                    mode = self.engine.mode
                    config = self.engine.config.model_copy()
                    epoch = self.engine.baseline_epoch
                    baseline_started_at = self.engine.baseline_started_at
                if generation != current_generation:
                    if capture is not None:
                        capture.stop()
                    capture = None
                    last_camera_seq = 0
                    last_camera_recovery = 0
                    pipeline = None
                    before_frames = {}
                    baseline_epoch = -1
                    setup_error = False
                    current_generation = generation
                    if mode != "demo":
                        try:
                            from .vision import VisionPipeline
                            weights = os.getenv("SPARK_MODEL", str(ROOT / "models" / "yolov8n-pose.pt"))
                            (ROOT / "models").mkdir(parents=True, exist_ok=True)
                            pipeline = VisionPipeline(weights=weights, lane_count=config.players)
                            pipeline.load()
                            if mode == "webcam":
                                from .capture import WebcamCapture
                                capture = WebcamCapture(config.camera_index)
                                capture.start()
                        except Exception as exc:
                            setup_error = True
                            self._capture_error(f"{type(exc).__name__}: {exc}。可切换回演示模式；重试请重新选择模式。", generation)
                if setup_error:
                    self.stop_event.wait(0.3)
                    continue
                if epoch != baseline_epoch:
                    baseline_epoch = epoch
                    before_frames = {}
                    if pipeline is not None:
                        pipeline.reset_baseline()
                if mode == "demo":
                    frame, observations = self._demo_frame(start)
                    new_baselines = [o["player_id"] for o in observations if o["player_id"] not in before_frames]
                    self._publish(frame, observations, True, generation, before_frames, new_baselines,
                                  model="模拟演示 · 非真实检测", elapsed=0.1, expected_epoch=epoch)
                    self.stop_event.wait(max(0, 0.1 - (time.monotonic() - start)))
                    continue
                try:
                    if mode == "webcam":
                        seq, frame, captured_at, error = capture.read_latest(last_camera_seq)
                        recovery = capture.recovery_epoch
                        if recovery != last_camera_recovery:
                            last_camera_recovery = recovery
                            last_camera_seq = seq
                            self._capture_error("摄像头曾中断，需重新建立判罚基准", generation)
                            self.stop_event.wait(0.03)
                            continue
                        if error:
                            self._capture_error(error, generation)
                        if frame is None:
                            self.stop_event.wait(0.03)
                            continue
                        last_camera_seq = seq
                    else:
                        with self.lock:
                            if self.ros_frame_seq == last_ros_seq or self.ros_frame is None:
                                frame = None
                            else:
                                last_ros_seq = self.ros_frame_seq
                                frame = self.ros_frame.copy()
                                captured_at = self.ros_frame_time
                        if frame is None:
                            self.stop_event.wait(0.03)
                            continue
                    if captured_at < baseline_started_at:
                        continue
                    if frame.shape[1] > 1280:
                        frame = cv2.resize(frame, (1280, int(frame.shape[0] * 1280 / frame.shape[1])))
                    analysis = pipeline.process(frame, captured_at)
                    if mode == "webcam" and capture.recovery_epoch != last_camera_recovery:
                        last_camera_recovery = capture.recovery_epoch
                        self._capture_error("摄像头在推理期间中断，旧结果已丢弃", generation)
                        continue
                    self._publish(analysis.annotated, analysis.observations, analysis.stable, generation,
                                  before_frames, getattr(analysis, "baseline_created", []),
                                  getattr(analysis, "baseline_reset", False), getattr(analysis, "stability_reason", ""),
                                  elapsed=time.monotonic() - start, expected_epoch=epoch, captured_at=captured_at)
                except Exception as exc:
                    self._capture_error(f"视觉处理失败：{type(exc).__name__}: {exc}", generation)
                    self.stop_event.wait(0.3)
                self.stop_event.wait(max(0, 0.075 - (time.monotonic() - start)))
        finally:
            if capture is not None:
                capture.stop()

    def _demo_frame(self, now):
        with self.lock:
            state = self.engine.snapshot()
            players = state["players"]
            phase = state["phase"]
            observations = []
            frame = np.full((540, 960, 3), (27, 37, 31), dtype=np.uint8)
            cv2.rectangle(frame, (0, 0), (960, 65), (18, 25, 22), -1)
            cv2.putText(frame, "SYNTHETIC DEMO / NO CAMERA", (145, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (142, 231, 223), 1)
            cv2.putText(frame, phase.upper(), (650, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 220, 200), 1)
            cv2.line(frame, (0, 470), (960, 470), (160, 182, 172), 2)
            count = len(players)
            for player in players:
                pid = player["id"]
                lane_width = 960 / count
                cx = int(lane_width * (pid - 0.5))
                cv2.line(frame, (int(lane_width * (pid - 1)), 65), (int(lane_width * (pid - 1)), 540), (65, 83, 72), 1)
                moving = phase == "red" and self.demo_moves.get(pid, 0) > now
                color = (95, 222, 199) if player["status"] == "active" else (95, 134, 236)
                if player["status"] == "eliminated":
                    color = (90, 90, 90)
                bob = int(8 * math.sin(now * 6 + pid)) if phase == "green" else 0
                sway = int(25 * math.sin(now * 5)) if moving else 0
                cy = 260 + bob
                cv2.rectangle(frame, (cx - 65, cy - 95), (cx + 65, cy + 140), color, 1)
                cv2.circle(frame, (cx, cy - 55), 25, color, 2)
                cv2.line(frame, (cx, cy - 30), (cx, cy + 65), color, 3)
                cv2.line(frame, (cx, cy), (cx - 45, cy + 40), color, 3)
                cv2.line(frame, (cx, cy), (cx + 45, cy + 30 - sway), color, 3)
                cv2.line(frame, (cx, cy + 65), (cx - 30, cy + 125), color, 3)
                cv2.line(frame, (cx, cy + 65), (cx + 30, cy + 125), color, 3)
                cv2.putText(frame, f"LANE {pid:02d}", (cx - 49, 445), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1)
                cv2.putText(frame, "MOVEMENT" if moving else player["status"].upper(), (cx - 50, 500), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
                observations.append(dict(player_id=pid, visible=True, confidence=1.0, motion=0.15 if moving else 0.002,
                                         ambiguous=False, bbox=[cx - 65, cy - 95, cx + 65, cy + 140]))
            return frame, observations
