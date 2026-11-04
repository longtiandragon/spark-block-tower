"""Blue-block task planner. No actuator calls: the ROS adapter owns motion.

All methods are called under the API's lock. Commands are leased and correlated;
sequence completion never counts as physical pickup/placement confirmation.
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BlockError(ValueError):
    pass


class Cell(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    row: int = Field(ge=0, le=2, strict=True)
    col: int = Field(ge=0, le=2, strict=True)
    layer: int = Field(default=0, ge=0, le=2, strict=True)

    @property
    def key(self):
        return f"{self.row}-{self.col}-{self.layer}"


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="蓝色积木排列", min_length=1, max_length=40)
    cells: list[Cell] = Field(min_length=1, max_length=9)
    color: Literal["blue"] = "blue"

    @model_validator(mode="after")
    def supported(self):
        keys = {c.key for c in self.cells}
        if len(keys) != len(self.cells):
            raise ValueError("同一格同一层不能重复放置")
        for c in self.cells:
            if c.layer and f"{c.row}-{c.col}-{c.layer-1}" not in keys:
                raise ValueError("上层积木必须有下层支撑")
        return self


class Telemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    bridge_id: str = Field(min_length=8, max_length=64)
    operation_mode: Literal["mobile", "stationary"] = "mobile"
    dry_run: bool = True
    ready: bool = False
    stopped: bool = True
    fault: str | None = Field(default=None, max_length=200)
    pose: list[float] | None = Field(default=None, min_length=3, max_length=3)
    camera_age: float = Field(default=1e6, ge=0)
    scan_age: float = Field(default=1e6, ge=0)
    odom_age: float = Field(default=1e6, ge=0)
    clearance: float = Field(default=0, ge=0)
    slots: list[str] = Field(default_factory=list, max_length=27)
    sources: int = Field(default=0, ge=0, le=9)
    stop_ack: str | None = Field(default=None, max_length=32)


STAGES = ["navigate_pick", "pick", "verify_pick", "navigate_home", "place", "verify_place"]
LABELS = dict(idle="准备搭建", navigate_pick="前往抓取接近点", pick="吸附并抬起积木",
              verify_pick="检查抓取结果", navigate_home="运回起始搭建区", place="放置到目标格位",
              verify_place="检查放置结果", awaiting_pick="等待确认积木已吸稳",
              awaiting_place="等待确认实际放置", paused="已暂停，等待现场检查",
              estop="停止已锁定", completed="布局搭建完成")


def layout_from_text(text: str) -> Layout:
    """Small explicit grammar; ambiguous language never becomes robot commands."""
    if any(w in text for w in ("红", "绿", "黄", "取消", "不要", "别", "不搭")):
        raise BlockError("当前仅支持蓝色积木；请明确描述要生成的布局，如“三块排成一行”。")
    counts = re.findall(r"([0-9]+|[零一二两三四五六七八九十百千万]+)\s*(?:块|层|个(?=\s*(?:蓝色)?积木))", text)
    chars = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if any(not value.isdigit() and value not in chars for value in counts):
        raise BlockError("当前最多九块、三层，请明确填写支持的积木数量。")
    numbers = [int(value) if value.isdigit() else chars[value] for value in counts]
    if any(n < 1 or n > 9 for n in numbers) or len(set(numbers)) > 1:
        raise BlockError("积木数量超出范围或与层数冲突，请重新描述。")
    n = numbers[0] if numbers else 3
    if any(w in text for w in ("塔", "叠", "层")):
        if n > 3:
            raise BlockError("当前最多三层，每层都需要单独标定。")
        cells = [Cell(row=1, col=1, layer=i) for i in range(n)]
        name = f"{n} 层蓝色积木塔"
    elif re.search(r"\bL\b|L[形型]", text, re.I) or "拐角" in text:
        if counts and n != 3:
            raise BlockError("拐角模板使用三块积木。")
        cells = [Cell(row=0, col=0), Cell(row=1, col=0), Cell(row=1, col=1)]
        name = "蓝色积木拐角"
    elif any(w in text for w in ("行", "排", "列", "直线")):
        if n > 3:
            raise BlockError("一行最多三格；更多积木请在布局板上编辑。")
        cells = [Cell(row=i if "列" in text else 1, col=1 if "列" in text else i) for i in range(n)]
        name = "蓝色积木直线"
    else:
        raise BlockError("可说“三块排成一行”“搭两层塔”或“搭一个拐角”；草案需要保存后才能运行。")
    return Layout(name=name, cells=cells)


class BlockEngine:
    def __init__(self, data_dir: Path, clock=time.monotonic):
        self.clock = clock
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.layout = Layout(name="三层蓝色积木塔", cells=[Cell(row=1, col=1, layer=i) for i in range(3)])
        try:
            self.layout = Layout.model_validate_json((data_dir / "layout.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.session = uuid.uuid4().hex
        self.revision = 0
        self.mode = "demo"
        self.phase = "idle"
        self.index = 0
        self.stage = 0
        self.plan = []
        self.placed = []
        self.events = []
        self.checks = []
        self.storage_error = None
        self.command = None
        self.command_started = 0.0
        self.bridge = None
        self.bridge_time = -1e9
        self.owner = None
        self.operator_time = -1e9
        self.home = None
        self.home_owner = None
        self.stop_id = None
        self.evidence = None
        self.armed = False
        self.last_step = clock()

    def write(self, filename, value):
        path = self.data_dir / filename
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            raise BlockError("记录保存失败，请检查数据目录权限与可用空间。") from None

    def log(self, message):
        self.revision += 1
        self.events.append(dict(id=self.revision, time=time.strftime("%H:%M:%S"), message=message))
        self.events = self.events[-150:]
        self.storage_error = None
        try:
            self.write("last-run.json", self.snapshot())
        except BlockError as error:
            self.phase, self.command, self.armed = "estop", None, False
            if self.mode == "robot":
                self.stop_id = uuid.uuid4().hex
            self.storage_error = str(error)
            self.revision += 1
            self.events.append(dict(id=self.revision, time=time.strftime("%H:%M:%S"), message=str(error)))
            raise

    def check_revision(self, revision):
        if revision != self.revision:
            raise BlockError("任务已变化，请等待同步后重新操作。")

    def save_layout(self, layout):
        if self.phase != "idle":
            raise BlockError("请先结束并重置当前任务，再保存布局。")
        self.write("layout.json", layout.model_dump())
        self.layout = layout
        self.log("已保存目标布局：" + layout.name)

    def ready_error(self):
        b = self.bridge
        if not b or self.clock() - self.bridge_time > 0.8:
            return "机器人桥接离线或心跳过期"
        if b.fault or not b.ready:
            return b.fault or "机器人标定、停止接口或驱动尚未就绪"
        if b.dry_run:
            return "桥接为 dry-run，不允许执行真实搭建"
        if max(b.camera_age, b.scan_age, b.odom_age) + self.clock() - self.bridge_time > 0.5:
            return "相机、雷达或定位数据过期"
        if b.clearance < 0.45:
            return "周围空间不足（雷达距离小于 0.45 m）"
        if self.clock() - self.operator_time > 2:
            return "操作台心跳过期"
        return None

    def control(self, action, revision, mode=None):
        if action not in {"estop", "pause"}:
            self.check_revision(revision)
        if action in {"pause", "estop"}:
            self.halt("estop" if action == "estop" or self.phase == "estop" else "paused", "操作员请求停止；保持吸盘状态，现场检查负载。")
        elif action in {"reset", "recover"}:
            if self.phase not in {"idle", "completed", "paused", "estop"}:
                raise BlockError("请先暂停任务。")
            if self.phase in {"paused", "estop"} and action != "recover":
                raise BlockError("请先现场确认设备停止、负载已安全处理，并使用恢复按钮。")
            if self.mode == "robot" and (not self.bridge or not self.bridge.stopped or self.clock()-self.bridge_time > 0.8):
                raise BlockError("需要机器人新鲜的停止确认，不能仅依据网页状态复位。")
            if self.mode == "robot" and (self.bridge.odom_age + self.clock()-self.bridge_time > .5
                    or (self.stop_id is not None and self.bridge.stop_ack != self.stop_id)):
                raise BlockError("机器人尚未确认本次停止请求，或停止观测已过期。")
            # Archive BEFORE changing the session or clearing in-memory evidence.
            # A failed archive leaves the completed/stopped run fully reviewable.
            self.write(f"run-{self.session}.json", self.snapshot())
            self.phase, self.command, self.armed = "idle", None, False
            self.plan, self.placed, self.evidence = [], [], None
            self.checks = []
            self.index, self.stage = 0, 0
            self.session = uuid.uuid4().hex
            self.stop_id = None
            self.log("已重置；下次任务需重新开始，现场积木须与空搭建区初始条件一致。")
        elif action == "mode":
            if self.phase != "idle" or mode not in {"demo", "robot"}:
                raise BlockError("仅在准备阶段切换模式。")
            self.mode = mode
            self.home = None
            self.home_owner = None
            self.log("切换为" + ("合成演示" if mode == "demo" else "真实机器人模式"))
        elif action == "record_home":
            if self.phase != "idle" or self.mode != "robot":
                raise BlockError("请在真实机器人准备阶段记录起点。")
            if not self.bridge or self.clock()-self.bridge_time > 0.8 or not self.bridge.stopped or self.bridge.odom_age + self.clock()-self.bridge_time > 0.5 or (self.bridge.operation_mode == "mobile" and self.bridge.pose is None):
                raise BlockError("需要新鲜定位且机器人已停止。")
            self.home = list(self.bridge.pose) if self.bridge.operation_mode == "mobile" else None
            self.home_owner = self.bridge.bridge_id
            self.log("已确认固定工作区；机器人及相机须保持固定。" if self.bridge.operation_mode == "stationary" else "已记录当前 map 位姿为起始搭建区。")
        elif action == "start":
            if self.phase != "idle":
                raise BlockError("请先重置任务。")
            if self.mode == "robot":
                error = self.ready_error()
                if error:
                    raise BlockError(error)
                if (self.bridge.operation_mode == "mobile" and not self.home) or self.home_owner != self.bridge.bridge_id:
                    raise BlockError("先在搭建区记录机器人起始位置。")
                if not self.bridge.stopped:
                    raise BlockError("机器人尚未停止。")
                if len(self.layout.cells) > self.bridge.sources or any(c.key not in self.bridge.slots for c in self.layout.cells):
                    raise BlockError("目标超出已标定的抓取站点或放置格位。")
                self.owner = self.bridge.bridge_id
            self.plan = [dict(cell=c.model_dump(), key=c.key, source=i) for i, c in enumerate(sorted(self.layout.cells, key=lambda c: (c.layer, c.row, c.col)))]
            self.placed, self.index, self.stage = [], 0, 0
            self.armed = self.mode == "robot"
            self.issue()
        elif action in {"confirm_pick", "confirm_place"}:
            expected = "awaiting_pick" if action == "confirm_pick" else "awaiting_place"
            if self.phase != expected:
                raise BlockError("当前没有对应的待确认步骤。")
            if self.mode == "robot":
                error = self.ready_error()
                if error:
                    raise BlockError(error)
                if not self.bridge.stopped:
                    raise BlockError("请等待底盘和机械臂停止，再确认实物结果。")
            self.checks[-1]["verification"] = "operator_confirmed"
            self.log("人工确认：" + ("积木已吸稳并抬起" if action == "confirm_pick" else "积木实际落点及支撑正确"))
            self.advance("operator_confirmed")
        elif action == "reject":
            if self.phase not in {"awaiting_pick", "awaiting_place"}:
                raise BlockError("当前没有待复核结果。")
            self.halt("paused", "实际结果不符合预期，已停止；现场处理后重新规划。")
        else:
            raise BlockError("未知操作。")

    def halt(self, phase, reason):
        self.phase, self.command, self.armed = phase, None, False
        if self.mode == "robot":
            self.stop_id = uuid.uuid4().hex
        self.log(reason)

    def issue(self):
        self.phase = STAGES[self.stage]
        self.evidence = None
        self.command_started = self.clock()
        self.last_step = self.clock()
        self.command = dict(id=uuid.uuid4().hex, session=self.session, action=self.phase,
                            source=self.plan[self.index]["source"], slot=self.plan[self.index]["key"], home=self.home,
                            operation_mode=self.bridge.operation_mode if self.mode == "robot" else "mobile")
        self.log(f"第 {self.index+1}/{len(self.plan)} 块：{LABELS[self.phase]}")

    def advance(self, verification=None):
        if self.stage == 5:
            self.placed.append({**self.plan[self.index], "verification": verification})
            self.index += 1
            self.stage = 0
            if self.index == len(self.plan):
                self.phase, self.command, self.armed = "completed", None, False
                if self.mode == "robot":
                    self.stop_id = uuid.uuid4().hex
                self.log("合成演示完成。" if self.mode == "demo" else "所有积木已逐块经人工确认完成。")
                return
        else:
            self.stage += 1
        self.issue()

    def tick(self):
        if self.armed:
            error = self.ready_error()
            if error:
                self.halt("estop", error + "；停止已锁定，不自动恢复。")
        if self.command and self.mode == "robot" and self.clock()-self.command_started > 150:
            self.halt("estop", "动作超时，停止已锁定。")
        if self.command and self.mode == "demo" and self.clock()-self.last_step >= 0.7:
            self.advance("simulated" if self.stage == 5 else None)

    def telemetry(self, payload):
        # Detect a gap before replacing the old heartbeat with a fresh one.
        self.tick()
        if self.bridge and payload.operation_mode != self.bridge.operation_mode:
            self.home, self.home_owner = None, None
            if self.armed:
                self.halt("estop", "工作模式已更换，必须重新检查现场。")
        if self.armed and payload.bridge_id != self.owner:
            self.halt("estop", "桥接进程已更换，必须重新检查现场。")
        if self.home_owner is not None and payload.bridge_id != self.home_owner:
            self.home, self.home_owner = None, None
        self.bridge, self.bridge_time = payload, self.clock()
        self.tick()
        return dict(session=self.session, command=copy.deepcopy(self.command) if self.armed else None,
                    lease_seconds=0.65, armed=self.armed, stop_id=self.stop_id)

    def result(self, command_id, bridge_id, ok, observation, frame_id=None):
        self.tick()
        if not self.command or command_id != self.command["id"] or bridge_id != self.owner or not self.armed:
            raise BlockError("结果已过期或不属于当前动作。")
        if not ok:
            self.halt("paused", "动作失败：" + observation)
            return
        if self.stage in {2, 5}:
            self.evidence = dict(command_id=command_id, frame_id=frame_id, observation=observation, verification="pending_operator")
            self.checks.append(dict(self.evidence, block_index=self.index, stage=self.phase,
                                    image_url=f"/api/blocks/evidence/{command_id}.jpg"))
            self.phase = "awaiting_pick" if self.stage == 2 else "awaiting_place"
            self.command = None
            self.log("已收到视觉观察，请结合现场确认；动作完成不代表吸附或放置成功。")
        else:
            self.advance()

    def snapshot(self):
        return copy.deepcopy(dict(session=self.session, revision=self.revision, mode=self.mode,
            phase=self.phase, phase_label=({'navigate_pick': '准备固定取料区', 'navigate_home': '移至搭建准备姿态'}.get(self.phase, LABELS[self.phase])
                if self.bridge and self.mode == 'robot' and self.bridge.operation_mode == 'stationary' else LABELS[self.phase]), layout=self.layout.model_dump(),
            plan=self.plan, placed=self.placed, index=self.index, stage=self.stage, armed=self.armed,
            home=self.home, workspace_recorded=self.home_owner is not None, events=self.events, checks=self.checks, evidence=self.evidence, command=self.command,
            storage_error=self.storage_error,
            stop_id=self.stop_id,
            bridge=self.bridge.model_dump() if self.bridge else None,
            bridge_fresh=self.clock()-self.bridge_time <= 0.8,
            readiness=self.ready_error() if self.mode == "robot" else None))
