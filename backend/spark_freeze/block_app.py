"""Default SPARK Blocks application; isolated from the historical referee runtime."""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import threading
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
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, ConfigDict
from starlette.concurrency import run_in_threadpool

from .assistant import RefereeAssistant
from .blocks import BlockEngine, BlockError, Layout, Telemetry, layout_from_text
from .block_vision import detect_blue
from .fixed_tower import FixedTower, logical_plan
from .tower_simulation import scene as simulation_scene

ROOT = Path(__file__).resolve().parents[2]


class Action(BaseModel):
    action: Literal["start", "pause", "estop", "reset", "recover", "mode", "record_home", "confirm_pick", "confirm_place", "reject"]
    revision: int = Field(ge=0)
    session: str | None = Field(default=None, max_length=32)
    mode: Literal["demo", "robot"] | None = None


class SavedLayout(BaseModel):
    revision: int = Field(ge=0)
    session: str = Field(min_length=32, max_length=32)
    layout: Layout


class Question(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    use_cloud: bool = False


class Result(BaseModel):
    command_id: str = Field(min_length=32, max_length=32, pattern="^[a-f0-9]+$")
    bridge_id: str = Field(min_length=8, max_length=64)
    ok: bool
    observation: str = Field(default="动作序列完成", max_length=500)
    frame_id: str | None = Field(default=None, min_length=32, max_length=32, pattern="^[a-f0-9]+$")


class FixedAction(BaseModel):
    action: Literal['start', 'feed', 'support', 'stop', 'reset']
    session: str = Field(min_length=32, max_length=32)
    revision: int = Field(ge=0)
    operator: str = Field(min_length=32, max_length=32)
    checked: bool = False
    levels: Literal[1,2,3] = 3
    structure: Literal['single','tower2','tower3','pyramid3'] | None = None
    automatic: bool = False


class FixedAgent(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: str = Field(min_length=32, max_length=32)
    ready: bool
    stopped: bool
    fault: str | None = Field(default=None, max_length=300)
    progress: str = Field(default='', max_length=200)
    structures: list[Literal['single','tower2','tower3','pyramid3']] = Field(default_factory=lambda:['single','tower2','tower3'], max_length=4)
    calibration_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    profile_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    sources: list[list[float]] = Field(default_factory=list,max_length=3)
    source_age: float = Field(default=1e9,ge=0)
    source_note: str = Field(default='',max_length=200)
    staging: dict | None = None
    slot_version: str | None = Field(default=None,max_length=80)
    slots_ready: bool = False


class FixedResult(BaseModel):
    id: str = Field(min_length=32, max_length=32)
    command_id: str = Field(min_length=32, max_length=32)
    ok: bool
    details: dict = Field(default_factory=dict)


def decode_frame(body):
    try:
        with Image.open(io.BytesIO(body), formats=["JPEG"]) as header:
            width, height = header.size
            if min(width, height) < 8 or max(width, height) > 1920 or width*height > 1920*1080:
                raise ValueError("dimensions")
        frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("decode")
        return frame
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, cv2.error):
        raise HTTPException(422, "无效或过大的 JPEG") from None


def create_block_app(data_dir=None):
    engine = BlockEngine(Path(data_dir or os.getenv("SPARK_BLOCKS_DATA_DIR", str(ROOT / "data" / "blocks"))))
    lock = threading.RLock()
    fixed = FixedTower(engine.data_dir, enabled=os.getenv('SPARK_FIXED_TOWER') == '1')
    fixed_frame = dict(body=b'', received=-1e9, sequence=0)
    assistant = RefereeAssistant()
    camera = dict(body=b"", boxes=[], received=-1e9, sequence=0, source_sequence=0,
                  session=None, bridge_id=None)
    inspection = {}
    last_inspection = dict(bridge_id=None, source_sequence=0)

    def camera_current():
        return (camera['session'] == engine.session and engine.bridge is not None
                and camera['bridge_id'] == engine.bridge.bridge_id)

    def verify_frame_command(command_id, bridge_id):
        if (not engine.armed or not engine.command or engine.command['id'] != command_id
                or engine.command['action'] not in {'verify_pick', 'verify_place'}
                or bridge_id != engine.owner):
            raise BlockError("检查画面不属于当前待检查动作。")

    @asynccontextmanager
    async def lifespan(app):
        async def watchdog():
            while True:
                with lock:
                    try:
                        engine.tick()
                        fixed.tick()
                    except BlockError:
                        # Persistence failures latch the engine before raising. Keep
                        # the watchdog alive so subsequent state/stop checks still run.
                        pass
                await asyncio.sleep(.1)
        task = asyncio.create_task(watchdog())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            with lock:
                if fixed.busy():
                    fixed.stop('网页服务停止；机器人租约到期后停止并保持吸力。')
                if engine.armed:
                    try:
                        engine.halt("estop", "服务正在停止；机器人租约到期后停止，不自动续动。")
                    except BlockError:
                        pass

    app = FastAPI(title="SPARK Blocks · 蓝色积木搭建", lifespan=lifespan)
    app.state.engine, app.state.lock = engine, lock
    app.state.fixed_tower = fixed

    def control_auth(x_control_token: str = Header(default="")):
        expected = os.getenv("SPARK_CONTROL_TOKEN", "")
        if expected and not hmac.compare_digest(expected, x_control_token):
            raise HTTPException(401, "控制口令不正确")

    def bridge_auth(x_bridge_token: str = Header(default="")):
        expected = os.getenv("SPARK_BRIDGE_TOKEN", "")
        if len(expected) < 16 or not hmac.compare_digest(expected, x_bridge_token):
            raise HTTPException(401, "机器人桥接令牌未配置或不正确")

    @app.exception_handler(BlockError)
    async def conflict(request, error):
        return JSONResponse(status_code=409, content=dict(detail=str(error)))

    @app.get("/api/health")
    def health():
        result = dict(ok=True, project="spark-blocks", default_color="blue")
        result['simulation_default']=os.getenv('SPARK_TOWER_PRESENTATION')=='1'
        if fixed.enabled:
            result['fixed_tower'] = True
        return result

    @app.get("/api/blocks/state")
    def state():
        with lock:
            engine.tick()
            result = engine.snapshot()
            result["camera"] = dict(boxes=camera["boxes"] if camera_current() else [], fresh=camera_current() and time.monotonic()-camera["received"] < 1,
                                    sequence=camera["sequence"])
            return result

    @app.post("/api/blocks/control", dependencies=[Depends(control_auth)])
    def control(payload: Action):
        with lock:
            if payload.action == 'start' and engine.mode == 'robot' and fixed.busy():
                raise BlockError('固定三层塔正在执行，不能同时启动其他实机任务。')
            if payload.action not in {"estop", "pause"} and payload.session != engine.session:
                raise BlockError("任务会话已改变，请同步当前任务后重新操作。")
            if payload.action == "start" and engine.mode == "robot":
                if min(len(os.getenv("SPARK_CONTROL_TOKEN", "")), len(os.getenv("SPARK_BRIDGE_TOKEN", ""))) < 16:
                    raise BlockError("实机需要配置两个独立的至少 16 字符令牌。")
                if os.getenv("SPARK_CONTROL_TOKEN") == os.getenv("SPARK_BRIDGE_TOKEN"):
                    raise BlockError("控制口令与桥接令牌必须不同。")
            engine.control(payload.action, payload.revision, payload.mode)
            return engine.snapshot()

    @app.put("/api/blocks/layout", dependencies=[Depends(control_auth)])
    def layout(payload: SavedLayout):
        with lock:
            if payload.session != engine.session:
                raise BlockError("任务会话已改变，请同步当前任务后重新保存。")
            engine.check_revision(payload.revision)
            engine.save_layout(payload.layout)
            return engine.snapshot()

    @app.post("/api/blocks/operator/heartbeat", dependencies=[Depends(control_auth)])
    def heartbeat():
        with lock:
            # Check the previous lease before a returning tab can renew it.
            engine.tick()
            engine.operator_time = engine.clock()
        return dict(ok=True)

    @app.post("/api/blocks/propose", dependencies=[Depends(control_auth)])
    def propose(payload: Question):
        return dict(layout=layout_from_text(payload.text).model_dump(), source="local_grammar", executed=False)

    @app.post("/api/blocks/assistant", dependencies=[Depends(control_auth)])
    def ask(payload: Question):
        with lock:
            context = engine.snapshot()
        # Only bounded task facts leave the machine. No frames, tokens, or local paths.
        facts = {k: context[k] for k in ("mode", "phase_label", "layout", "placed", "index", "evidence", "readiness")}
        local = (f"{'合成演示' if context['mode']=='demo' else '实机任务'}：{context['phase_label']}。"
                 f"已确认 {len(context['placed'])}/{len(context['layout']['cells'])} 块。"
                 + (f"当前阻碍：{context['readiness']}。" if context['readiness'] else "")
                 + "蓝色识别只说明可见区域，不能证明吸盘吸稳或塔体稳定。每层使用独立标定高度。")
        result = dict(answer=local, source="local_facts", warning=None, revision=context["revision"], session=context["session"])
        if not payload.use_cloud:
            return result
        if not assistant.status()["configured"]:
            return {**result, "warning": "云端模型未配置，已返回本地任务摘要。"}
        if not assistant.busy.acquire(blocking=False):
            return {**result, "warning": "助手忙，已返回本地任务摘要。"}
        try:
            reply = assistant.request_model([
                dict(role="system", content="你是 SPARK 搭积木只读助手。仅依据给出的任务事实回答；不能操作机械臂、修改布局或启动任务。"
                     "区分合成演示、动作序列完成和现场确认；没有图像输入，不推断抓取成功、塔体稳定、坐标或硬件验收。中文简短回答。"),
                dict(role="user", content=json.dumps(dict(question=payload.text, facts=facts), ensure_ascii=False))])
            if reply.get("tool_calls") or not isinstance(reply.get("content"), str) or not reply["content"].strip():
                raise ValueError("Invalid response")
            result.update(answer=reply["content"][:4000], source="cloud_ai")
        except Exception:
            result["warning"] = "云端请求失败，已返回本地任务摘要。"
        finally:
            assistant.busy.release()
        return result

    @app.get("/api/blocks/assistant/status")
    def ai_status():
        return dict(configured=assistant.status()["configured"], read_only=True)

    @app.post("/api/blocks/robot/tick", dependencies=[Depends(bridge_auth)])
    def robot_tick(payload: Telemetry):
        with lock:
            return engine.telemetry(payload)

    @app.post("/api/blocks/robot/result", dependencies=[Depends(bridge_auth)])
    def robot_result(payload: Result):
        with lock:
            engine.tick()
            if not engine.command or payload.command_id != engine.command["id"] or payload.bridge_id != engine.owner or not engine.armed:
                raise BlockError("结果已过期或不属于当前动作。")
            if engine.command and engine.command["action"] in {"verify_pick", "verify_place"} and payload.ok:
                if (not payload.frame_id or inspection.get('frame_id') != payload.frame_id
                        or inspection.get('command_id') != payload.command_id
                        or inspection.get('session') != engine.session
                        or inspection.get('bridge_id') != payload.bridge_id
                        or time.monotonic()-inspection.get('received', -1e9) > 1):
                    raise BlockError("需要本次动作专属的新检查画面，不能使用预览帧或其他动作的图片。")
                image_path = engine.data_dir / f"{payload.command_id}.jpg"
                try:
                    image_path.write_bytes(inspection["body"])
                except OSError:
                    engine.halt("estop", "检查图片保存失败，任务已停止。")
                    raise BlockError("检查图片保存失败，请检查磁盘。") from None
            engine.result(payload.command_id, payload.bridge_id, payload.ok, payload.observation, payload.frame_id)
            inspection.clear()
            return dict(ok=True)

    @app.post("/api/blocks/robot/frame", dependencies=[Depends(bridge_auth)])
    async def robot_frame(request: Request, x_bridge_id: str = Header(default=""),
                          x_task_session: str = Header(default=""),
                          x_frame_sequence: int = Header(ge=1),
                          x_command_id: str | None = Header(default=None)):
        received = time.monotonic()
        with lock:
            if not engine.bridge or x_bridge_id != engine.bridge.bridge_id or (engine.armed and x_bridge_id != engine.owner):
                raise BlockError("请先建立桥接心跳；拒绝其他进程的画面。")
            session = engine.session
            if x_task_session != session:
                raise BlockError("画面属于旧任务会话，已丢弃。")
            if x_command_id:
                verify_frame_command(x_command_id, x_bridge_id)
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 2_000_000:
                raise HTTPException(413, "画面超过 2 MB")
            chunks.append(chunk)
        frame = await run_in_threadpool(decode_frame, b"".join(chunks))
        preview, boxes = await run_in_threadpool(detect_blue, frame)
        ok, jpeg = cv2.imencode(".jpg", preview)
        if not ok:
            raise HTTPException(422, "画面编码失败")
        with lock:
            if session != engine.session or not engine.bridge or x_bridge_id != engine.bridge.bridge_id:
                raise BlockError("任务或桥接已切换，已丢弃旧画面。")
            if time.monotonic()-received > 1:
                raise BlockError("画面过期或乱序，已丢弃。")
            frame_id = None
            if x_command_id:
                verify_frame_command(x_command_id, x_bridge_id)
                if last_inspection['bridge_id'] == x_bridge_id and x_frame_sequence <= last_inspection['source_sequence']:
                    raise BlockError("不能复用此前检查的相机帧。")
                frame_id = uuid.uuid4().hex
                inspection.clear()
                inspection.update(frame_id=frame_id, command_id=x_command_id, session=session,
                                  bridge_id=x_bridge_id, body=jpeg.tobytes(), received=received)
                last_inspection.update(bridge_id=x_bridge_id, source_sequence=x_frame_sequence)
            newer = not camera_current() or x_frame_sequence > camera['source_sequence']
            if not newer and not x_command_id:
                raise BlockError("相机源帧序号重复或倒退，已丢弃。")
            if newer:
                camera.update(body=jpeg.tobytes(), boxes=boxes, received=received, sequence=camera["sequence"]+1,
                              source_sequence=x_frame_sequence, session=session, bridge_id=x_bridge_id)
        return dict(ok=True, boxes=len(boxes), frame_id=frame_id)

    @app.get("/api/blocks/frame.jpg")
    def frame():
        with lock:
            body = camera["body"] if camera_current() else None
        if not body:
            raise HTTPException(404, "等待机器人相机画面")
        return Response(body, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/blocks/evidence/{command_id}.jpg")
    def evidence(command_id: str):
        import re
        if not re.fullmatch("[a-f0-9]{32}", command_id):
            raise HTTPException(404)
        path = engine.data_dir / f"{command_id}.jpg"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/blocks/report")
    def report():
        with lock:
            body = engine.snapshot()
        return JSONResponse(body, headers={"Content-Disposition": 'attachment; filename="spark-blocks-report.json"'})

    @app.get('/api/fixed-tower/state')
    def fixed_state():
        with lock:
            fixed.tick()
            return {**fixed.snapshot(), 'camera_fresh': time.monotonic()-fixed_frame['received'] < 1,
                    'frame_sequence': fixed_frame['sequence']}

    @app.get('/api/fixed-tower/simulation-scene')
    def fixed_simulation_scene():
        with lock:
            body=fixed_frame['body'] if time.monotonic()-fixed_frame['received']<1 else None
        if body:
            try:return simulation_scene(body,'实时相机识别')
            except ValueError:pass
        reference=fixed.path/'simulation-reference.jpg'
        if reference.exists():
            try:return simulation_scene(reference.read_bytes(),'现场识别快照')
            except ValueError:pass
        raise HTTPException(409,'等待三个可辨认的蓝色积木顶面')

    @app.get('/api/fixed-tower/plan/{structure}')
    def fixed_plan(structure: str):
        plan=logical_plan(structure)  # Whitelist before constructing any path.
        with lock:
            agent=fixed.agent
            current=bool(agent and fixed.clock()-fixed.agent_at<=1)
            plan['motion_enabled']=bool(current and agent.get('ready') and structure in agent.get('structures',[]))
            plan['ik_audit']=None
            path=fixed.path/'ik'/(structure+'.json')
            try:
                if path.stat().st_size>150_000:
                    raise ValueError('Audit too large')
                report=json.loads(path.read_text(encoding='utf8'))
                if (report.get('structure')!=structure or report.get('motion_sent') is not False
                        or report.get('source')!='robot_firmware_readonly_ik'
                        or type(report.get('passed')) is not bool
                        or type(report.get('checked_points')) is not int or report['checked_points']<1):
                    raise ValueError('Invalid audit')
                matches=bool(current and agent.get('calibration_sha256')==report.get('calibration_sha256')
                             and agent.get('profile_sha256')==report.get('profile_sha256')
                             and report.get('calibration_sha256') and report.get('profile_sha256'))
                plan['ik_audit']={k:report.get(k) for k in ('passed','checked_points','failure_count','inspected_at')}
                plan['ik_audit'].update(matches_current=matches,collision_verified=False,physical_verified=False)
                geometry=report.get('payload_geometry',{})
                plan['ik_audit']['payload_geometry_passed']=geometry.get('passed') is True
                plan['ik_audit']['arm_links_checked']=False
            except (OSError,ValueError,TypeError):
                pass
            return plan

    @app.post('/api/fixed-tower/heartbeat', dependencies=[Depends(control_auth)])
    def fixed_heartbeat(payload: FixedAction):
        with lock:
            fixed.heartbeat(payload.operator)
        return dict(ok=True)

    @app.post('/api/fixed-tower/control', dependencies=[Depends(control_auth)])
    def fixed_control(payload: FixedAction):
        with lock:
            if payload.action != 'stop':
                a, b = os.getenv('SPARK_CONTROL_TOKEN', ''), os.getenv('SPARK_BRIDGE_TOKEN', '')
                if min(len(a),len(b)) < 16 or a == b:
                    raise BlockError('实机需要两个独立的控制和执行端令牌。')
            if payload.action == 'start' and engine.armed:
                raise BlockError('其他实机任务正在执行。')
            fixed.control(payload.action, payload.session, payload.revision, payload.operator, payload.checked, payload.levels, payload.structure,payload.automatic)
            return fixed.snapshot()

    @app.post('/api/fixed-tower/robot/tick', dependencies=[Depends(bridge_auth)])
    def fixed_tick(payload: FixedAgent):
        with lock:
            return fixed.poll(payload.model_dump())

    @app.post('/api/fixed-tower/robot/result', dependencies=[Depends(bridge_auth)])
    def fixed_result(payload: FixedResult):
        with lock:
            fixed.result(payload.id, payload.command_id, payload.ok, payload.details)
        return dict(ok=True)

    @app.post('/api/fixed-tower/robot/frame', dependencies=[Depends(bridge_auth)])
    async def fixed_robot_frame(request: Request, x_bridge_id: str = Header(default='')):
        received = time.monotonic()
        with lock:
            if not fixed.agent or fixed.agent['id'] != x_bridge_id or time.monotonic()-fixed.agent_at > 1:
                raise BlockError('固定塔画面执行端不匹配或离线。')
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 2_000_000:
                raise HTTPException(413, '画面超过 2 MB')
            chunks.append(chunk)
        await run_in_threadpool(decode_frame, b''.join(chunks))
        with lock:
            if time.monotonic()-received > .8 or not fixed.agent or fixed.agent['id'] != x_bridge_id:
                raise BlockError('固定塔画面过期。')
            fixed_frame.update(body=b''.join(chunks), received=received, sequence=fixed_frame['sequence']+1)
        return dict(ok=True)

    @app.get('/api/fixed-tower/frame.jpg')
    def fixed_image():
        with lock:
            if time.monotonic()-fixed_frame['received'] > 1 or not fixed_frame['body']:
                raise HTTPException(404, '等待新鲜相机画面')
            return Response(fixed_frame['body'], media_type='image/jpeg', headers={'Cache-Control':'no-store'})

    @app.get('/api/fixed-tower/report')
    def fixed_report():
        with lock:
            return JSONResponse(fixed.snapshot(), headers={'Content-Disposition':'attachment; filename="fixed-tower-report.json"'})

    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app
