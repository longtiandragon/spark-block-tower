import pytest

from spark_freeze.engine import GameConfig, GameEngine


@pytest.mark.parametrize('auto', [False, True])
def test_one_hundred_sessions_do_not_share_players_evidence_or_timing(auto):
    clock = [100.0]
    engine = GameEngine(GameConfig(players=2, auto_eliminate=auto), clock=lambda: clock[0],
                        random_seconds=lambda a, b: a)
    sessions = set()
    previous_command = -1
    for game in range(100):
        engine.reset()
        assert engine.session_id not in sessions
        sessions.add(engine.session_id)
        assert not engine.evidence and not engine.streaks and not engine.cooldown_until
        assert engine.command_id > previous_command
        engine.camera['ready'] = True
        engine.start()
        while engine.phase != 'red':
            clock[0] = engine.deadline + .001
            engine.tick()
        if game % 2 == 0:
            engine.pause()
            remaining = engine.remaining_game()
            clock[0] += 30
            assert engine.remaining_game() == remaining
            engine.resume()
            while engine.phase != 'red':
                clock[0] = engine.deadline + .001
                engine.tick()
        # Four distinct captured poses spanning .6s, always within red.
        for _ in range(4):
            clock[0] += .2
            engine.observe([dict(player_id=1, visible=True, confidence=.9, motion=.1)],
                           observed_at=clock[0])
        assert len(engine.evidence) == 1
        if not auto:
            engine.review(engine.evidence[0]['id'], 'confirm')
        assert engine.players[0]['status'] == 'eliminated'
        assert engine.players[1]['status'] == 'active'
        assert engine.phase == 'red'
        engine.declare_winner(2)
        assert engine.phase == 'finished'
        assert engine.command()['action'] == 'stop'
        assert engine.players[1]['status'] == 'winner'
        previous_command = engine.command_id
