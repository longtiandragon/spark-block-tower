import math

import pytest
from pydantic import ValidationError

from spark_freeze.engine import GameConfig, GameEngine, GameError


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def game():
    clock = Clock()
    engine = GameEngine(clock=clock, random_seconds=lambda a, b: a)
    engine.camera["ready"] = True
    return engine, clock


def red(game):
    engine, clock = game
    engine.start()
    for elapsed in (3, 1, 3, 1, 1.2):
        clock.advance(elapsed + 0.001)
        engine.tick()
    assert engine.phase == "red"


def observe(engine, motion=.1, **kwargs):
    return engine.observe([dict(player_id=1, visible=True, confidence=.9,
                               motion=motion, ambiguous=False, **kwargs)])


def test_full_cycle_and_no_penalty_in_green(game):
    engine, clock = game
    engine.start()
    clock.advance(3)
    engine.tick()
    assert engine.phase == "turning_away"
    clock.advance(1)
    engine.tick()
    assert engine.phase == "green"
    for _ in range(8):
        clock.advance(.1)
        assert observe(engine) == []
    assert engine.players[0]["status"] == "active"


def test_sustained_movement_creates_one_reviewable_record(game):
    engine, clock = game
    red(game)
    for _ in range(7):
        observe(engine)
        clock.advance(.1)
    assert len(engine.evidence) == 1
    assert engine.players[0]["status"] == "pending"
    engine.review(engine.evidence[0]["id"], "confirm")
    assert engine.players[0]["status"] == "eliminated"
    with pytest.raises(GameError):
        engine.review(engine.evidence[0]["id"], "dismiss")


def test_frame_gap_does_not_count_as_sustained_motion(game):
    engine, clock = game
    red(game)
    observe(engine)
    clock.advance(2)
    observe(engine)
    assert not engine.evidence


def test_camera_shake_and_occlusion_clear_streak(game):
    engine, clock = game
    red(game)
    for _ in range(4):
        observe(engine)
        clock.advance(.1)
    engine.observe([dict(player_id=1, visible=True, confidence=.9, motion=.5, ambiguous=False)], stable=False)
    clock.advance(.1)
    observe(engine)
    assert not engine.evidence
    clock.advance(.3)
    engine.observe([dict(player_id=1, visible=True, confidence=.9, motion=.5, ambiguous=True)])
    clock.advance(.2)
    observe(engine)
    assert not engine.evidence


def test_pause_freezes_game_time_resume_has_grace(game):
    engine, clock = game
    red(game)
    engine.pause()
    remaining = engine.remaining_game()
    clock.advance(99)
    engine.tick()
    assert engine.remaining_game() == remaining
    engine.resume()
    assert engine.phase == "turning_back"
    assert observe(engine) == []


def test_estop_is_latched_until_reset(game):
    engine, clock = game
    engine.start()
    engine.estop()
    clock.advance(10)
    engine.tick()
    assert engine.command()["action"] == "stop"
    for method in (engine.start, engine.resume, engine.finish):
        with pytest.raises(GameError):
            method()
    engine.reset()
    assert engine.phase == "lobby"
    assert not engine.robot["armed"]


def test_rejected_penalty_resets_baseline_and_has_cooldown(game):
    engine, clock = game
    red(game)
    for _ in range(7):
        observe(engine)
        clock.advance(.1)
    epoch = engine.baseline_epoch
    engine.review(engine.evidence[0]["id"], "dismiss")
    assert engine.baseline_epoch > epoch
    assert engine.players[0]["status"] == "active"
    for _ in range(10):
        observe(engine)
        clock.advance(.1)
    assert len(engine.evidence) == 1


def test_ros_turn_requires_actual_arrival_and_times_out(game):
    engine, clock = game
    engine.mode = "ros"
    engine.players[0]["visible"] = True
    engine.robot.update(armed=True, yaw=math.pi - .1)
    engine.start()
    clock.advance(3)
    engine.tick()
    assert engine.command()["action"] == "turn"
    clock.advance(2)
    engine.tick()
    assert engine.phase == "turning_away"
    engine.robot["yaw"] = -.1
    engine.tick()
    assert engine.phase == "green"
    clock.advance(3)
    engine.tick()
    clock.advance(engine.deadline - clock() + 1)
    engine.tick()
    assert engine.phase == "estop"


def test_ros_half_turn_allows_documented_initial_low_speed(game):
    engine, clock = game
    engine.mode = "ros"
    engine.players[0]["visible"] = True
    engine.robot.update(armed=True, yaw=0.0)
    engine.start()
    clock.advance(3)
    engine.tick()
    # 0.15 rad/s is documented for first physical validation. Even ideal
    # motion takes >20 seconds, before proportional slowdown near the target.
    clock.advance(math.pi / .15)
    engine.robot.update(yaw=math.pi - .1, moving=True)
    engine.tick()
    assert engine.phase == "turning_away"
    clock.advance(4)
    engine.robot.update(yaw=math.pi, moving=False)
    engine.tick()
    assert engine.phase == "green"


