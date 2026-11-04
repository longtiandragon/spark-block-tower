import copy

import pytest

from spark_blocks_bridge.safety import LeaseGate, pose, validate_config


def config():
    return dict(arm_feedback=dict(physically_verified=True, topic='/measured_tcp', max_age_s=.3, tolerance_mm=.8, timeout_s=20), commissioned=True, arm_stop_verified=True, arm_stop_service='/tested_stop',
        limits=[[100, 300], [-150, 150], [-60, 200]], rest=[120, 0, 50], carry=[200, 0, 150],
        image_size=[640, 480], pixel_to_arm=[.5, 100, .5, -150],
        dock_xy_tolerance_m=.01, dock_yaw_tolerance_rad=.02,
        sources=[dict(map_pose=[1, 1, 0], roi=[0, 0, 1, 1], approach_z=50, pickup_z=-20)],
        slots={'1-1-0': dict(above=[200, 0, 100], down=[200, 0, -10], transfer=[], retreat=[])})


def test_site_requires_measured_complete_inputs():
    assert validate_config(config())
    for key in ['limits', 'pixel_to_arm', 'rest', 'carry', 'sources', 'slots', 'arm_stop_service', 'dock_xy_tolerance_m']:
        bad = config()
        bad.pop(key)
        with pytest.raises(ValueError):
            validate_config(bad)


@pytest.mark.parametrize('offset', [[26, 0], [0, float('nan')], [True, 0], [1], '5,0'])
def test_pickup_correction_rejects_unbounded_or_invalid_offsets(offset):
    c = config()
    c['sources'][0]['pickup_xy_offset_mm'] = offset
    with pytest.raises(ValueError, match='pickup_xy_offset_mm'):
        validate_config(c)


@pytest.mark.parametrize('change', [
    {'commissioned': False}, {'arm_stop_verified': False}, {'carry': [200, 0, 900]},
    {'pixel_to_arm': [float('nan'), 0, 1, 0]}, {'dock_xy_tolerance_m': .5},
    {'sources': [dict(map_pose=[1, 0, 0], roi=[1, 1, 0, 0], approach_z=10, pickup_z=20)]},
])
def test_unsafe_calibration_rejected(change):
    c = config()
    c.update(change)
    with pytest.raises(ValueError):
        validate_config(c)


def test_vertical_approach_and_explicit_transfer_required():
    c = config()
    c['slots']['1-1-0']['above'][0] += 1
    with pytest.raises(ValueError):
        validate_config(c)
    c = config()
    c['slots']['1-1-0'].pop('transfer')
    with pytest.raises(ValueError):
        validate_config(c)


def response(armed=True, session='one', command='cmd1'):
    return dict(session=session, armed=armed, command={'id': command} if armed and command else None, lease_seconds=.65)


def test_latency_counts_from_request_send_not_receipt():
    now = [100.0]
    g = LeaseGate(lambda: now[0])
    g.accept(response(False), 100)
    now[0] = 100.5
    assert g.accept(response(), 100)
    assert g.valid('cmd1')
    now[0] = 100.66
    assert not g.valid()
    assert not g.accept(response(), 100)
    assert g.latched


def test_latch_never_rearms_from_old_session_or_late_response():
    g = LeaseGate(lambda: 100)
    assert not g.accept(response(), 100)  # reboot cannot start an armed task
    g.accept(response(False), 100)
    assert g.accept(response(), 100)
    g.stop()
    assert not g.accept(response(), 100)
    g.accept(response(False), 100)
    assert not g.accept(response(), 100)
    g.accept(response(False, session='two'), 100)
    assert g.accept(response(session='two'), 100)
    assert not g.valid('old-command')


def test_waiting_review_keeps_lease_without_permitting_old_motion():
    g = LeaseGate(lambda: 100)
    g.accept(response(False), 100)
    g.accept(response(), 100)
    g.accept(response(command=None), 100)
    assert g.valid() and not g.valid('cmd1')


def test_renewal_after_expiry_cannot_beat_watchdog():
    now = [100.0]
    g = LeaseGate(lambda: now[0])
    g.accept(response(False), now[0])
    assert g.accept(response(), now[0])
    now[0] += .66
    # Request is new and fast, but the PREVIOUS lease has already expired.
    assert not g.accept(response(), now[0])
    assert g.latched and not g.valid()


def test_malformed_command_cannot_partially_renew_an_active_lease():
    now = [100.0]
    g = LeaseGate(lambda: now[0])
    g.accept(response(False), now[0])
    g.accept(response(), now[0])
    now[0] += .5
    broken = response()
    broken['command'] = {'action': 'pick'}  # missing id
    assert g.accept(broken, now[0]) is False
    assert g.latched and not g.valid('cmd1')


