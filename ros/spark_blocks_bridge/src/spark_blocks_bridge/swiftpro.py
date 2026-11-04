"""SwiftPro 3.2.0 bounded executor. Serial and clocks injected for offline tests.

P2200 reads sensors; M2221 converts those angles with the firmware's geometry.
P2220 is deliberately never used as physical feedback. No automatic motor enable,
home, retries, pump cleanup or recovery after a fault.
"""
import math
import re
import threading
import time
import uuid
from .safety import finite, pose, workspace_limits


def triple(reply, axes):
    pattern = r'\s*' + r'\s+'.join(axis+r'(-?\d+(?:\.\d+)?)' for axis in axes) + r'\s*'
    match = re.fullmatch(pattern, reply)
    if not match:
        raise RuntimeError('Malformed measured response: '+reply)
    result = list(map(float, match.groups()))
    if not all(math.isfinite(v) for v in result):
        raise RuntimeError('Non-finite measurement')
    return result


def bit(reply):
    if reply not in ('V0', 'V1'):
        raise RuntimeError('Invalid status response')
    return reply == 'V1'


def waypoints(start, target):
    count = max(1, math.ceil(math.dist(start, target)/3.))
    return [[a+(b-a)*i/count for a, b in zip(start, target)] for i in range(1, count+1)]


class SerialLink:
    """One query owner; independent stop can write while a reply is missing."""
    def __init__(self, port, clock=time.monotonic):
        self.port, self.clock = port, clock
        self.read_lock, self.write_lock = threading.Lock(), threading.Lock()
        self.seq, self.latched = 0, False
        self.buffer = b''

    def stop(self):
        with self.write_lock:
            self.latched = True
            # write_timeout on the port bounds this path. Do not flush/drain:
            # that can wait indefinitely for a disconnected USB endpoint.
            self.port.write(b'M410\nM2000\n')

    def query(self, command, timeout=.25):
        if not isinstance(command, str) or not command.strip() or '\n' in command or '\r' in command:
            raise ValueError('One serial command is required')
        with self.read_lock:
            with self.write_lock:
                if self.latched and command.split()[0] not in {'P2200', 'P2201', 'P2203', 'P2234', 'M2221', 'M2200', 'M2203'}:
                    raise RuntimeError('Serial stop latched')
                self.seq += 1
                seq = self.seq
                self.port.write(('#%d %s\n' % (seq, command)).encode('ascii'))
            deadline = self.clock()+timeout
            while self.clock() < deadline:
                if b'\n' not in self.buffer:
                    self.buffer += self.port.readline()
                    if len(self.buffer) > 4096:
                        self.buffer = b''
                        raise RuntimeError('Serial response exceeded buffer limit')
                    if b'\n' not in self.buffer:
                        continue
                raw, self.buffer = self.buffer.split(b'\n', 1)
                line = raw.decode('ascii', errors='replace').strip()
                if not line.startswith('$%d ' % seq):
                    continue
                parts = line.split(' ', 2)
                if len(parts) < 2 or parts[1].lower() != 'ok':
                    raise RuntimeError('Firmware rejected command: '+line)
                return parts[2] if len(parts) == 3 else ''
            raise RuntimeError('Serial reply timed out; command is not retried')


