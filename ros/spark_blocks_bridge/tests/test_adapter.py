"""Execute the actual adapter orchestration with injected ROS boundaries.

These tests do not emulate physics, ROS transport, or hardware stop performance.
"""
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import numpy as np

from spark_blocks_bridge.safety import LeaseGate


@pytest.fixture
def adapter(monkeypatch):
    modules = {
        'actionlib': [], 'rosgraph': [], 'rospy': [], 'tf': [], 'requests': [], 'cv_bridge': ['CvBridge'],
        'geometry_msgs.msg': ['Twist'], 'move_base_msgs.msg': ['MoveBaseAction', 'MoveBaseGoal'],
        'nav_msgs.msg': ['Odometry'], 'sensor_msgs.msg': ['Image', 'LaserScan'],
        'spark_carry_object.msg': ['position', 'status'], 'std_srvs.srv': ['Trigger'],
        'std_msgs.msg': ['String'],
    }
    for name, members in modules.items():
        module = ModuleType(name)
        for member in members:
            setattr(module, member, type(member, (), {}))
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'blocks_bridge_node.py'
    spec = importlib.util.spec_from_file_location('tested_blocks_adapter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    obj = module.BlocksBridge.__new__(module.BlocksBridge)
    module.rospy.Time = SimpleNamespace(now=lambda: SimpleNamespace(to_sec=lambda: 100.))
    obj.arm_last_motion_id = None
    obj.arm_feedback = SimpleNamespace(fresh=lambda: True, available=lambda: True, stopped=lambda *args: True)
    obj.lock = threading.RLock()
    obj.gate = LeaseGate()
    obj.id = 'test-bridge'
    obj.busy = True
    obj.active = None
    obj.result = None
    obj.owned_goal = False
    obj.stop_output_failed = False
    obj.watchdog_fault = None
    obj.config = dict(sources=[{'map_pose': [1, 0, 0]}], slots={'1-1-2': {
        'transfer': [[200, 0, 180]], 'above': [240, 0, 180], 'down': [240, 0, 130],
        'retreat': [[170, 0, 180]],
    }}, rest=[120, 0, 50], carry=[200, 0, 150], dock_xy_tolerance_m=.01, dock_yaw_tolerance_rad=.02)
    obj.executor_message = lambda command, **payload: ('test-action', SimpleNamespace(data='test'))
    obj.publish_arm_lease = lambda: None
    obj.check = lambda command: None
    return obj


def command(action='place'):
    return dict(id='cmd-1', session='test-session', action=action, source=0, slot='1-1-2', home=[0, 0, 0])


def test_place_executes_commissioned_approach_then_releases_then_vertical_retreat(adapter):
    operations = []
    adapter.move_arm = lambda xyz, _: operations.append(('move', xyz))
    adapter.vacuum = lambda enabled, _: operations.append(('pump', enabled))
    adapter.stop = lambda reason: operations.append(('stop', reason))
    adapter.execute(command())
    assert operations == [('move', [200, 0, 180]), ('move', [240, 0, 180]),
                          ('move', [240, 0, 130]), ('pump', False), ('move', [240, 0, 180]),
                          ('move', [170, 0, 180]), ('move', [120, 0, 50])]
    assert adapter.result['ok'] and not adapter.busy
    assert '尚未验证' in adapter.result['observation']


def setup_pick(adapter):
    adapter.config.update(limits=[[100, 300], [-150, 150], [-60, 200]],
                          pixel_to_arm=[.5, 100, .5, -150])
    adapter.config['sources'][0].update(approach_z=50, pickup_z=-20)
    adapter.blue_center = lambda source, cmd: [300, 200]


def test_pick_stops_above_source_before_unconfirmed_transport(adapter):
    setup_pick(adapter)
    operations = []
    adapter.move_arm = lambda xyz, _: operations.append(('move', xyz))
    adapter.vacuum = lambda enabled, _: operations.append(('pump', enabled))
    adapter.stop = lambda reason: operations.append(('stop', reason))
    adapter.execute(command('pick'))
    assert adapter.result['ok']
    assert operations == [('move', [120, 0, 50]), ('move', [200, 0, 50]),
                          ('move', [200, 0, -20]), ('pump', True), ('move', [200, 0, 50])]
    assert ('move', adapter.config['carry']) not in operations


def test_missing_suction_evidence_prevents_lift_and_preserves_vacuum(adapter):
    setup_pick(adapter)
    operations = []
    adapter.move_arm = lambda xyz, _: operations.append(('move', xyz))
    adapter.vacuum = lambda enabled, _: operations.append(('pump', enabled))
    def reject(_):
        raise RuntimeError('No fresh measured suction confirmation')
    adapter.require_grasp = reject
    adapter.stop = lambda reason: operations.append(('stop', reason))
    adapter.execute(command('pick'))
    assert not adapter.result['ok']
    assert operations[-2][0] == 'pump' and operations[-2][1] is True
    assert operations[-1][0] == 'stop'
    assert ('pump', False) not in operations


def test_pick_applies_fixed_xy_correction_to_both_approach_and_contact(adapter):
    setup_pick(adapter)
    adapter.config['sources'][0]['pickup_xy_offset_mm'] = [5, -2]
    operations = []
    adapter.move_arm = lambda xyz, _: operations.append(('move', xyz))
    adapter.vacuum = lambda enabled, _: operations.append(('pump', enabled))
    adapter.stop = lambda reason: operations.append(('stop', reason))
    adapter.execute(command('pick'))
    assert adapter.result['ok']
    assert operations[1:3] == [('move', [205, -2, 50]), ('move', [205, -2, -20])]
    assert operations[-1] == ('move', [205, -2, 50])


def test_confirmed_navigate_home_moves_to_travel_pose_first(adapter):
    operations = []
    adapter.move_arm = lambda xyz, _: operations.append(('move', xyz))
    adapter.navigate = lambda xyz, _: operations.append(('navigate', xyz))
    adapter.stop = lambda reason: operations.append(('stop', reason))
    adapter.execute(command('navigate_home'))
    assert adapter.result['ok']
    assert operations == [('move', adapter.config['carry']), ('navigate', [0, 0, 0])]


def test_cancel_during_descent_never_releases_suspended_load(adapter):
    operations = []
    def move(xyz, _):
        operations.append(('move', xyz))
        if xyz[2] == 130:
            raise RuntimeError('lease cancelled')
    adapter.move_arm = move
    adapter.vacuum = lambda enabled, _: operations.append(('pump', enabled))
    adapter.stop = lambda reason: operations.append(('stop', reason))
    adapter.execute(command())
    assert not adapter.result['ok']
    assert not any(op[0] == 'pump' for op in operations)
    assert operations[-1] == ('stop', 'lease cancelled')
    assert not adapter.busy and adapter.active is None


def test_displaced_base_invalidates_place_even_when_tf_is_fresh(adapter):
    # Restore real check implementation after fixture's actuator-free setup.
    check = type(adapter).check.__get__(adapter)
    adapter.dry = False
    adapter.done = threading.Event()
    adapter.gate = LeaseGate(lambda: 100)
    adapter.gate.accept(dict(session='s1', armed=False), 100)
    adapter.gate.accept(dict(session='s1', armed=True, command={'id': 'cmd-1'}, lease_seconds=.65), 100)
    adapter.sensor_fault = lambda: None
    adapter.moving = False
    adapter.current_pose = lambda: [.2, 0, 0]
    with pytest.raises(RuntimeError, match='Docking'):
        check(command())
    adapter.current_pose = lambda: [0, 0, 0]
    adapter.moving = True
    with pytest.raises(RuntimeError, match='Base moved'):
        check(command())


@pytest.mark.parametrize('goal_sent', [False, True])
def test_velocity_only_flows_after_own_goal_is_sent(adapter, monkeypatch, goal_sent):
    def twist():
        return SimpleNamespace(linear=SimpleNamespace(x=0), angular=SimpleNamespace(z=0))
    globals_ = type(adapter).watchdog.__globals__
    monkeypatch.setitem(globals_, 'Twist', twist)
    monkeypatch.setattr(globals_['rospy'], 'is_shutdown', lambda: False, raising=False)
    waits = iter([False, True])
    adapter.done = SimpleNamespace(wait=lambda _: next(waits))
    adapter.dry = False
    adapter.ownership_ok = True
    adapter.sensor_fault = lambda: None
    adapter.gate = SimpleNamespace(armed=True, valid=lambda *args: True)
    adapter.active = command('navigate_pick')
    adapter.navigation_command_id = 'cmd-1' if goal_sent else None
    adapter.nav = twist()
    adapter.nav.linear.x = .5
    adapter.nav.angular.z = .8
    adapter.nav_time = time.monotonic()
    output = []
    adapter.vel = SimpleNamespace(publish=output.append)
    adapter.watchdog()
    assert len(output) == 1
    assert output[0].linear.x == (.10 if goal_sent else 0)
    assert output[0].angular.z == (.15 if goal_sent else 0)


@pytest.mark.parametrize('blocked', ['busy', 'moving', 'stop_pending', 'arm_stopped', 'stale'])
def test_stop_ack_requires_completed_stop_and_fresh_motion_observation(adapter, blocked):
    adapter.stop_request = 'stop-1'
    adapter.busy = adapter.moving = adapter.stop_pending = False
    adapter.arm_stopped = True
    adapter.ages = lambda: (.1, .1, .1)
    assert adapter.stop_acknowledgement() == 'stop-1'
    if blocked == 'stale':
        adapter.ages = lambda: (.1, .1, .8)
    else:
        setattr(adapter, blocked, blocked != 'arm_stopped')
    assert adapter.stop_acknowledgement() is None


@pytest.mark.parametrize('failed_output', ['velocity', 'navigation'])
def test_stop_still_calls_arm_stop_if_another_output_raises(adapter, failed_output):
    called = threading.Event()
    def broken(*args):
        raise RuntimeError('transport disconnected')
    adapter.gate = LeaseGate()
    adapter.active = command('navigate_pick')
    adapter.dry = False
    adapter.owned_goal = True
    adapter.stop_pending = False
    adapter.arm_stopped = True
    adapter.vel = SimpleNamespace(publish=broken if failed_output == 'velocity' else lambda _: None)
    adapter.client = SimpleNamespace(cancel_goal=broken if failed_output == 'navigation' else lambda: None)
    def arm_stop():
        called.set()
        return SimpleNamespace(success=True)
    adapter.stop_arm = arm_stop
    adapter.stop('operator stop')
    assert called.wait(1), 'an unrelated output exception must not skip the arm stop'
    assert adapter.gate.latched and not adapter.gate.armed
    assert adapter.stop_output_failed


def test_watchdog_exception_latches_failure_and_attempts_stop(adapter, monkeypatch):
    globals_ = type(adapter).watchdog.__globals__
    monkeypatch.setattr(globals_['rospy'], 'is_shutdown', lambda: False, raising=False)
    adapter.done = SimpleNamespace(wait=lambda _: False)
    adapter.gate = LeaseGate(lambda: 100)
    adapter.gate.accept(dict(session='s', armed=False), 100)
    adapter.gate.accept(dict(session='s', armed=True, lease_seconds=.65, command={'id': 'cmd-1'}), 100)
    adapter.dry = False
    adapter.stop_pending = False
    adapter.arm_stopped = True
    adapter.vel = SimpleNamespace(publish=lambda _: None)
    stopped = threading.Event()
    adapter.stop_arm = lambda: (stopped.set() or SimpleNamespace(success=True))
    def fail_sensor():
        raise RuntimeError('unexpected sensor exception')
    adapter.sensor_fault = fail_sensor
    adapter.watchdog()
    assert stopped.wait(1)
    assert adapter.watchdog_fault and adapter.gate.latched
    # Even a later renewed lease cannot hide a dead watchdog from execution.
    adapter.gate.valid = lambda *args: True
    with pytest.raises(RuntimeError, match='cancelled'):
        type(adapter).check(adapter, command())


@pytest.mark.parametrize('current', [True, False])
def test_rejected_current_result_stops_but_consumed_result_does_not(adapter, current):
    adapter.gate = SimpleNamespace(armed=True, command_id='cmd-1' if current else 'cmd-2')
    stops = []
    adapter.stop = stops.append
    adapter.reject_result('cmd-1')
    assert bool(stops) is current


def test_inspection_upload_and_result_share_exact_frame_token(adapter):
    adapter.frame_seq = 10
    adapter.wait = lambda *_: None
    adapter.frame_copy = lambda: (np.zeros((100, 100, 3), np.uint8), 11)
    adapter.session = lambda: None
    uploads = []
    def post(session, route, **kwargs):
        uploads.append((route, kwargs['headers']))
        return {'frame_id': 'b'*32}
    adapter.post = post
    adapter.stop = lambda reason: pytest.fail(reason)
    adapter.execute(command('verify_place'))
    assert adapter.result['ok'] and adapter.result['frame_id'] == 'b'*32
    assert uploads[0][1]['X-Command-Id'] == 'cmd-1'
    assert uploads[0][1]['X-Task-Session'] == 'test-session'
    assert uploads[0][1]['X-Frame-Sequence'] == '11'


def test_frozen_ros_clock_cannot_make_old_camera_message_fresh(adapter):
    adapter.frame_lock = threading.Lock()
    adapter.frame, adapter.frame_seq, adapter.frame_stamp = object(), 10, time.monotonic()-2
    adapter.age = lambda _: 0
    with pytest.raises(RuntimeError, match='Fresh camera'):
        adapter.frame_copy()


@pytest.mark.parametrize('topic', ['image', 'scan', 'odom'])
@pytest.mark.parametrize('replayed_stamp', [100.0, 99.9])
def test_replayed_sensor_messages_cannot_refresh_age_or_replace_newer_state(adapter, monkeypatch, topic, replayed_stamp):
    now = [200.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    adapter.frame_lock = threading.Lock()
    adapter.frame, adapter.frame_stamp, adapter.frame_seq = None, 0, 0
    adapter.scan_age = adapter.odom_age = 1e6
    adapter.scan_time = adapter.odom_time = -1e9
    adapter.sensor_stamps = {}
    # ROS time may be paused; local elapsed time must still expire the data.
    adapter.age = lambda _: 0
    def message(stamp, dangerous):
        return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(to_sec=lambda: stamp)),
            ranges=[.2 if dangerous else 2.0], range_min=.1, range_max=10,
            twist=SimpleNamespace(twist=SimpleNamespace(
                linear=SimpleNamespace(x=.1 if dangerous else 0, y=0), angular=SimpleNamespace(z=0))))
    newest = message(100.0, True)
    getattr(adapter, topic)(newest)
    now[0] += .6
    getattr(adapter, topic)(message(replayed_stamp, False))
    assert adapter.ages()[['image', 'scan', 'odom'].index(topic)] > .5
    if topic == 'image':
        assert adapter.frame is newest and adapter.frame_seq == 1
    elif topic == 'scan':
        assert adapter.clearance == .2
    else:
        assert adapter.moving
    # The first genuinely newer sample restores observation freshness, without
    # requiring a bridge restart just because a duplicate packet was ignored.
    fresh = message(100.1, False)
    getattr(adapter, topic)(fresh)
    assert adapter.ages()[['image', 'scan', 'odom'].index(topic)] == 0
    if topic == 'image':
        assert adapter.frame is fresh and adapter.frame_seq == 2
    elif topic == 'scan':
        assert adapter.clearance == 2.0
    else:
        assert not adapter.moving