@pytest.mark.parametrize('height', [-10, -20])
def test_tower_layers_cannot_reuse_or_reverse_release_height(height):
    c = config()
    c['slots']['1-1-1'] = dict(above=[200, 0, 100], down=[200, 0, height], transfer=[], retreat=[])
    with pytest.raises(ValueError, match='height'):
        validate_config(c)


def test_camera_dimensions_must_fit_backend_upload_limit():
    c = config()
    c['image_size'] = [1920, 1920]
    with pytest.raises(ValueError, match='image_size'):
        validate_config(c)


@pytest.mark.parametrize('broken', [
    None, [], {}, {'session': 'one', 'armed': 'false'},
    {'session': 'one', 'armed': True, 'lease_seconds': .65},
    {'session': 'one', 'armed': True, 'command': [], 'lease_seconds': .65},
    {'session': 'one', 'armed': True, 'command': {'id': 'next', 'session': 'other'}, 'lease_seconds': .65},
    {'session': 'one', 'armed': True, 'command': None, 'lease_seconds': float('nan')},
    {'session': 'one', 'armed': True, 'command': None, 'lease_seconds': .65, 'stop_id': 'stop'},
])
def test_invalid_backend_envelopes_fail_closed_without_throwing(broken):
    g = LeaseGate(lambda: 100)
    g.accept(response(False), 100)
    g.accept(response(), 100)
    assert not g.accept(broken, 100)
    assert not g.response_valid and g.latched and not g.valid()


@pytest.mark.parametrize('sent_at', [99.9, 100.1, float('nan'), float('inf')])
def test_invalid_or_reordered_request_time_cannot_reset_gate(sent_at):
    g = LeaseGate(lambda: 100)
    g.accept(response(False), 100)
    g.accept(response(), 100)
    assert not g.accept(response(False, session='new-session'), sent_at)
    assert g.session == 'one' and g.latched and not g.response_valid


@pytest.mark.parametrize('bad_config', [None, [], {'commissioned': True, 'arm_stop_verified': True, 'arm_stop_service': '/stop', 'limits': None}])
def test_malformed_site_shape_reports_validation_error(bad_config):
    with pytest.raises(ValueError):
        validate_config(bad_config)


def test_pose_never_skips_limit_checks_for_empty_bounds():
    with pytest.raises(ValueError, match='bounds'):
        pose([10000, 0, 10000], [])


def test_subpixel_roi_is_rejected_before_opencv_crop():
    c = config()
    c['sources'][0]['roi'] = [.1, .1, .10001, .10001]
    with pytest.raises(ValueError, match='pixel'):
        validate_config(c)


def test_calibrated_tower_with_increasing_heights_is_valid():
    c = config()
    c['slots']['1-1-1'] = dict(above=[200, 0, 100], down=[200, 0, 20], transfer=[], retreat=[])
    c['slots']['1-1-2'] = dict(above=[200, 0, 130], down=[200, 0, 50], transfer=[], retreat=[])
    assert validate_config(c)
    del c['slots']['1-1-1']
    with pytest.raises(ValueError, match='lower layer'):
        validate_config(c)


def test_missing_measured_feedback_cannot_commission_legacy_driver():
    c = config()
    c.pop('arm_feedback')
    with pytest.raises(ValueError, match='arm_feedback'):
        validate_config(c)


def test_stationary_configuration_does_not_require_map_or_docking():
    c = config()
    c['operation_mode'] = 'stationary'
    c['sources'][0].pop('map_pose')
    c.pop('dock_xy_tolerance_m')
    c.pop('dock_yaw_tolerance_rad')
    assert validate_config(c)


@pytest.mark.parametrize('change', [dict(verified=False), dict(command_xyz_mm=[201,0,-10]),
    dict(measured_xyz_mm=[200,0,-6]), dict(measured_xyz_mm=[200,0,float('nan')])])
def test_release_reference_must_match_verified_station(change):
    c = config()
    ref = dict(verified=True, command_xyz_mm=[200,0,-10], measured_xyz_mm=[200,0,-8.9])
    ref.update(change)
    c['slots']['1-1-0']['release_reference'] = ref
    with pytest.raises(ValueError): validate_config(c)


def test_release_reference_does_not_overwrite_motion_target():
    from spark_blocks_bridge.safety import placement_reference
    c = config()
    c['slots']['1-1-0']['release_reference'] = dict(verified=True,
        command_xyz_mm=[200,0,-10], measured_xyz_mm=[200,0,-8.9])
    validated = validate_config(c)
    slot = validated['slots']['1-1-0']
    assert slot['down'] == [200,0,-10]
    assert placement_reference(slot, c['limits']) == [200,0,-8.9]
