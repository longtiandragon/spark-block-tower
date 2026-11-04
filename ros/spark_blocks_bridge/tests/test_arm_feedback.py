import pytest
from spark_blocks_bridge.arm_feedback import ArmFeedback


def sample(seq, **kwargs):
    return dict(protocol=2, driver_id='test-driver', kind='measured_tcp', seq=seq, stamp=100+seq*.05, xyz_mm=[200., 0., 20.],
                moving=False, powered=True, attached=[True]*3, **kwargs)


def test_arrival_needs_three_new_measured_samples_after_command():
    now = [10.]
    m = ArmFeedback(clock=lambda: now[0])
    for i in range(1, 4):
        m.update(sample(i), 100+i*.05)
    m.begin([200, 0, 20], 100.15)
    assert not m.reached()
    for i in range(4, 7):
        now[0] += .05
        m.update(sample(i), 100+i*.05)
        assert m.reached() == (i == 6)
    now[0] += .31
    assert not m.reached() and not m.stopped()


@pytest.mark.parametrize('change', [dict(kind='commanded_tcp'), dict(xyz_mm=[float('nan'), 0, 20]),
    dict(moving=0), dict(powered=False), dict(attached=[1, 1, 1]), dict(seq=True), dict(stamp=99)])
def test_invalid_or_cached_feedback_cannot_advance(change):
    m = ArmFeedback()
    m.begin([200, 0, 20], 100)
    data = sample(1)
    data.update(change)
    assert not m.update(data, 100.05)
    assert not m.fresh() and not m.reached()


def test_duplicate_moving_or_wrong_target_resets_arrival():
    m = ArmFeedback()
    m.begin([200, 0, 20], 100)
    assert m.update(sample(1), 100.05)
    assert not m.update(sample(1), 100.05)
    for i in range(2, 5):
        data = sample(i)
        data['moving'] = True
        assert m.update(data, data['stamp'])
    assert not m.reached()
    for i in range(5, 8):
        data = sample(i)
        data['xyz_mm'][2] = 25
        assert m.update(data, data['stamp'])
    assert not m.reached()
    assert not m.stopped(after=101)


def test_old_command_or_restarted_driver_cannot_complete_new_motion():
    m = ArmFeedback()
    m.begin([200, 0, 20], 100, 'new-action')
    for i in range(1, 4):
        data = sample(i)
        data['completed_id'] = 'old-action'
        assert m.update(data, data['stamp'])
    assert not m.reached()
    data = sample(4)
    data.update(driver_id='replacement-driver', completed_id='new-action')
    assert not m.update(data, data['stamp'])
    assert not m.fresh()


def test_pump_output_ack_is_not_position_or_suction_confirmation():
    m = ArmFeedback()
    data = sample(1)
    data.update(completed_id='pump-1', pump_applied=True)
    m.update(data, data['stamp'])
    assert m.applied('pump-1', True)
    assert not m.applied('other', True) and not m.applied('pump-1', False)


def test_old_executor_protocol_cannot_enable_motion():
    m = ArmFeedback()
    data = sample(1)
    data.pop('protocol')
    assert not m.update(data, data['stamp'])
    assert not m.available()
