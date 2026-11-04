import copy
import pytest

from spark_blocks_bridge.manipulation import GraspMonitor, calibrated_heights
from spark_blocks_bridge.safety import validate_config


def site():
    return {'limits': [[100, 300], [-150, 150], [-100, 300]],
            'sources': [{'support_surface_z_mm': -180}],
            'slots': {'1-1-%d' % i: {'support_surface_z_mm': -180, 'xy_mm': [200, 0]}
                      for i in range(3)}}


def model():
    return dict(verified=True, block_height_mm=100, tool_to_cup_z_mm=30,
                compression_mm=1, clearance_mm=40)


def test_heights_include_tool_offset_compression_and_each_tower_layer():
    config = site()
    original = copy.deepcopy(config)
    result = calibrated_heights(config, model())
    assert config == original
    assert result['sources'][0]['pickup_z'] == -51
    assert result['sources'][0]['approach_z'] == -11
    assert [result['slots']['1-1-%d' % i]['down'][2] for i in range(3)] == [-51, 49, 149]
    assert result['slots']['1-1-2']['above'] == [200, 0, 189]


def test_unreachable_upper_layer_is_rejected_without_clamping():
    config = site()
    config['limits'][2][1] = 150
    with pytest.raises(ValueError, match='workspace'):
        calibrated_heights(config, model())


@pytest.mark.parametrize('change', [dict(verified=False), dict(compression_mm=3),
                                   dict(block_height_mm=float('nan')), dict(clearance_mm=0)])
def test_unmeasured_or_invalid_geometry_rejected(change):
    measured = model()
    measured.update(change)
    with pytest.raises(ValueError):
        calibrated_heights(site(), measured)


def test_existing_height_is_never_silently_overwritten():
    config = site()
    config['sources'][0]['pickup_z'] = -36
    with pytest.raises(ValueError, match='conflicts'):
        calibrated_heights(config, model())


def test_sensor_mode_cannot_enable_with_unverified_feedback():
    config = dict(arm_feedback=dict(physically_verified=True, topic='/measured_tcp', max_age_s=.3, tolerance_mm=.8, timeout_s=20), commissioned=True, arm_stop_verified=True, arm_stop_service='/stop',
                  verification_mode='sensor', grasp_monitor={'topic': '/pump_commands',
                  'physically_verified': False, 'max_age_s': .3, 'acquire_timeout_s': 2})
    with pytest.raises(ValueError, match='verified measured'):
        validate_config(config)


def sample(seq, stamp, holding=True, kind='vacuum_switch'):
    return dict(seq=seq, stamp=stamp, holding=holding, kind=kind)


def test_fresh_stable_measurements_required_and_loss_stops_monitor():
    now = [10.]
    monitor = GraspMonitor(clock=lambda: now[0])
    started = monitor.begin(100)
    for i in range(3):
        now[0] = 10+i*.05
        assert monitor.update(sample(i, 100+i*.05), 100+i*.05)
    assert monitor.confirmed(started) and monitor.fault() is None
    assert monitor.update(sample(3, 100.15, False), 100.15)
    assert not monitor.confirmed(started) and 'lost' in monitor.fault()


def test_old_queued_or_duplicate_feedback_cannot_confirm_new_grasp():
    now = [10.]
    monitor = GraspMonitor(clock=lambda: now[0])
    monitor.begin(100)
    assert not monitor.update(sample(1, 99.9), 100)
    assert monitor.update(sample(2, 100), 100)
    assert not monitor.update(sample(2, 100), 100)
    assert not monitor.confirmed()
    for i in range(3, 6):
        now[0] += .05
        monitor.update(sample(i, 100+i*.01), 100+i*.01)
    assert monitor.confirmed()
    now[0] += .31
    assert not monitor.confirmed() and 'stale' in monitor.fault()


@pytest.mark.parametrize('bad', [None, {}, dict(seq=True, stamp=100, holding=True, kind='vacuum_switch'),
    sample(1, 100, kind='pump_command'), sample(1, 100, kind='motor_current'),
    sample(1, 101), sample(1, 99), sample(1, 0), sample(1, float('nan')), sample(1, 100, holding=1)])
def test_malformed_commands_current_and_unfresh_samples_are_not_grip(bad):
    monitor = GraspMonitor(clock=lambda: 10)
    assert not monitor.update(bad, 100)
    assert not monitor.confirmed()
