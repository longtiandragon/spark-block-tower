from __future__ import annotations

import hmac
import io
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from . import __version__
from .engine import GameConfig, GameError, RUNNING
from .runtime import ROOT, Runtime
from .assistant import RefereeAssistant


class Control(BaseModel):
    action: Literal["start", "pause", "resume", "reset", "clear_estop", "estop", "finish", "demo_move", "declare_winner"]
    player_id: int | None = Field(default=None, strict=True, ge=1, le=4)
    expected_session_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class Review(BaseModel):
    decision: Literal["confirm", "dismiss"]
    expected_session_id: str | None = None


class RobotTick(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    yaw: float
    odom_age: float = Field(ge=0, le=1e9)
    scan_age: float = Field(ge=0, le=1e9)
    clearance: float | None = Field(default=None, ge=0, le=1e6)
    moving: bool = False
    fault: str | None = Field(default=None, max_length=300)


class Arm(BaseModel):
    armed: bool
    expected_session_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class SpeechAck(BaseModel):
    speech_id: str = Field(max_length=64)
    session_id: str = Field(max_length=64)
    status: Literal["claimed", "playing", "done", "failed"]
    owner: str = Field(min_length=8, max_length=80)
    error: str | None = Field(default=None, max_length=150)


class AudioPoll(BaseModel):
    ready: bool
    error: str | None = Field(default=None, max_length=150)


class AssistantQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=600)
    expected_session_id: str
    evidence_id: str | None = Field(default=None, max_length=64)
    use_cloud: bool = False


def decode_robot_frame(body: bytes):
    """Read the JPEG header before allocating its decoded pixel buffer."""
    try:
        with Image.open(io.BytesIO(body), formats=["JPEG"]) as header:
            width, height = header.size
            if min(width, height) < 8 or max(width, height) > 4096 or width * height > 3840 * 2160:
                raise ValueError("invalid_dimensions")
        decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, cv2.error):
        raise HTTPException(422, "无效或过大的 JPEG 画面") from None
    if decoded is None:
        raise HTTPException(422, "无效的 JPEG 画面")
    return decoded


