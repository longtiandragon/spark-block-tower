"""Deterministic game rules. No camera, HTTP, or robot libraries in this module."""
from __future__ import annotations

import math
import random
import time
import uuid
from datetime import datetime
from typing import Callable, Literal

from .speech import PHASE_CLIPS, PHRASES

from pydantic import BaseModel, ConfigDict, Field, model_validator


PHASE_LABELS = {
    "lobby": "等待开场", "countdown": "准备开始", "turning_away": "裁判转身",
    "green": "绿灯 · 可以前进", "turning_back": "裁判回头", "settling": "红灯 · 保持静止",
    "red": "红灯 · 正在判罚", "paused": "比赛暂停", "finished": "本场结束", "estop": "已紧急停止",
}
RUNNING = {"countdown", "turning_away", "green", "turning_back", "settling", "red"}
# At the documented initial 0.15 rad/s, a half-turn alone needs >20.9 s.
# This is a fault timeout, not the independent bridge watchdog stop latency.
ROS_TURN_TIMEOUT = 45.0


class GameError(ValueError):
    pass


class GameConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    players: int = Field(default=3, ge=1, le=4)
    green_min: float = Field(default=3, ge=1, le=20)
    green_max: float = Field(default=6, ge=1, le=30)
    red_seconds: float = Field(default=5, ge=2, le=20)
    settle_seconds: float = Field(default=1.2, ge=0.6, le=5)
    game_seconds: float = Field(default=120, ge=20, le=900)
    motion_threshold: float = Field(default=0.045, ge=0.015, le=0.3)
    hold_seconds: float = Field(default=0.45, ge=0.2, le=2)
    auto_eliminate: bool = False
    camera_index: int = Field(default=0, ge=0, le=10)
    speech_target: Literal["off", "browser", "robot"] = "off"

    @model_validator(mode="after")
    def validate_range(self):
        if self.green_max < self.green_min:
            raise ValueError("绿灯最长时间不能小于最短时间")
        return self


