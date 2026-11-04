import json
import math
import re
import threading
import time
import pytest
from spark_blocks_bridge.swiftpro import SerialLink, SwiftExecutor


class Clock:
    now = 100.
    def __call__(self):
        return self.now


class Firmware:
    def __init__(self):
        self.xyz = [180., 0., 20.]
        self.commands = []
        self.stops = 0
        self.reject = False
        self.wrong_endpoint = False
    def stop(self):
        self.stops += 1
    def query(self, cmd):
        self.commands.append(cmd)
        if cmd == 'P2201': return 'VSwiftPro'
        if cmd == 'P2203': return 'V3.2.0'
        if cmd == 'P2234' or cmd.startswith('M2203'): return 'V1'
        if cmd == 'P2200': return 'B90 L60 R30'
        if cmd.startswith('M2221'): return 'X%.2f Y%.2f Z%.2f' % tuple(self.xyz)
        if cmd == 'M2200': return 'V0'
        if cmd.startswith('M2222'): return 'V0' if self.reject else 'V1'
        if cmd.startswith('G0') and not self.wrong_endpoint:
            self.xyz = list(map(float, re.findall(r'[XYZ](-?[\d.]+)', cmd)))
        if cmd == 'P2220': pytest.fail('Cached target must not be used as measured feedback')
        return ''


@pytest.fixture
def executor():
    clock, fw, output = Clock(), Firmware(), []
    e = SwiftExecutor(fw, [[100, 300], [-150, 150], [-60, 180]], output.append, clock, clock)
    e.identify()
    e.sample()
    e.heartbeat(dict(driver_id=e.instance, owner='bridge', session='task', seq=1, stamp=clock(), armed=True))
    return e, fw, clock, output


def command(e, clock, **kwargs):
    return dict(driver_id=e.instance, owner='bridge', session='task', id='action-1', stamp=clock(), **kwargs)


def tick(e, clock):
    clock.now += .05
    e.heartbeat(dict(driver_id=e.instance, owner='bridge', session='task', seq=e.lease_seq+1, stamp=clock(), armed=True))
    e.step()


def test_measured_angles_are_converted_by_firmware_and_not_cached_xyz(executor):
    e, fw, _, output = executor
    assert 'P2200' in fw.commands and 'M2221 B90.00 L60.00 R30.00' in fw.commands
    assert output[-1]['xyz_mm'] == fw.xyz
    assert output[-1]['driver_id'] == e.instance


def test_full_path_checked_then_bounded_commands_complete_once(executor):
    e, fw, clock, output = executor
    cmd = command(e, clock, action='move', xyz_mm=[190, 0, 20])
    e.accept(cmd)
    for _ in range(16): tick(e, clock)
    motions = [c for c in fw.commands if c.startswith('G0')]
    assert len(motions) == 4
    assert all(c.endswith('F300') for c in motions)
    before = fw.commands[:fw.commands.index(motions[0])]
    assert len([c for c in before if c.startswith('M2222')]) == 5
    previous = [180, 0, 20]
    for cmd in motions:
        target = list(map(float, re.findall(r'[XYZ](-?[\d.]+)', cmd)))
        assert math.dist(previous, target) <= 3.01
        previous = target
    assert output[-1]['completed_id'] == 'action-1'
    assert not e.accept(command(e, clock, action='move', xyz_mm=[190, 0, 20]))
    assert not e.latched


def test_unreachable_path_never_moves_or_pumps(executor):
    e, fw, clock, _ = executor
    fw.reject = True
    e.accept(command(e, clock, action='move', xyz_mm=[190, 0, 20]))
    with pytest.raises(RuntimeError, match='waypoint'): e.step()
    assert not any(c.startswith(('G0', 'M2231')) for c in fw.commands)
    assert fw.stops == 1 and e.latched


def test_descent_uses_slow_feed(executor):
    e, fw, clock, _ = executor
    e.accept(command(e, clock, action='move', xyz_mm=[180, 0, 17]))
    e.step()
    assert [c for c in fw.commands if c.startswith('G0')] == ['G0 X180.00 Y0.00 Z17.00 F120']


def test_pump_ack_correlated_but_never_claims_suction(executor):
    e, fw, clock, output = executor
    e.accept(command(e, clock, action='pump', enabled=True))
    e.step()
    tick(e, clock)
    assert output[-1]['completed_id'] == 'action-1' and output[-1]['pump_applied'] is True
    assert 'holding' not in output[-1]
    e.stop('lost connection')
    assert 'M2231 V0' not in fw.commands