class SwiftExecutor:
    def __init__(self, link, limits, emit, ros_now, clock=time.monotonic, tolerance=.8):
        if not finite(tolerance) or not .1 <= tolerance <= 2:
            raise ValueError('Measured arrival tolerance must be 0.1..2 mm')
        self.tolerance = tolerance
        self.link, self.limits, self.emit = link, workspace_limits(limits), emit
        self.ros_now, self.clock = ros_now, clock
        self.instance = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.owner, self.session = None, None
        self.lease_seq, self.lease_until = -1, float('-inf')
        self.last_lease_stamp = float('-inf')
        self.seq, self.seen = 0, set()
        self.active, self.pending = None, None
        self.completed_id, self.pump_applied = None, None
        self.last_motion_id, self.last_motion_target = None, None
        self.settled = 0
        self.latest, self.sample_time = None, float('-inf')
        self.fault, self.latched = None, False
        self.segment_until, self.action_until = float('inf'), float('inf')
        self.path, self.index, self.sent = [], 0, False
        self.worker_lock = threading.Lock()

    def identify(self):
        if self.link.query('P2201') != 'VSwiftPro' or self.link.query('P2203') != 'V3.2.0':
            raise RuntimeError('Only verified SwiftPro firmware 3.2.0 protocol is supported')

    def stop(self, reason):
        with self.lock:
            self.latched, self.fault = True, str(reason)
            self.pending = None
        # Never wait for worker_lock/read_lock; a blocked command cannot block stop.
        self.link.stop()

    def heartbeat(self, data):
        with self.lock:
            if not self.latched and (self.active is not None or self.pending is not None) and self.clock() >= self.lease_until:
                self.stop('Active executor lease expired before renewal')
                raise RuntimeError('Active executor lease expired before renewal')
            if (not isinstance(data, dict) or data.get('driver_id') != self.instance
                    or not isinstance(data.get('owner'), str) or not data['owner']
                    or not isinstance(data.get('session'), str) or not data['session']
                    or type(data.get('seq')) is not int or data['seq'] <= self.lease_seq
                    or not finite(data.get('stamp')) or data['stamp'] <= 0 or data['stamp'] <= self.last_lease_stamp
                    or not 0 <= self.ros_now()-data['stamp'] <= .3
                    or type(data.get('armed')) is not bool):
                raise RuntimeError('Invalid, delayed or repeated driver lease')
            if self.owner is not None and (data['owner'], data['session']) != (self.owner, self.session):
                raise RuntimeError('Executor owner/session changed; restart after inspection')
            self.owner, self.session = data['owner'], data['session']
            self.lease_seq, self.last_lease_stamp = data['seq'], data['stamp']
            self.lease_until = self.clock()+max(0, .3-(self.ros_now()-data['stamp'])) if data['armed'] else float('-inf')

    def valid(self):
        with self.lock:
            if self.latched or self.clock() >= min(self.lease_until, self.segment_until, self.action_until):
                raise RuntimeError('Executor stopped or local lease expired')

    def accept(self, data):
        with self.lock:
            self.valid()
            if (not isinstance(data, dict) or data.get('driver_id') != self.instance
                    or (data.get('owner'), data.get('session')) != (self.owner, self.session)
                    or not isinstance(data.get('id'), str) or not 1 <= len(data['id']) <= 64
                    or not finite(data.get('stamp')) or not 0 <= self.ros_now()-data['stamp'] <= .3):
                raise RuntimeError('Command identity or timestamp rejected')
            if data['id'] in self.seen:
                return False  # A transport duplicate never replays a physical action.
            if self.active is not None or self.pending is not None:
                raise RuntimeError('Executor busy; commands cannot queue')
            command = dict(data)
            if command.get('action') == 'move':
                command['xyz_mm'] = pose(command.get('xyz_mm'), self.limits)
                command['goal_xyz_mm'] = pose(command.get('goal_xyz_mm', command['xyz_mm']), self.limits)
                if math.dist(command['goal_xyz_mm'], command['xyz_mm']) > 3:
                    raise RuntimeError('Measured goal reference exceeds calibrated offset limit')
            elif command.get('action') != 'pump' or type(command.get('enabled')) is not bool:
                raise RuntimeError('Unsupported actuator command')
            elif not command['enabled']:
                reference = command.get('support_xyz_mm')
                if (not self.last_motion_id or command.get('after_motion_id') != self.last_motion_id
                        or self.completed_id != self.last_motion_id or reference != self.last_motion_target):
                    raise RuntimeError('Pump release needs matching completed placement and support reference')
                command['support_xyz_mm'] = pose(reference, self.limits)
            self.seen.add(command['id'])
            self.pending = command
            return True

    def watchdog(self):
        with self.lock:
            active = self.active is not None or self.pending is not None
            expired = active and (self.clock() >= min(self.lease_until, self.segment_until, self.action_until)
                                  or self.clock()-self.sample_time > .3)
        if expired and not self.latched:
            self.stop('Local executor deadline or measured feedback expired')

    def sample(self):
        started = self.clock()
        stamp = self.ros_now()  # Acquisition start, not publication time.
        powered = bit(self.link.query('P2234'))
        attached = [bit(self.link.query('M2203 N%d' % i)) for i in range(3)]
        angles = triple(self.link.query('P2200'), 'BLR')
        xyz = triple(self.link.query('M2221 B%.2f L%.2f R%.2f' % tuple(angles)), 'XYZ')
        moving = bit(self.link.query('M2200'))
        if self.clock()-started > .2:
            raise RuntimeError('Sensor acquisition too slow; cannot publish fresh feedback')
        with self.lock:
            self.seq += 1
            result = dict(protocol=2, kind='measured_tcp', seq=self.seq, stamp=stamp, driver_id=self.instance,
                          xyz_mm=xyz, moving=moving, powered=powered, attached=attached,
                          completed_id=self.completed_id, pump_applied=self.pump_applied,
                          fault=self.fault, latched=self.latched, lease_owner=self.owner, lease_session=self.session,
                          lease_armed=self.clock() < self.lease_until and not self.latched)
            self.latest, self.sample_time = result, started
        self.emit(result)
        if not powered or attached != [True]*3:
            raise RuntimeError('Power or motor hold lost')
        return result

    def step(self):
        # ROS subscribers only validate and stage one command. All queries here.
        if not self.worker_lock.acquire(False):
            raise RuntimeError('Only one executor worker is allowed')
        try:
            measured = self.sample()
            if self.latched:
                return
            with self.lock:
                if self.pending is not None:
                    self.active, self.pending = self.pending, None
                    self.action_until = self.clock()+120
                    self.path, self.index, self.sent = [], 0, False
                    self.settled = 0
                cmd = self.active
            if cmd is None:
                return
            self.valid()
            if not self.path and cmd['action'] == 'move':
                if measured['moving']:
                    raise RuntimeError('Arm must be stopped before accepting a new path')
                origin = pose(measured['xyz_mm'], self.limits)
                planned = waypoints(origin, cmd['xyz_mm'])
                # Entire straight path rejected before the first G0. Refresh
                # physical feedback during long preflights, without moving.
                for point in [origin]+planned:
                    self.valid()
                    if self.link.query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(point)) != 'V1':
                        raise RuntimeError('Firmware rejected a path waypoint')
                    if self.clock()-self.sample_time > .08:
                        check = self.sample()
                        if check['moving'] or math.dist(check['xyz_mm'], origin) > 2:
                            raise RuntimeError('Arm moved during path preflight')
                self.path = planned
            if cmd['action'] == 'pump':
                if measured['moving']:
                    raise RuntimeError('Pump command requires stationary arm')
                if not cmd['enabled']:
                    reference = cmd['support_xyz_mm']
                    if (self.completed_id != cmd['after_motion_id']
                            or abs(measured['xyz_mm'][2]-reference[2]) > .8
                            or math.dist(measured['xyz_mm'][:2], reference[:2]) > 2):
                        raise RuntimeError('Placement moved outside support tolerance; retain vacuum')
                self.valid()
                self.link.query('M2231 V%d' % cmd['enabled'])
                self.pump_applied = cmd['enabled']  # Acknowledged output, NOT suction evidence.
                self.finish(cmd)
                return
            if self.sent:
                if measured['moving']:
                    self.settled = 0
                    return
                target = cmd['goal_xyz_mm'] if self.index == len(self.path)-1 else self.path[self.index]
                self.settled = self.settled+1 if math.dist(measured['xyz_mm'], target) <= self.tolerance else 0
                if self.settled < 3:
                    return  # A stale stopped flag is not arrival; deadline still runs.
                self.index += 1
                self.sent = False
                self.settled = 0
                if self.index == len(self.path):
                    self.finish(cmd)
                    return
            self.valid()
            if math.dist(measured['xyz_mm'], self.path[self.index]) > 3.1:
                raise RuntimeError('Next segment exceeds measured 3 mm step; no retry')
            self.segment_until = self.clock()+4
            # Descent 2 mm/s, other moves 5 mm/s; each command remains <=3 mm.
            feed = 120 if self.path[self.index][2] < measured['xyz_mm'][2]-.1 else 300
            self.link.query('G0 X%.2f Y%.2f Z%.2f F%d' % tuple(self.path[self.index]+[feed]))
            self.sent = True
        except Exception as exc:
            if not self.latched:
                self.stop(str(exc))
            raise
        finally:
            self.worker_lock.release()

    def finish(self, command):
        with self.lock:
            self.valid()
            self.completed_id = command['id']
            if command['action'] == 'move':
                self.last_motion_id = command['id']
                self.last_motion_target = list(command['goal_xyz_mm'])
            self.active = None
            self.action_until = self.segment_until = float('inf')