def create_app(runtime: Runtime | None = None):
    runtime = runtime or Runtime()
    assistant = RefereeAssistant()

    @asynccontextmanager
    async def lifespan(app):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="SPARK Freeze · AI 裁判", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime

    def control_auth(x_control_token: str = Header(default="")):
        expected = os.getenv("SPARK_CONTROL_TOKEN", "")
        if expected and not hmac.compare_digest(expected, x_control_token):
            raise HTTPException(401, "控制口令不正确，请在设置中填写")

    def bridge_auth(x_bridge_token: str = Header(default="")):
        expected = os.getenv("SPARK_BRIDGE_TOKEN", "")
        if len(expected) < 16 or not hmac.compare_digest(expected, x_bridge_token):
            raise HTTPException(401, "机器人桥接令牌未配置或不正确（至少 16 字符）")

    def require_session(expected):
        if expected is not None and expected != runtime.engine.session_id:
            raise GameError("场次已经改变，请等待界面同步后重新操作")

    def validate_reset(command):
        engine = runtime.engine
        require_session(command.expected_session_id)
        if command.action == "reset" and engine.phase == "estop":
            raise GameError("急停已锁定，请明确点击“解除停止并重置”")
        if command.action == "clear_estop" and (engine.phase != "estop"
                or command.expected_session_id != engine.session_id
                or command.expected_revision != engine.revision):
            raise GameError("请先同步当前急停状态，再明确解除停止")

    @app.exception_handler(GameError)
    async def game_error(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/assistant/status")
    def assistant_status():return assistant.status()

    @app.post("/api/assistant/ask", dependencies=[Depends(control_auth)])
    def assistant_ask(payload: AssistantQuestion):
        with runtime.lock:
            require_session(payload.expected_session_id)
            try:context = assistant.context(runtime.engine.snapshot(), payload.evidence_id)
            except ValueError as error:raise GameError(str(error)) from error
        # Network is entirely outside the game lock. The reply names its source revision.
        return assistant.answer(payload.question, context, payload.use_cloud)

    @app.get("/api/state")
    def state():
        with runtime.lock:
            return runtime.engine.snapshot()

    @app.get("/api/frame.jpg")
    def frame():
        with runtime.lock:
            body = runtime.last_frame
        return Response(body, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def require_speech_ready():
        target = runtime.engine.config.speech_target
        if target != "off" and not runtime.speech_assets.clips:
            raise GameError(runtime.speech_assets.error)
        if target == "robot":
            if runtime.engine.mode != "ros":raise GameError("机器人播报需要 ROS 模式")
            if time.monotonic() - runtime.audio_time > 2 or not runtime.engine.robot.get("audio", {}).get("ready"):
                raise GameError("机器人音频尚未就绪，请检查桥接的 audio_enabled、声卡与缓存")

    @app.get("/api/speech/catalog")
    def speech_catalog():return runtime.speech_assets.status()

    @app.get("/api/speech/assets/{filename}")
    def speech_asset(filename: str):
        path = runtime.speech_assets.path(filename)
        if not path or not path.is_file():raise HTTPException(404, "语音资源不存在")
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    def accept_speech(payload, target):
        with runtime.lock:
            engine = runtime.engine
            if not engine.speech or engine.speech["target"] != target:
                raise GameError("播放端与当前播报不匹配")
            if payload.status == "done" and engine.speech_started is not None:
                clip = runtime.speech_assets.clips.get(engine.speech["clip"])
                if clip and engine.clock() - engine.speech_started < clip["duration"] - 0.25:
                    raise GameError("音频尚未完整播放，不能提前确认完成")
            engine.acknowledge_speech(payload.speech_id, payload.session_id, payload.status,
                                      target + ":" + payload.owner, payload.error)
            return {"ok": True}

    @app.post("/api/speech/ack", dependencies=[Depends(control_auth)])
    def speech_ack(payload: SpeechAck):return accept_speech(payload, "browser")

    @app.post("/api/robot/speech/ack", dependencies=[Depends(bridge_auth)])
    def robot_speech_ack(payload: SpeechAck):return accept_speech(payload, "robot")

    @app.post("/api/robot/speech/poll", dependencies=[Depends(bridge_auth)])
    def robot_speech_poll(payload: AudioPoll):
        with runtime.lock:
            runtime.audio_time = time.monotonic()
            runtime.engine.robot["audio"] = payload.model_dump()
            speech = runtime.engine.speech
            return {"speech": dict(speech) if speech and speech["target"] == "robot" else None}

    @app.post("/api/speech/test", dependencies=[Depends(control_auth)])
    def speech_test():
        with runtime.lock:
            engine = runtime.engine
            if engine.phase not in {"lobby", "finished"}:raise GameError("请在开场前测试语音")
            if engine.config.speech_target == "off":raise GameError("请先在比赛设置中选择播放端")
            require_speech_ready()
            engine.speech = dict(id=uuid.uuid4().hex, session_id=engine.session_id, clip="test",
                                 text="我是 SPARK 裁判。语音播放测试完成。", target=engine.config.speech_target,
                                 status="pending", owner=None, error=None, required=False)
            engine.speech_started = None
            engine.speech_deadline = engine.clock() + 20
            engine.log("speech", "已请求语音播放测试，请确认实际扬声器发声")
            return engine.snapshot()

    @app.post("/api/control", dependencies=[Depends(control_auth)])
    def control(command: Control):
        if command.action in {"reset", "clear_estop"}:
            with runtime.lock:
                validate_reset(command)
                ticket = runtime.prepare_archive()
            runtime._wait_report(ticket, required=True)
            with runtime.lock:
                runtime.verify_archive(ticket)
                validate_reset(command)
                runtime.engine.reset()
                runtime.demo_moves.clear()
                runtime.queue_report()
                return runtime.engine.snapshot()
        with runtime.lock:
            engine = runtime.engine
            action = command.action
            if action != "estop":
                require_session(command.expected_session_id)
            if action in {"resume", "declare_winner"} and (
                    command.expected_session_id != engine.session_id or command.expected_revision != engine.revision):
                raise GameError("比赛状态已经改变，请等待界面同步后重新确认操作")
            if action in {"start", "resume"}:
                runtime.check_camera_freshness()
                require_speech_ready()
            if action == "start":
                engine.start()
            elif action == "pause":
                engine.pause()
            elif action == "resume":
                engine.resume()
            elif action == "estop":
                if engine.phase == "estop":
                    # A fresh human stop cancels an earlier clear request,
                    # including one already waiting for its archive write.
                    # The engine's repeated watchdog stop remains idempotent.
                    engine.log("safety", "再次确认紧急停止，先前的解除请求已失效。")
                else:
                    engine.estop()
            elif action == "finish":
                engine.finish()
            elif action == "declare_winner":
                engine.declare_winner(command.player_id)
            elif action == "demo_move":
                if engine.mode != "demo" or engine.phase != "red":
                    raise GameError("请等到演示模式的红灯判罚阶段，再模拟偷动")
                player = engine.player(command.player_id if command.player_id is not None else 1)
                if player["status"] != "active":
                    raise GameError("请选择仍在参赛的玩家")
                runtime.demo_moves[player["id"]] = time.monotonic() + engine.config.hold_seconds + 0.8
            runtime.queue_report()
            return engine.snapshot()

    @app.post("/api/config", dependencies=[Depends(control_auth)])
    def configure(payload: dict):
        with runtime.lock:
            engine = runtime.engine
            if engine.phase not in {"lobby", "finished"}:
                raise GameError("请先结束或重置比赛，再修改设置")
            incoming = dict(payload)
            require_session(incoming.pop("expected_session_id", None))
            mode = incoming.pop("mode", engine.mode)
            if mode not in ("demo", "webcam", "ros"):
                raise HTTPException(422, "模式必须是 demo、webcam 或 ros")
            try:
                config = GameConfig.model_validate({**engine.config.model_dump(), **incoming})
            except ValidationError as exc:
                raise HTTPException(422, str(exc)) from exc
            if config.speech_target == "robot" and mode != "ros":
                raise HTTPException(422, "机器人播报只能用于 ROS 模式")
            if config.speech_target != "off" and not runtime.speech_assets.clips:
                raise GameError(runtime.speech_assets.error)
            ticket = runtime.prepare_archive()
        runtime._wait_report(ticket, required=True)
        with runtime.lock:
            runtime.verify_archive(ticket)
            engine.config = config
            engine.mode = mode
            engine.reset()
            runtime.changed_source()
            runtime.queue_report()
            return engine.snapshot()

    @app.post("/api/evidence/{evidence_id}/review", dependencies=[Depends(control_auth)])
    def review(evidence_id: str, command: Review):
        with runtime.lock:
            require_session(command.expected_session_id)
            runtime.engine.review(evidence_id, command.decision)
            runtime.queue_report()
            return runtime.engine.snapshot()

    @app.get("/api/evidence/{session_id}/{evidence_id}/{filename}")
    def evidence(session_id: str, evidence_id: str, filename: str):
        if not re.fullmatch("[a-f0-9]{32}", session_id) or not re.fullmatch("[a-f0-9]{32}", evidence_id) or filename not in {"before.jpg", "after.jpg"}:
            raise HTTPException(404, "证据不存在")
        path = runtime.data_dir / "sessions" / session_id / evidence_id / filename
        if not path.is_file():
            raise HTTPException(404, "证据图片不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/report")
    def report():
        with runtime.lock:
            snapshot = runtime.report_snapshot()
        return JSONResponse(snapshot, headers={
            "Content-Disposition": f'attachment; filename="spark-freeze-{snapshot["session_id"][:8]}.json"'})

    @app.post("/api/operator/heartbeat", dependencies=[Depends(control_auth)])
    def heartbeat():
        with runtime.lock:
            runtime.operator_time = time.monotonic()
        return {"ok": True}

    @app.post("/api/robot/tick", dependencies=[Depends(bridge_auth)])
    def robot_tick(payload: RobotTick):
        with runtime.lock:
            runtime.robot_time = time.monotonic()
            runtime.odom_age = payload.odom_age
            runtime.scan_age = payload.scan_age
            runtime.clearance = payload.clearance
            runtime.robot_fault = payload.fault
            runtime.engine.robot.update(yaw=payload.yaw, moving=payload.moving, connected=True,
                                        status=payload.fault or "桥接在线", clearance=payload.clearance,
                                        odom_age=payload.odom_age, scan_age=payload.scan_age, fault=payload.fault)
            if runtime.engine.robot["armed"]:
                fault = runtime.robot_unready()
                if fault:
                    runtime.engine.estop(fault)
            return runtime.engine.command()

    @app.post("/api/robot/frame", dependencies=[Depends(bridge_auth)])
    async def robot_frame(request: Request):
        received_at = time.monotonic()
        with runtime.lock:
            if runtime.engine.mode != "ros":
                raise HTTPException(409, "请先切换到 ROS 模式")
            generation = runtime.generation
            session_id = runtime.engine.session_id
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 2_000_000:
                raise HTTPException(413, "画面超过 2MB，请在桥接端缩小")
            chunks.append(chunk)
        decoded = await run_in_threadpool(decode_robot_frame, b"".join(chunks))
        with runtime.lock:
            if generation != runtime.generation or session_id != runtime.engine.session_id or runtime.engine.mode != "ros":
                raise HTTPException(409, "画面来源或场次已切换，已丢弃旧画面")
            if time.monotonic() - received_at > 1:
                raise HTTPException(422, "画面传输过慢，已丢弃过期画面")
            if received_at <= runtime.ros_frame_time:
                raise HTTPException(409, "更新的画面已接收，已丢弃乱序画面")
            runtime.ros_frame = decoded
            runtime.ros_frame_time = received_at
            runtime.ros_frame_seq += 1
        return {"ok": True}

    @app.post("/api/robot/arm", dependencies=[Depends(control_auth)])
    def arm(payload: Arm):
        with runtime.lock:
            engine = runtime.engine
            if not payload.armed:
                engine.robot["armed"] = False
                if engine.phase in RUNNING:
                    engine.estop("已手动停止机器人")
                else:
                    # Even an already-disarmed lobby must invalidate an enable
                    # request that was sent before this explicit stop action.
                    engine.log("robot", "已手动停用机器人，先前的启用请求已失效")
                return engine.snapshot()
            if (payload.expected_session_id != engine.session_id
                    or payload.expected_revision != engine.revision):
                raise GameError("机器人状态已经改变，请等待界面同步后重新确认启用")
            if engine.mode != "ros" or engine.phase != "lobby":
                raise GameError("只能在 ROS 模式等待开场时启用")
            if len(os.getenv("SPARK_CONTROL_TOKEN", "")) < 16 or len(os.getenv("SPARK_BRIDGE_TOKEN", "")) < 16:
                raise GameError("机器人控制需要分别配置至少 16 字符的控制口令和桥接令牌")
            fault = runtime.robot_unready()
            if fault:
                raise GameError(fault)
            engine.robot["armed"] = True
            engine.log("robot", "机器人转向已启用，比赛开始时记录正面朝向")
            return engine.snapshot()

    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        def no_frontend():
            return {"message": "请先在 frontend 运行 npm install && npm run build，或启动 Vite 开发服务", "docs": "/docs"}
    return app


# The old factory remains importable for historical regression tests. The default
# service no longer starts pose inference, match timers, or referee motion.
from .block_app import create_block_app

app = create_app() if os.getenv("SPARK_PROJECT") == "freeze" else create_block_app()