@pytest.mark.parametrize('stamp', [0, float('nan'), float('inf'), 101.0, 90.0])
def test_invalid_sensor_stamp_does_not_poison_following_fresh_sample(adapter, monkeypatch, stamp):
    globals_ = type(adapter).age.__globals__
    monkeypatch.setattr(globals_['rospy'], 'Time',
                        SimpleNamespace(now=lambda: SimpleNamespace(to_sec=lambda: 100.3)), raising=False)
    adapter.frame_lock = threading.Lock()
    adapter.frame, adapter.frame_stamp, adapter.frame_seq = None, 0, 0
    adapter.sensor_stamps = {}
    def message(value):
        return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(to_sec=lambda: value)))
    adapter.image(message(stamp))
    assert adapter.frame is None and adapter.frame_seq == 0
    fresh = message(100.2)
    adapter.image(fresh)
    assert adapter.frame is fresh and adapter.frame_seq == 1


def prepare_stop(adapter):
    adapter.gate = LeaseGate()
    adapter.dry = False
    adapter.stop_pending = adapter.moving = False
    adapter.arm_stopped = True
    adapter.vel = SimpleNamespace(publish=lambda _: None)
    adapter.stop_arm = lambda: SimpleNamespace(success=True)


def test_navigation_publish_exception_still_cancels_possibly_sent_goal(adapter, monkeypatch):
    prepare_stop(adapter)
    globals_ = type(adapter).navigate.__globals__
    monkeypatch.setattr(globals_['rospy'], 'Duration', lambda x: x, raising=False)
    monkeypatch.setattr(globals_['rospy'], 'Time', SimpleNamespace(now=lambda: 0), raising=False)
    monkeypatch.setattr(globals_['tf'], 'transformations',
                        SimpleNamespace(quaternion_from_euler=lambda *_: [0, 0, 0, 1]), raising=False)
    monkeypatch.setitem(globals_, 'MoveBaseGoal', lambda: SimpleNamespace(target_pose=SimpleNamespace(
        header=SimpleNamespace(), pose=SimpleNamespace(position=SimpleNamespace(), orientation=SimpleNamespace()))))
    operations = []
    def send(_):
        operations.append('send')
        raise RuntimeError('publish raised after handing goal to transport')
    adapter.client = SimpleNamespace(wait_for_server=lambda _: True, send_goal=send,
                                     cancel_goal=lambda: operations.append('cancel'))
    adapter.move_arm = lambda *_: None
    adapter.execute(command('navigate_pick'))
    assert operations == ['send', 'cancel']
    assert not adapter.owned_goal and adapter.navigation_command_id is None
    assert adapter.gate.latched and not adapter.result['ok']


