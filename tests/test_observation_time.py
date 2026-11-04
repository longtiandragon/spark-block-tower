import pytest

from spark_freeze.engine import GameEngine


def test_runtime_does_not_turn_inference_latency_into_movement_duration(tmp_path, monkeypatch):
    import numpy as np
    from spark_freeze.runtime import Runtime
    import spark_freeze.runtime as runtime_module
    runtime = Runtime(tmp_path)
    engine, clock = setup_game()
    runtime.engine = engine
    monkeypatch.setattr(runtime_module.time, 'monotonic', lambda: clock[0])
    for capture, completed in [(100.01, 100.02), (100.06, 100.32), (100.11, 100.62)]:
        clock[0] = completed
        runtime._publish(np.zeros((80, 80, 3), np.uint8), observation(), True, 0, {}, captured_at=capture)
    assert not engine.evidence, 'Only 0.1 seconds elapsed between captured poses'


def setup_game():
    clock = [100.0]
    engine = GameEngine(clock=lambda: clock[0])
    engine.game_deadline = 200
    engine.transition('red', 10)
    return engine, clock


def observation():
    return [dict(player_id=1, visible=True, confidence=.9, motion=.1)]


def test_slow_inference_does_not_stretch_a_short_movement():
    engine, clock = setup_game()
    for capture, completed in [(100.01, 100.02), (100.06, 100.32), (100.11, 100.62)]:
        clock[0] = completed
        assert engine.observe(observation(), observed_at=capture) == []
    assert engine.players[0]['status'] == 'active'


def test_capture_gap_resets_streak_even_if_results_arrive_close_together():
    engine, clock = setup_game()
    for capture, completed in [(100.01, 100.5), (100.81, 100.82), (101.01, 101.02)]:
        clock[0] = completed
        assert engine.observe(observation(), observed_at=capture) == []
    assert engine.streaks[1] == 100.81


def test_real_sustained_movement_with_variable_latency_still_judged():
    engine, clock = setup_game()
    for capture, completed in [(100.01, 100.3), (100.26, 100.31)]:
        clock[0] = completed
        assert engine.observe(observation(), observed_at=capture) == []
    clock[0] = 100.7
    records = engine.observe(observation(), observed_at=100.51)
    assert len(records) == 1
    assert records[0]['duration_seconds'] == .5


@pytest.mark.parametrize('stamp', [99.0, 101.0, float('nan'), float('inf')])
def test_invalid_or_before_baseline_time_does_not_judge(stamp):
    engine, clock = setup_game()
    clock[0] = 100.5
    assert engine.observe(observation(), observed_at=stamp) == []
    assert not engine.streaks


def test_repeated_frame_never_accumulates_a_violation():
    engine, clock = setup_game()
    for completed in [100.1, 100.3, 100.6, 100.8]:
        clock[0] = completed
        assert engine.observe(observation(), observed_at=100.01) == []
    assert not engine.evidence


def test_cooldown_uses_capture_time_not_late_completion():
    engine, clock = setup_game()
    engine.cooldown_until[1] = 101
    clock[0] = 101.2
    engine.observe(observation(), observed_at=100.9)
    assert not engine.streaks


@pytest.mark.parametrize('stamp', [100.01, 100.1, 102.0, float('nan')])
def test_runtime_rejects_duplicate_old_or_invalid_frame_without_replacing_preview(tmp_path, monkeypatch, stamp):
    import numpy as np
    import spark_freeze.runtime as runtime_module
    from spark_freeze.runtime import Runtime
    runtime = Runtime(tmp_path)
    engine, clock = setup_game()
    runtime.engine = engine
    clock[0] = 101
    monkeypatch.setattr(runtime_module.time, 'monotonic', lambda: clock[0])
    runtime.frame_time = 100.1
    old_preview = runtime.last_frame
    runtime._publish(np.zeros((80, 80, 3), np.uint8), observation(), True, 0, {}, captured_at=stamp)
    assert runtime.frame_time == 100.1
    assert runtime.last_frame == old_preview
    assert not engine.streaks
