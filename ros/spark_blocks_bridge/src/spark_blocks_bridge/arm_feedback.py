"""Measured TCP feedback, independent of ROS. Never accept commanded positions."""
import math
import threading
import time
from .safety import finite


class ArmFeedback:
    def __init__(self, max_age=.3, tolerance=2., clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock, self.max_age, self.tolerance = clock, max_age, tolerance
        self.seq, self.stamp, self.received = -1, -1., float('-inf')
        self.sample = None
        self.driver_id = None
        self.command_id = None
        self.target, self.after, self.count = None, float('inf'), 0

    def begin(self, target, ros_now, command_id=None):
        with self.lock:
            self.target, self.after, self.count = list(target), ros_now, 0
            self.command_id = command_id

    def update(self, data, ros_now):
        with self.lock:
            xyz = data.get('xyz_mm') if isinstance(data, dict) else None
            if (not isinstance(data, dict) or type(data.get('protocol')) is not int or data['protocol'] != 2
                    or data.get('kind') != 'measured_tcp'
                    or not isinstance(data.get('driver_id'), str) or not 1 <= len(data['driver_id']) <= 64
                    or (self.driver_id is not None and data['driver_id'] != self.driver_id)
                    or type(data.get('seq')) is not int or data['seq'] <= self.seq
                    or not finite(data.get('stamp')) or data['stamp'] <= 0 or data['stamp'] <= self.stamp
                    or not finite(ros_now) or not 0 <= ros_now-data['stamp'] <= self.max_age
                    or not isinstance(xyz, list) or len(xyz) != 3 or not all(finite(v) for v in xyz)
                    or type(data.get('moving')) is not bool or data.get('powered') is not True
                    or not isinstance(data.get('attached'), list) or len(data['attached']) != 3
                    or any(v is not True for v in data['attached'])):
                self.sample, self.count = None, 0
                return False
            self.driver_id = data['driver_id']
            now = self.clock()
            continuous = now-self.received <= self.max_age
            self.seq, self.stamp, self.received = data['seq'], data['stamp'], now
            self.sample = dict(data, xyz_mm=list(xyz))
            at_target = (self.target is not None and self.stamp > self.after
                         and (self.command_id is None or self.command_id == data.get('completed_id'))
                         and not data.get('latched') and not data.get('fault')
                         and not data['moving'] and math.dist(xyz, self.target) <= self.tolerance)
            self.count = (self.count+1 if continuous else 1) if at_target else 0
            return True

    def fresh(self):
        with self.lock:
            return self.sample is not None and self.clock()-self.received <= self.max_age

    def reached(self):
        with self.lock:
            return self.fresh() and self.count >= 3

    def stopped(self, after=float('-inf')):
        with self.lock:
            return self.fresh() and self.stamp > after and not self.sample['moving']

    def position(self):
        with self.lock:
            if not self.fresh():
                raise RuntimeError('Measured arm feedback stale or invalid')
            return list(self.sample['xyz_mm'])

    def available(self):
        with self.lock:
            return self.fresh() and not self.sample.get('latched') and not self.sample.get('fault')

    def applied(self, command_id, enabled):
        with self.lock:
            return (self.available() and self.sample.get('completed_id') == command_id
                    and self.sample.get('pump_applied') is enabled and not self.sample['moving'])