def test_failed_arm_stop_thread_can_be_retried_without_false_ack(adapter, monkeypatch):
    prepare_stop(adapter)
    adapter.busy = False
    adapter.stop_request = 'stop-1'
    adapter.ages = lambda: (0, 0, 0)
    attempts, calls = [], []
    adapter.stop_arm = lambda: (calls.append('stop arm') or SimpleNamespace(success=True))
    class Worker:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            attempts.append('start')
            if len(attempts) == 1:
                raise RuntimeError('cannot start new thread')
            self.target()
    monkeypatch.setitem(type(adapter).stop.__globals__, 'threading', SimpleNamespace(Thread=Worker))
    adapter.stop('first request')
    assert not adapter.stop_pending and not adapter.arm_stopped
    assert adapter.stop_acknowledgement() is None
    assert adapter.gate.latched
    adapter.stop_request = 'stop-2'
    adapter.stop('operator retries stop')
    assert calls == ['stop arm']
    assert adapter.stop_acknowledgement() == 'stop-2'


def prepare_network(adapter, monkeypatch, iterations):
    prepare_stop(adapter)
    globals_ = type(adapter).network.__globals__
    monkeypatch.setattr(globals_['rospy'], 'is_shutdown', lambda: False, raising=False)
    adapter.gate.accept(dict(session='test-session', armed=False), time.monotonic())
    adapter.busy = False
    adapter.seen = set()
    adapter.fault = adapter.config_error = None
    adapter.ownership_ok = True
    adapter.ownership_checked = time.monotonic()
    adapter.arm = adapter.pump = SimpleNamespace(get_num_connections=lambda: 1)
    adapter.current_pose = lambda: [0, 0, 0]
    adapter.ages = lambda: (0, 0, 0)
    adapter.clearance = 1
    adapter.stop_request = None
    adapter.session = lambda: None
    completed = []
    adapter.done = SimpleNamespace(is_set=lambda: len(completed) >= iterations,
                                   wait=lambda _: completed.append(True))
    return globals_