def test_lease_expiry_stops_in_worker_independent_watchdog(executor):
    e, fw, clock, _ = executor
    e.accept(command(e, clock, action='move', xyz_mm=[190, 0, 20]))
    e.step()
    clock.now += .31
    e.watchdog()
    assert e.latched and fw.stops == 1
    with pytest.raises(RuntimeError):
        e.accept(command(e, clock, action='pump', enabled=False))
    assert 'M2231 V0' not in fw.commands


def test_queued_new_action_is_rejected_and_old_instance_cannot_execute(executor):
    e, _, clock, _ = executor
    cmd = command(e, clock, action='move', xyz_mm=[190, 0, 20])
    with pytest.raises(RuntimeError): e.accept(dict(cmd, driver_id='old-driver'))
    e.accept(cmd)
    with pytest.raises(RuntimeError, match='busy'): e.accept(dict(cmd, id='second'))


def test_endpoint_error_stops_without_replay(executor):
    e, fw, clock, _ = executor
    fw.wrong_endpoint = True
    e.accept(command(e, clock, action='move', xyz_mm=[192, 0, 20]))
    e.step()
    # No second waypoint is sent if actual motion never reaches the first.
    with pytest.raises(RuntimeError, match='expired'):
        for _ in range(85): tick(e, clock)
    assert len([c for c in fw.commands if c.startswith('G0')]) == 1
    assert e.latched and fw.stops == 1


@pytest.mark.parametrize('patch', [dict(seq=1), dict(stamp=99), dict(owner='different'), dict(session='different')])
def test_old_or_changed_lease_rejected(executor, patch):
    e, _, clock, _ = executor
    clock.now += .05
    data = dict(driver_id=e.instance, owner='bridge', session='task', seq=2, stamp=clock(), armed=True)
    data.update(patch)
    with pytest.raises(RuntimeError): e.heartbeat(data)


def test_emergency_write_does_not_wait_for_missing_serial_reply():
    class Port:
        def __init__(self): self.writes = []
        def write(self, data): self.writes.append(data)
        def readline(self): time.sleep(.005); return b''
    port = Port()
    link = SerialLink(port)
    failures = []
    def query():
        try: link.query('G0 X200 Y0 Z20 F120', timeout=.15)
        except RuntimeError: failures.append(True)
    worker = threading.Thread(target=query)
    worker.start()
    deadline = time.monotonic()+1
    while not port.writes and time.monotonic() < deadline: time.sleep(.001)
    link.stop()
    assert worker.is_alive() and port.writes[-1] == b'M410\nM2000\n'
    with pytest.raises(RuntimeError, match='latched'): link.query('M2231 V0')
    worker.join(1)
    assert failures == [True]


def test_executor_measurements_drive_correlated_bridge_arrival(executor):
    from spark_blocks_bridge.arm_feedback import ArmFeedback
    e, _, clock, _ = executor
    monitor = ArmFeedback(clock=clock)
    e.emit = lambda data: monitor.update(data, clock())
    e.sample()
    cmd = command(e, clock, action='move', xyz_mm=[189, 0, 20])
    monitor.begin(cmd['xyz_mm'], clock(), cmd['id'])
    e.accept(cmd)
    for i in range(14):
        tick(e, clock)
        if i < 4:
            assert not monitor.reached()
    assert monitor.reached() and monitor.driver_id == e.instance


def test_segment_watchdog_deadline_retains_pump_even_with_fresh_lease(executor):
    e, fw, clock, _ = executor
    e.accept(command(e, clock, action='move', xyz_mm=[190, 0, 20]))
    e.step()
    for _ in range(81):
        clock.now += .05
        e.heartbeat(dict(driver_id=e.instance, owner='bridge', session='task', seq=e.lease_seq+1, stamp=clock(), armed=True))
        e.sample()
    e.watchdog()
    assert e.latched and 'M2231 V0' not in fw.commands


def test_no_pump_off_when_move_query_loses_its_reply(executor):
    e, fw, clock, _ = executor
    query = fw.query
    def fail(cmd):
        if cmd.startswith('G0'):
            fw.commands.append(cmd)
            raise RuntimeError('no matching acknowledgement')
        return query(cmd)
    fw.query = fail
    e.accept(command(e, clock, action='move', xyz_mm=[190, 0, 20]))
    with pytest.raises(RuntimeError, match='acknowledgement'): e.step()
    assert len([c for c in fw.commands if c.startswith('G0')]) == 1
    assert 'M2231 V0' not in fw.commands and e.latched