class GameEngine:
    def __init__(self, config: GameConfig | None = None, *, clock: Callable = time.monotonic,
                 random_seconds: Callable = random.uniform):
        self.clock = clock
        self.random_seconds = random_seconds
        self.config = config or GameConfig()
        self.mode = "demo"
        self.revision = 0
        self.baseline_epoch = 0
        self.robot = {"connected": False, "armed": False, "yaw": 0.0, "status": "未连接"}
        self.camera = {"ready": False, "fps": 0.0, "width": 960, "height": 540,
                       "error": None, "model": "模拟演示", "last_frame_age": None}
        self.storage = {"error": None}
        self.reset()

    def reset(self):
        self.session_id = uuid.uuid4().hex
        self.phase = "lobby"
        self.speech = None
        self.round = 0
        self.deadline = None
        self.game_deadline = None
        self.saved_game_remaining = self.config.game_seconds
        self.message = "请按赛道站好；演示模式可直接开始体验。"
        self.players = [dict(id=i + 1, name=f"{i + 1:02d} 号玩家", status="active", visible=False,
                             motion=0.0, confidence=0.0) for i in range(self.config.players)]
        self.evidence = []
        self.events = []
        self.streaks = {}
        self.cooldown_until = {}
        self.last_observed = None
        self.face_yaw = 0.0
        self.command_id = getattr(self, "command_id", 0) + 1
        self.robot["armed"] = False
        self.invalidate_baseline()
        self.log("info", "新场次已创建。玩家编号对应固定赛道，请勿交换赛道。")

    def invalidate_baseline(self):
        self.baseline_epoch += 1
        self.baseline_started_at = self.clock()
        self.streaks.clear()
        self.last_observed = None

    def log(self, kind, text):
        self.events.append(dict(id=uuid.uuid4().hex[:12], time=datetime.now().strftime("%H:%M:%S"),
                                kind=kind, text=text))
        self.events = self.events[-1000:]
        self.revision += 1

    def transition(self, phase, seconds=None, message=None):
        self.phase = phase
        self.deadline = self.clock() + seconds if seconds is not None else None
        self.streaks.clear()
        self.last_observed = None
        self.command_id += 1
        if phase in {"settling", "red", "lobby"}:
            self.invalidate_baseline()
        self.message = message or PHASE_LABELS[phase]
        self.speech = None
        clip = PHASE_CLIPS.get(phase)
        if clip and self.config.speech_target != "off":
            self.speech = dict(id=uuid.uuid4().hex, session_id=self.session_id, clip=clip,
                               text=PHRASES[clip], target=self.config.speech_target,
                               status="pending", owner=None, error=None,
                               required=phase in {"countdown", "green", "turning_back"})
            self.speech_deadline = self.clock() + 20
            self.speech_started = None
        self.log("phase", self.message)

    def acknowledge_speech(self, speech_id, session_id, status, owner, error=None):
        speech = self.speech
        if not speech or speech["id"] != speech_id or session_id != self.session_id:
            raise GameError("播报已过期，场次或阶段已经改变")
        if speech["owner"] not in (None, owner):
            raise GameError("本条播报由另一个播放端领取")
        if speech["status"] in {"done", "failed"}:
            if speech["status"] == status:return
            raise GameError("播报已完成，拒绝旧状态")
        if status not in {"claimed", "playing", "done", "failed"}:
            raise GameError("无效播放状态")
        if status == "done" and (speech["status"] != "playing" or self.speech_started is None):
            raise GameError("必须先开始播放再确认完成")
        if speech["status"] == "playing" and status == "claimed":
            raise GameError("拒绝播放状态回退")
        speech["owner"] = owner
        if status == "playing" and self.speech_started is None:self.speech_started = self.clock()
        speech["status"] = status
        speech["error"] = str(error or "")[:150] if status == "failed" else None
        self.revision += 1
        if status == "failed" and speech["required"] and self.phase in RUNNING:
            self.pause("关键口令播放失败，比赛已暂停，请检查播放设备后恢复。")

    def start(self):
        if self.phase != "lobby":
            raise GameError("请先重置到等待开场，再开始比赛")
        if not self.camera["ready"]:
            raise GameError("画面尚未就绪，请检查摄像头和 AI 模型状态")
        if self.mode != "demo" and not any(p["visible"] for p in self.players):
            raise GameError("尚未检测到玩家，请进入赛道并露出上半身")
        if self.mode == "ros" and not self.robot["armed"]:
            raise GameError("请先连接并启用机器人；电脑摄像头模式不需要机器人")
        self.face_yaw = self.robot["yaw"]
        self.game_deadline = self.clock() + self.config.game_seconds
        self.transition("countdown", 3)

    def pause(self, reason="裁判暂停了比赛"):
        if self.phase not in RUNNING:
            raise GameError("当前没有进行中的比赛")
        if self.remaining_game() <= 0:
            self.finish("时间到，本场结束；待复核记录仍可处理。")
            return
        self.saved_game_remaining = max(0, self.game_deadline - self.clock())
        self.game_deadline = None
        self.transition("paused", message=reason)

    def resume(self):
        if self.phase != "paused":
            raise GameError("只有暂停中的比赛可以继续")
        if not self.camera["ready"]:
            raise GameError("画面未恢复，暂时不能继续")
        if self.mode == "ros" and not self.robot["armed"]:
            raise GameError("机器人已停止，请重置比赛后重新启用")
        self.game_deadline = self.clock() + self.saved_game_remaining
        if self.round == 0:
            # A pause during the initial countdown/turn-away must not skip
            # the first green phase and start judging a nonexistent round 0.
            self.transition("countdown", 3, "重新准备开场，等待首轮绿灯")
        else:
            self.transition("turning_back", 1 if self.mode != "ros" else ROS_TURN_TIMEOUT, "继续比赛，裁判先回到正面")

    def finish(self, reason="裁判结束了本场比赛"):
        if self.phase == "estop":
            raise GameError("紧急停止后请明确重置，不能直接结束或恢复")
        if self.phase in {"lobby", "finished"}:
            raise GameError("当前没有可结束的比赛")
        self.saved_game_remaining = self.remaining_game()
        self.game_deadline = None
        self.robot["armed"] = False
        self.transition("finished", message=reason)

    def estop(self, reason="已紧急停止。重置后才能重新开始。"):
        if self.phase == "estop":
            return
        self.saved_game_remaining = self.remaining_game()
        self.game_deadline = None
        self.robot["armed"] = False
        self.transition("estop", message=reason)

    def declare_winner(self, player_id):
        if self.phase not in RUNNING | {"paused"}:
            raise GameError("只能在比赛中确认到达终点")
        if self.remaining_game() <= 0:
            raise GameError("比赛时间已到，不能再确认获胜")
        player = self.player(player_id)
        if player["status"] != "active":
            raise GameError("该玩家已淘汰或有待复核判罚")
        player["status"] = "winner"
        self.finish(f"裁判确认 {player['name']} 到达终点，获胜！")

    def player(self, player_id):
        for player in self.players:
            if player["id"] == player_id:
                return player
        raise GameError("玩家编号不存在")

    def remaining_game(self):
        if self.game_deadline is None:
            return self.saved_game_remaining
        return max(0, self.game_deadline - self.clock())

    def tick(self):
        now = self.clock()
        if (self.speech and not self.speech["required"] and self.speech["status"] not in {"done", "failed"}
                and now >= self.speech_deadline):
            self.speech.update(status="failed", error="播放端未按时确认")
            self.revision += 1
        if self.phase not in RUNNING:
            return
        if self.remaining_game() <= 0:
            self.finish("时间到，本场结束；待复核记录仍可处理。")
            return
        if self.speech and self.speech["required"] and self.speech["status"] != "done":
            if now >= self.speech_deadline:
                self.pause("关键口令播放超时，比赛已暂停，请检查播放设备。")
            return
        if self.mode == "ros" and self.phase in {"turning_away", "turning_back"}:
            target = self.face_yaw + (math.pi if self.phase == "turning_away" else 0)
            delta = math.atan2(math.sin(target - self.robot["yaw"]), math.cos(target - self.robot["yaw"]))
            if abs(delta) > 0.06 or self.robot.get("moving", False):
                if self.deadline is not None and now >= self.deadline:
                    self.estop("机器人转向超时，已停止。请检查里程计和场地。")
                return
        elif self.deadline is None or now < self.deadline:
            return
        if self.phase == "countdown":
            self.transition("turning_away", 1 if self.mode != "ros" else ROS_TURN_TIMEOUT)
        elif self.phase == "turning_away":
            self.round += 1
            self.transition("green", self.random_seconds(self.config.green_min, self.config.green_max))
        elif self.phase == "green":
            self.transition("turning_back", 1 if self.mode != "ros" else ROS_TURN_TIMEOUT)
        elif self.phase == "turning_back":
            self.transition("settling", self.config.settle_seconds, "红灯！请保持静止，等待画面稳定。")
        elif self.phase == "settling":
            self.transition("red", self.config.red_seconds)
        elif self.phase == "red":
            self.transition("turning_away", 1 if self.mode != "ros" else ROS_TURN_TIMEOUT)

    def observe(self, observations, stable=True, *, finalize=True, observed_at=None, defer_evidence=False):
        """Return newly created evidence records; caller attaches real frame pairs."""
        now = self.clock()
        sample_time = now if observed_at is None else observed_at
        if observed_at is not None and (not math.isfinite(sample_time)
                or sample_time > now or sample_time < self.baseline_started_at
                or (self.last_observed is not None and sample_time <= self.last_observed)):
            self.streaks.clear()
            return []
        new_evidence = []
        previous_observed = self.last_observed
        self.last_observed = sample_time
        if previous_observed is None or sample_time - previous_observed > 0.6:
            self.streaks.clear()
        by_id = {o["player_id"]: o for o in observations}
        # Observation and timer threads may race at a deadline. Never judge
        # after the actual red/game deadline just because tick has not run.
        judging_interval = (self.phase == "red" and self.deadline is not None
                            and now < self.deadline and self.remaining_game() > 0)
        for player in self.players:
            pid = player["id"]
            obs = by_id.get(pid, {})
            player["visible"] = bool(obs.get("visible", False))
            confidence = float(obs.get("confidence", 0))
            player["confidence"] = round(confidence, 3) if math.isfinite(confidence) else 0.0
            score = float(obs.get("motion", 0))
            player["motion"] = round(score, 4) if math.isfinite(score) else 0.0
            player["ambiguous"] = bool(obs.get("ambiguous", False))
            player["reason"] = obs.get("reason", "")
            judge = (judging_interval and stable and player["visible"]
                     and not player["ambiguous"] and player["confidence"] >= 0.5
                     and player["status"] == "active" and sample_time >= self.cooldown_until.get(pid, 0))
            if not judge or not math.isfinite(score) or score < self.config.motion_threshold:
                self.streaks.pop(pid, None)
                continue
            start = self.streaks.setdefault(pid, sample_time)
            if sample_time - start < self.config.hold_seconds:
                continue
            evidence_id = uuid.uuid4().hex
            status = "confirmed" if self.config.auto_eliminate and not defer_evidence else "pending"
            item = dict(id=evidence_id, player_id=pid, round=self.round, score=round(score, 4),
                        duration_seconds=round(sample_time - start, 3),
                        status=status, created_at=datetime.now().isoformat(timespec="seconds"),
                        before_url=f"/api/evidence/{self.session_id}/{evidence_id}/before.jpg",
                        after_url=f"/api/evidence/{self.session_id}/{evidence_id}/after.jpg")
            self.evidence.append(item)
            player["status"] = "eliminated" if status == "confirmed" else "pending"
            self.streaks.pop(pid, None)
            self.log("violation", f"{player['name']} 持续移动：{'正在保存证据' if defer_evidence else '已自动淘汰' if status == 'confirmed' else '等待裁判复核'}")
            new_evidence.append(item)
        if finalize:
            self.finalize_observations()
        return new_evidence

    def finalize_observations(self):
        """Runtime calls this only after evidence storage has succeeded."""
        if (self.phase in RUNNING | {"paused"}
                and all(player["status"] == "eliminated" for player in self.players)):
            self.finish("所有玩家均已淘汰，本场结束，无获胜者。")

    def review(self, evidence_id, decision):
        if decision not in {"confirm", "dismiss"}:
            raise GameError("复核操作必须是 confirm 或 dismiss")
        evidence = next((e for e in self.evidence if e["id"] == evidence_id), None)
        if not evidence:
            raise GameError("记录不存在或已切换场次")
        if evidence.get("saving"):
            raise GameError("证据图片仍在保存，请等待保存完成后再复核")
        if evidence["status"] != "pending":
            raise GameError("这条记录已经处理过")
        player = self.player(evidence["player_id"])
        evidence["status"] = "confirmed" if decision == "confirm" else "dismissed"
        player["status"] = "eliminated" if decision == "confirm" else "active"
        self.cooldown_until[player["id"]] = self.clock() + 2
        self.streaks.pop(player["id"], None)
        if decision == "dismiss":
            self.invalidate_baseline()
            # All lanes currently share the reset epoch. Their evidence must
            # not carry a pre-reset movement streak across that new baseline.
            self.streaks.clear()
        self.log("review", f"{player['name']}：{'确认淘汰' if decision == 'confirm' else '撤销判罚，恢复参赛'}")
        self.finalize_observations()

    def command(self):
        action = "stop"
        target = self.face_yaw
        if (self.mode == "ros" and self.robot["armed"]
                and self.phase in {"turning_away", "turning_back"}
                and self.deadline is not None and self.clock() < self.deadline
                and self.remaining_game() > 0):
            action = "turn"
            if self.phase == "turning_away":
                target += math.pi
        return dict(command_id=self.command_id, action=action,
                    target_yaw=math.atan2(math.sin(target), math.cos(target)), max_speed=0.3, lease_seconds=0.7)

    def snapshot(self):
        return dict(phase=self.phase, phase_label=PHASE_LABELS[self.phase], mode=self.mode,
                    round=self.round, remaining=round(max(0, (self.deadline or self.clock()) - self.clock()), 1),
                    game_remaining=round(self.remaining_game(), 1), session_id=self.session_id, message=self.message,
                    camera=dict(self.camera), robot=dict(self.robot), storage=dict(self.storage),
                    speech=dict(self.speech) if self.speech else None, face_yaw=self.face_yaw,
                    players=[dict(p) for p in self.players],
                    events=[dict(e) for e in self.events[-80:]], evidence=[dict(e) for e in self.evidence],
                    config=self.config.model_dump(), revision=self.revision)