def test_action_thread_start_failure_clears_busy_and_stops(adapter, monkeypatch):
    globals_ = prepare_network(adapter, monkeypatch, 1)
    starts = []
    class Worker:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            if self.target == adapter.execute:
                starts.append('action')
                raise RuntimeError('cannot start new thread')
            self.target()
    monkeypatch.setitem(globals_, 'threading', SimpleNamespace(Thread=Worker))
    adapter.post = lambda *_args, **_kwargs: dict(session='test-session', armed=True,
                                                command=command(), lease_seconds=.65)
    adapter.network()
    assert starts == ['action']
    assert not adapter.busy and adapter.active is None
    assert adapter.gate.latched and not adapter.gate.armed
    assert adapter.fault


@pytest.mark.parametrize('server_consumed', [False, True])
def test_result_transport_failure_never_reexecutes_pick(adapter, monkeypatch, server_consumed):
    globals_ = prepare_network(adapter, monkeypatch, 3)
    class HTTPError(Exception):
        response = SimpleNamespace(status_code=409)
    monkeypatch.setattr(globals_['requests'], 'HTTPError', HTTPError, raising=False)
    class Worker:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setitem(globals_, 'threading', SimpleNamespace(Thread=Worker))
    executions, reports = [], []
    remote = [command('pick')]
    def execute(cmd):
        executions.append(cmd['id'])
        adapter.busy, adapter.active = False, None
        adapter.result = dict(command_id=cmd['id'], bridge_id=adapter.id, ok=True, observation='test')
    adapter.execute = execute
    def post(session, route, **kwargs):
        if route == 'tick':
            return dict(session='test-session', armed=True, command=remote[0], lease_seconds=.65)
        reports.append(kwargs['json']['command_id'])
        if len(reports) == 1:
            if server_consumed:
                remote[0] = None
            raise OSError('response lost' if server_consumed else 'request lost')
        if server_consumed:
            raise HTTPError('already consumed')
        remote[0] = None
        return {'ok': True}
    adapter.post = post
    adapter.network()
    assert executions == ['cmd-1']
    assert reports == ['cmd-1', 'cmd-1']
    assert adapter.result is None
    assert not adapter.gate.latched and adapter.fault is None