def test_fresh_heartbeat_cannot_resurrect_expired_active_lease(executor):
    e, fw, clock, _ = executor
    e.accept(command(e, clock, action='move', xyz_mm=[190, 0, 20]))
    e.step()
    clock.now += .31
    with pytest.raises(RuntimeError, match='expired'):
        e.heartbeat(dict(driver_id=e.instance, owner='bridge', session='task', seq=2, stamp=clock(), armed=True))
    assert e.latched and fw.stops == 1


def test_stopped_flag_at_old_position_cannot_finish_short_move(executor):
    e, fw, clock, _ = executor
    fw.wrong_endpoint = True
    e.accept(command(e, clock, action='move', xyz_mm=[182, 0, 20]))
    e.step()
    tick(e, clock)
    assert e.completed_id is None and e.active is not None


def test_pump_release_without_verified_placement_is_rejected(executor):
    e, fw, clock, _ = executor
    with pytest.raises(RuntimeError, match='support'):
        e.accept(command(e, clock, action='pump', enabled=False))
    assert 'M2231 V0' not in fw.commands


def complete_placement(e, clock, **kwargs):
    cmd = command(e, clock, action='move', xyz_mm=[180, 0, 19], **kwargs)
    e.accept(cmd)
    for _ in range(6): tick(e, clock)
    assert e.completed_id == cmd['id']
    return cmd['id']


def release_command(e, clock, placement):
    return dict(command(e, clock, action='pump', enabled=False), id='release-1',
                after_motion_id=placement, support_xyz_mm=e.last_motion_target)


def test_supported_release_executes_once_after_placement(executor):
    e, fw, clock, output = executor
    placement = complete_placement(e, clock)
    e.accept(release_command(e, clock, placement))
    tick(e, clock)
    tick(e, clock)
    assert fw.commands.count('M2231 V0') == 1
    assert output[-1]['completed_id'] == 'release-1' and output[-1]['pump_applied'] is False


@pytest.mark.parametrize('offset', [[0, 0, .81], [2.01, 0, 0]])
def test_last_moment_drift_blocks_release_even_after_placement_completed(executor, offset):
    e, fw, clock, _ = executor
    placement = complete_placement(e, clock)
    e.accept(release_command(e, clock, placement))
    fw.xyz = [a+b for a,b in zip(fw.xyz, offset)]
    with pytest.raises(RuntimeError, match='support tolerance'): tick(e, clock)
    assert 'M2231 V0' not in fw.commands and e.latched


def test_stale_placement_id_cannot_release_at_same_position(executor):
    e, fw, clock, _ = executor
    complete_placement(e, clock)
    with pytest.raises(RuntimeError, match='support reference'):
        e.accept(release_command(e, clock, 'old-motion'))
    assert 'M2231 V0' not in fw.commands


def test_measured_reference_does_not_change_nominal_motion(executor):
    e, fw, clock, _ = executor
    original = fw.query
    def shifted(cmd):
        reply = original(cmd)
        if cmd.startswith('G0'):
            fw.xyz[2] += 1.1
        return reply
    fw.query = shifted
    placement = complete_placement(e, clock, goal_xyz_mm=[180, 0, 20.1])
    assert 'G0 X180.00 Y0.00 Z19.00 F120' in fw.commands
    assert e.last_motion_target == [180, 0, 20.1]
    e.accept(release_command(e, clock, placement))
    tick(e, clock)
    assert fw.commands.count('M2231 V0') == 1


def test_three_stopped_samples_are_required_for_each_segment(executor):
    e, fw, clock, _ = executor
    e.accept(command(e, clock, action='move', xyz_mm=[186,0,20]))
    tick(e, clock)
    for _ in range(2): tick(e, clock)
    assert len([c for c in fw.commands if c.startswith('G0')]) == 1
    tick(e, clock)
    assert len([c for c in fw.commands if c.startswith('G0')]) == 2


def test_heartbeat_after_latch_does_not_repeat_stop_or_clear_latch(executor):
    e, fw, clock, _ = executor
    e.accept(command(e, clock, action='move', xyz_mm=[186,0,20]))
    e.stop('operator stop')
    clock.now += .4
    e.heartbeat(dict(driver_id=e.instance, owner='bridge', session='task', seq=2, stamp=clock(), armed=True))
    assert e.latched and fw.stops == 1


def test_fragmented_serial_ack_is_buffered_without_resending_command():
    class Port:
        def __init__(self):
            self.parts = [b'$0 ok old\n$1 o', b'k X180.00 Y0.', b'00 Z20.00\n']
            self.writes = []
        def write(self, data): self.writes.append(data)
        def readline(self): return self.parts.pop(0) if self.parts else b''
    port = Port()
    assert SerialLink(port).query('M2221 B90 L60 R30') == 'X180.00 Y0.00 Z20.00'
    assert len(port.writes) == 1