@pytest.mark.parametrize("expired", ["turn", "game"])
def test_expired_ros_turn_cannot_renew_a_command_before_timer_tick(game, expired):
    engine, clock = game
    engine.mode = "ros"
    engine.players[0]["visible"] = True
    engine.robot.update(armed=True, yaw=0.0)
    engine.start()
    clock.advance(3)
    engine.tick()
    assert engine.command()["action"] == "turn"
    cutoff = engine.deadline if expired == "turn" else engine.game_deadline
    clock.advance(cutoff - clock())
    assert engine.command()["action"] == "stop"


def test_invalid_timing_and_nan_rejected():
    with pytest.raises(ValidationError):
        GameConfig(green_min=10, green_max=2)
    with pytest.raises(ValidationError):
        GameConfig(motion_threshold=float("nan"))


def test_finish_does_not_invent_a_winner(game):
    engine, clock = game
    engine.start()
    clock.advance(121)
    engine.tick()
    assert engine.phase == "finished"
    assert not any(p["status"] == "winner" for p in engine.players)


def test_winner_requires_active_player(game):
    engine, clock = game
    engine.start()
    engine.players[0]["status"] = "pending"
    with pytest.raises(GameError):
        engine.declare_winner(1)
    engine.declare_winner(2)
    assert engine.players[1]["status"] == "winner"
    assert engine.phase == "finished"


def test_reset_does_not_reuse_robot_command_numbers(game):
    engine, _ = game
    engine.start()
    engine.estop()
    previous = engine.command_id
    engine.reset()
    assert engine.command_id > previous


@pytest.mark.parametrize("expired", ["red", "game"])
def test_expired_interval_cannot_create_penalty_before_timer_tick(game, expired):
    engine, clock = game
    red(game)
    # Hold begins 0.4 s before the cutoff. A late frame would previously
    # complete the required 0.45 s streak while phase still said red.
    cutoff = clock() + .4
    if expired == "red":
        engine.deadline = cutoff
    else:
        engine.game_deadline = cutoff
    observe(engine)
    clock.advance(.5)
    assert observe(engine) == []
    assert not engine.evidence
    assert not engine.streaks


def test_pausing_after_time_limit_finishes_instead_of_freezing_zero_seconds(game):
    engine, clock = game
    engine.start()
    clock.advance(engine.config.game_seconds)
    engine.pause()
    assert engine.phase == "finished"
    assert engine.remaining_game() == 0


def test_expired_game_cannot_be_awarded_before_timer_tick(game):
    engine, clock = game
    engine.start()
    clock.advance(engine.config.game_seconds)
    with pytest.raises(GameError, match="时间已到"):
        engine.declare_winner(1)
    assert not any(p["status"] == "winner" for p in engine.players)


def test_first_countdown_pause_resume_does_not_skip_green(game):
    engine, clock = game
    engine.start()
    clock.advance(1)
    engine.pause()
    engine.resume()
    assert engine.phase == "countdown"
    clock.advance(3)
    engine.tick()
    clock.advance(1)
    engine.tick()
    assert engine.phase == "green"
    assert engine.round == 1


def test_confirmation_keeps_other_players_baseline_and_streak(game):
    engine, clock = game
    red(game)
    for _ in range(7):
        observe(engine)
        clock.advance(.1)
    epoch = engine.baseline_epoch
    engine.streaks[2] = clock()
    engine.review(engine.evidence[0]["id"], "confirm")
    assert engine.baseline_epoch == epoch
    assert 2 in engine.streaks


def test_dismissal_clears_all_streaks_with_shared_baseline_reset(game):
    engine, clock = game
    red(game)
    for _ in range(7):
        observe(engine)
        clock.advance(.1)
    engine.streaks[2] = clock()
    engine.review(engine.evidence[0]["id"], "dismiss")
    assert engine.streaks == {}


@pytest.mark.parametrize("auto", [False, True])
def test_last_confirmed_elimination_finishes_without_inventing_winner(game, auto):
    engine, clock = game
    engine.config = GameConfig(players=1, auto_eliminate=auto)
    engine.reset()
    red(game)
    for _ in range(7):
        observe(engine)
        clock.advance(.1)
    if not auto:
        assert engine.phase == "red"  # Pending evidence can still be dismissed.
        engine.review(engine.evidence[0]["id"], "confirm")
    assert engine.phase == "finished"
    assert engine.players[0]["status"] == "eliminated"
    assert not engine.robot["armed"]


def test_nonfinite_confidence_is_not_a_penalty_or_invalid_json(game):
    engine, clock = game
    red(game)
    for _ in range(7):
        engine.observe([dict(player_id=1, visible=True, confidence=float("inf"), motion=.1)])
        clock.advance(.1)
    assert engine.players[0]["confidence"] == 0
    assert not engine.evidence


def test_repeated_estop_does_not_reset_the_stop_revision(game):
    engine, clock = game
    engine.start()
    engine.estop()
    stopped = engine.snapshot()
    clock.advance(.1)
    engine.estop("第二次停止请求")
    assert engine.revision == stopped["revision"]
    assert engine.message == stopped["message"]