@pytest.mark.parametrize('topic', ['image', 'scan', 'odom'])
@pytest.mark.parametrize('armed,gap,should_stop', [(True, .3, False), (True, .6, True), (False, .6, False)])
def test_returning_sensor_cannot_hide_active_task_data_gap(adapter, monkeypatch, topic, armed, gap, should_stop):
    now = [200.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    adapter.gate = LeaseGate(lambda: now[0])
    adapter.gate.accept(dict(session='s', armed=False), now[0])
    adapter.frame_lock = threading.Lock()
    adapter.frame, adapter.frame_stamp, adapter.frame_seq = None, 0, 0
    adapter.scan_age = adapter.odom_age = 1e6
    adapter.scan_time = adapter.odom_time = -1e9
    adapter.sensor_stamps = {}
    adapter.age = lambda _: 0
    def message(stamp):
        return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(to_sec=lambda: stamp)),
            ranges=[2.0], range_min=.1, range_max=10,
            twist=SimpleNamespace(twist=SimpleNamespace(
                linear=SimpleNamespace(x=0, y=0), angular=SimpleNamespace(z=0))))
    stops = []
    def stop(reason):
        stops.append(reason)
        adapter.gate.stop()
    adapter.stop = stop
    getattr(adapter, topic)(message(100))
    if armed:
        adapter.gate.accept(dict(session='s', armed=True, command={'id': 'cmd'}, lease_seconds=.65), now[0])
    now[0] += gap
    # A newer sample arrives before the next watchdog iteration.
    getattr(adapter, topic)(message(100+gap))
    assert bool(stops) == should_stop
    assert adapter.gate.latched == should_stop
    assert adapter.ages()[['image', 'scan', 'odom'].index(topic)] == 0
    if should_stop:
        getattr(adapter, topic)(message(101))
        assert adapter.gate.latched and not adapter.gate.armed


@pytest.mark.parametrize('delay', [.01, .6])
def test_image_freshness_after_conversion_uses_copied_frame_not_new_cache(adapter, monkeypatch, delay):
    now = [200.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    adapter.frame_lock = threading.Lock()
    adapter.frame, adapter.frame_seq, adapter.frame_stamp = object(), 1, now[0]
    adapter.age = lambda _: 0  # ROS time alone can stay frozen.
    def convert(*args):
        now[0] += delay
        # A newer camera message does not make the copied frame newer.
        adapter.frame, adapter.frame_seq, adapter.frame_stamp = object(), 2, now[0]
        return np.zeros((480, 640, 3), dtype=np.uint8)
    adapter.cv = SimpleNamespace(imgmsg_to_cv2=convert)
    if delay > .5:
        with pytest.raises(RuntimeError, match='Fresh camera'):
            adapter.frame_copy()
    else:
        frame, seq = adapter.frame_copy()
        assert seq == 1 and frame.shape == (480, 640, 3)


@pytest.mark.parametrize('disconnected', ['arm', 'pump'])
def test_local_action_check_rejects_disconnected_driver_before_publishing(adapter, disconnected):
    adapter.dry = False
    adapter.done = threading.Event()
    adapter.gate = LeaseGate(lambda: 100)
    adapter.gate.accept(dict(session='s', armed=False), 100)
    adapter.gate.accept(dict(session='s', armed=True, lease_seconds=.65, command={'id': 'cmd-1'}), 100)
    adapter.ownership_ok = True
    adapter.ownership_checked = time.monotonic()
    adapter.ages = lambda: (0, 0, 0)
    adapter.clearance = 1
    adapter.current_pose = lambda: [0, 0, 0]
    adapter.moving = False
    outputs = []
    adapter.arm = SimpleNamespace(get_num_connections=lambda: int(disconnected != 'arm'), publish=outputs.append)
    adapter.pump = SimpleNamespace(get_num_connections=lambda: int(disconnected != 'pump'), publish=outputs.append)
    adapter.check = type(adapter).check.__get__(adapter)
    adapter.wait = lambda *_: None
    with pytest.raises(RuntimeError, match='driver'):
        adapter.vacuum(False, command('place'))
    assert outputs == []
    # Reconnected transport passes the local check, without claiming actuator feedback.
    getattr(adapter, disconnected).get_num_connections = lambda: 1
    adapter.check(command('place'))


@pytest.mark.parametrize('motion', ['drift', 'stationary', 'jitter', 'settles', 'missing', 'ambiguous'])
def test_blue_target_must_stay_in_one_small_window_not_drift_between_frames(adapter, monkeypatch, motion):
    now, frames = [100.0], [0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    adapter.wait = lambda *_: now.__setitem__(0, now[0]+.1)
    def frame_copy():
        index = frames[0]
        frames[0] += 1
        offset = (index if motion == 'drift' else min(index, 15) if motion == 'settles'
                  else index % 2 if motion == 'jitter' else 0)
        frame = np.zeros((100, 320, 3), dtype=np.uint8)
        frame[30:60, 30+offset:60+offset] = (255, 0, 0)
        if index == 8 and motion == 'missing':
            frame[:] = 0
        elif index == 8 and motion == 'ambiguous':
            frame[30:60, 100:130] = (255, 0, 0)
        return frame, frames[0]
    adapter.frame_copy = frame_copy
    if motion == 'drift':
        with pytest.raises(RuntimeError, match='stable blue target'):
            adapter.blue_center({'roi': [0, 0, 1, 1]}, command('pick'))
    else:
        center = adapter.blue_center({'roi': [0, 0, 1, 1]}, command('pick'))
        assert frames[0] >= 11
        if motion == 'settles':
            assert frames[0] >= 25
            assert center == pytest.approx((59.5, 44.5))
        elif motion in ('missing', 'ambiguous'):
            assert frames[0] >= 20


@pytest.mark.parametrize('action', ['navigate_pick', 'navigate_home'])
def test_stationary_routes_never_dispatch_navigation(adapter, action):
    adapter.config['operation_mode'] = 'stationary'
    adapter.config['sources'][0].pop('map_pose')
    operations = []
    adapter.move_arm = lambda xyz, _: operations.append(('arm', xyz))
    adapter.navigate = lambda *args: pytest.fail('Stationary mode must not navigate')
    adapter.stop = lambda reason: operations.append(('stop', reason))
    cmd = command(action)
    cmd.update(operation_mode='stationary', home=None)
    adapter.execute(cmd)
    assert adapter.result['ok'] and operations[0][0] == 'arm'


def test_missing_feedback_prevents_arm_command(adapter):
    adapter.config['limits'] = [[100, 300], [-150, 150], [-60, 200]]
    adapter.moving = False
    adapter.arm_feedback = None
    published = []
    adapter.arm = SimpleNamespace(publish=published.append)
    with pytest.raises(RuntimeError, match='feedback required'):
        adapter.move_arm([200, 0, 20], command())
    assert published == []


@pytest.mark.parametrize('failure', ['timeout', 'feedback_lost', 'cancel'])
def test_arrival_failure_stops_without_releasing_or_republishing(adapter, monkeypatch, failure):
    adapter.config.update(limits=[[100, 300], [-150, 150], [-60, 200]], arm_feedback={'timeout_s': .01})
    adapter.moving = False
    outputs, stops = [], []
    adapter.arm = SimpleNamespace(publish=outputs.append)
    adapter.stop = stops.append
    adapter.done = threading.Event()
    reads = []
    def fresh():
        reads.append(True)
        return failure != 'feedback_lost' or len(reads) <= 2
    adapter.arm_feedback = SimpleNamespace(fresh=fresh, available=fresh, begin=lambda *args: None, reached=lambda: False)
    def check(_):
        if failure == 'cancel' and outputs:
            raise RuntimeError('Motion lease cancelled')
    adapter.check = check
    with pytest.raises(RuntimeError):
        adapter.move_arm([200, 0, 20], command())
    assert len(outputs) == 1 and len(stops) == 1


def test_stop_service_ack_alone_cannot_claim_physical_stop(adapter):
    prepare_stop(adapter)
    adapter.arm_feedback = None
    adapter.stop('test')
    deadline = time.monotonic()+1
    while adapter.stop_pending and time.monotonic() < deadline:
        time.sleep(.01)
    assert not adapter.arm_stopped and not adapter.stop_pending


def test_stop_request_preempts_wait_for_arrival(adapter):
    adapter.config.update(limits=[[100, 300], [-150, 150], [-60, 200]], arm_feedback={'timeout_s': 5})
    adapter.moving = False
    adapter.done = threading.Event()
    sent, cancelled, stopped = threading.Event(), threading.Event(), threading.Event()
    adapter.arm = SimpleNamespace(publish=lambda _: sent.set())
    adapter.arm_feedback = SimpleNamespace(fresh=lambda: True, available=lambda: True, begin=lambda *args: None, reached=lambda: False)
    def check(_):
        if cancelled.is_set():
            raise RuntimeError('cancelled')
    adapter.check = check
    adapter.stop = lambda _: stopped.set()
    errors = []
    def run():
        try:
            adapter.move_arm([200, 0, 20], command())
        except RuntimeError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run)
    worker.start()
    assert sent.wait(1)
    cancelled.set()
    worker.join(1)
    assert not worker.is_alive() and stopped.is_set() and errors == ['cancelled']


def test_stop_feedback_exception_does_not_keep_service_success(adapter):
    prepare_stop(adapter)
    def broken(*args):
        raise RuntimeError('feedback broke')
    adapter.arm_feedback = SimpleNamespace(stopped=broken)
    adapter.stop('test')
    deadline = time.monotonic()+1
    while adapter.stop_pending and time.monotonic() < deadline:
        time.sleep(.01)
    assert not adapter.arm_stopped and not adapter.stop_pending


def test_move_waits_for_real_monitor_arrival(adapter):
    from spark_blocks_bridge.arm_feedback import ArmFeedback
    monitor = ArmFeedback()
    seq = [0]
    def update(_):
        seq[0] += 1
        stamp = 99.95+seq[0]*.05
        monitor.update(dict(protocol=2, driver_id='test-driver', completed_id='test-action', kind='measured_tcp', seq=seq[0], stamp=stamp, xyz_mm=[200,0,20],
                            moving=False, powered=True, attached=[True]*3), stamp)
        return False
    update(0)
    adapter.config.update(limits=[[100, 300], [-150, 150], [-60, 200]], arm_feedback={'timeout_s': 1})
    adapter.moving = False
    adapter.arm_feedback = monitor
    adapter.done = SimpleNamespace(wait=update)
    outputs = []
    adapter.arm = SimpleNamespace(publish=outputs.append)
    adapter.move_arm([200,0,20], command())
    assert seq[0] == 4 and len(outputs) == 1 and monitor.reached()


def test_executor_message_waits_for_lease_ack_before_command(adapter):
    from spark_blocks_bridge.arm_feedback import ArmFeedback
    import json
    m = ArmFeedback()
    adapter.arm_feedback = m
    adapter.gate.session = 'task'
    def sample(seq, armed):
        stamp = 100+seq*.01
        m.update(dict(protocol=2, driver_id='driver', kind='measured_tcp', seq=seq, stamp=stamp,
            xyz_mm=[200,0,20], moving=False, powered=True, attached=[True]*3,
            lease_owner=adapter.id, lease_session='task', lease_armed=armed), stamp)
    sample(1, False)
    waits = []
    def wait(*args):
        waits.append(True)
        sample(2, True)
    adapter.wait = wait
    identity, msg = type(adapter).executor_message(adapter, command(), action='move', xyz_mm=[200,0,20])
    data = json.loads(msg.data)
    assert waits == [True] and data['id'] == identity and data['driver_id'] == 'driver'
    assert data['owner'] == adapter.id and data['session'] == 'task'


def test_vacuum_waits_for_correlated_ack_without_republishing(adapter):
    adapter.arm_last_motion_id = 'placed-motion'
    adapter.config['limits'] = [[100,300],[-150,150],[-60,200]]
    outputs, waits = [], []
    adapter.pump = SimpleNamespace(publish=outputs.append)
    adapter.arm_feedback = SimpleNamespace(applied=lambda identity, enabled: len(waits) > 0)
    adapter.wait = lambda seconds, command: waits.append(seconds)
    adapter.vacuum(False, command())
    assert len(outputs) == 1 and waits == [.02, 1.0]
    assert adapter.carrying is False


def test_place_motion_uses_calibrated_feedback_without_changing_command(adapter):
    adapter.config.update(limits=[[100,300],[-150,150],[-60,200]], arm_feedback={'timeout_s':1})
    slot = adapter.config['slots']['1-1-2']
    slot['release_reference'] = dict(verified=True, command_xyz_mm=slot['down'], measured_xyz_mm=[240,0,131.1])
    payloads, targets, outputs = [], [], []
    def message(command, **payload):
        payloads.append(payload)
        return 'placement-1', SimpleNamespace(data='test')
    adapter.executor_message = message
    adapter.arm_feedback = SimpleNamespace(available=lambda: True, fresh=lambda: True,
        begin=lambda target, *args: targets.append(target), reached=lambda: True)
    adapter.moving = False
    adapter.arm = SimpleNamespace(publish=outputs.append)
    adapter.move_arm(slot['down'], command())
    assert payloads == [dict(action='move', xyz_mm=[240,0,130], goal_xyz_mm=[240,0,131.1])]
    assert targets == [[240,0,131.1]] and adapter.arm_last_motion_id == 'placement-1'
    assert len(outputs) == 1


def test_pump_release_carries_matching_placement_and_support_reference(adapter):
    adapter.config['limits'] = [[100,300],[-150,150],[-60,200]]
    adapter.arm_last_motion_id = 'placement-1'
    payloads = []
    def message(command, **payload):
        payloads.append(payload)
        return 'release-1', SimpleNamespace(data='test')
    adapter.executor_message = message
    adapter.pump = SimpleNamespace(publish=lambda msg: None)
    adapter.arm_feedback = SimpleNamespace(applied=lambda identity, enabled: True)
    adapter.wait = lambda *args: None
    adapter.vacuum(False, command())
    assert payloads == [dict(action='pump', enabled=False, support_xyz_mm=[240,0,130], after_motion_id='placement-1')]
