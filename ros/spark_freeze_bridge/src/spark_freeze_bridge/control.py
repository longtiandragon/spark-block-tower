"""Pure, fail-closed yaw control; all times are supplied monotonic seconds.

The caller serializes methods with a lock. No network or ROS work belongs in
this module. Source stamp age must be included when calling sensor updates.
"""

import math
from dataclasses import dataclass
from typing import Optional


def finite_number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def wrap_angle(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def scan_clearance(ranges, range_min, range_max, angle_increment, minimum_coverage=5.0):
    """Conservative minimum across the complete scan, including the rear.

Reject partial/broken scans. Positive infinity is a valid no-return beam,
represented by range_max; NaN/zero/negative returns are not clear space.
"""
    if (not ranges or not all(finite_number(x) for x in (range_min, range_max, angle_increment))
            or range_min < 0 or range_max <= range_min
            or abs(angle_increment) * (len(ranges) - 1) < minimum_coverage):
        return None
    valid = []
    for distance in ranges:
        if distance == float("inf"):
            valid.append(range_max)
        elif finite_number(distance) and range_min <= distance <= range_max and distance > 0:
            valid.append(distance)
    if len(valid) < max(1, math.ceil(len(ranges) * 0.5)):
        return None
    return min(valid)


@dataclass(frozen=True)
class Output:
    angular_z: float
    reason: str


class YawController:
    """P control in odom frame; at most 0.3 rad/s and never translates.

    The 0.6 s internal lease leaves a 0.1 s margin against the API's 0.7 s
    ceiling for a 20 Hz stop loop. This is not a hardware real-time guarantee.
    """

    def __init__(self, dry_run=True, max_speed=0.3, sensor_timeout=0.5,
                 min_clearance=0.45, tolerance=0.04, gain=0.8):
        if not all(finite_number(x) and x > 0 for x in
                   (max_speed, sensor_timeout, min_clearance, tolerance, gain)):
            raise ValueError("control parameters must be positive and finite")
        self.dry_run = bool(dry_run)
        self.max_speed = min(float(max_speed), 0.3)
        self.sensor_timeout = min(float(sensor_timeout), 0.5)
        self.min_clearance = float(min_clearance)
        self.tolerance = float(tolerance)
        self.gain = float(gain)
        self.yaw = 0.0
        self.odom_time = None
        self.scan_time = None
        self.clearance = None
        self.measured_speed = 0.0
        self.target_yaw = None
        self.command_speed = 0.0
        self.deadline = 0.0
        self.last_command_id = -1
        self.stopped_command_id = -1
        self.last_output = Output(0.0, "waiting_for_command")

    def update_odom(self, yaw, now, source_age=0.0, angular_speed=0.0):
        if (not all(finite_number(x) for x in (yaw, now, source_age, angular_speed))
                or source_age < 0):
            self.odom_time = None
            self.stop("invalid_odometry")
            return
        self.yaw = wrap_angle(yaw)
        self.odom_time = now - source_age
        self.measured_speed = angular_speed

    def update_scan(self, clearance, now, source_age=0.0):
        if (not all(finite_number(x) for x in (clearance, now, source_age))
                or clearance < 0 or source_age < 0):
            self.scan_time = None
            self.clearance = None
            self.stop("invalid_laser_scan")
            return
        self.clearance = float(clearance)
        self.scan_time = now - source_age

    @staticmethod
    def age(timestamp, now):
        # A finite JSON-safe sentinel, never Infinity/NaN on the wire.
        return 1e6 if timestamp is None else max(0.0, now - timestamp)

    def safety_fault(self, now):
        if self.dry_run:
            return "dry_run: real motion disabled"
        if self.age(self.odom_time, now) > self.sensor_timeout:
            return "odometry_missing_or_stale"
        if self.age(self.scan_time, now) > self.sensor_timeout:
            return "laser_missing_or_stale"
        if self.clearance is None or self.clearance < self.min_clearance:
            return "obstacle_too_close"
        return None

    def stop(self, reason="stop"):
        self.target_yaw = None
        self.command_speed = 0.0
        self.deadline = 0.0
        self.last_output = Output(0.0, reason)

    def accept(self, command, request_started, now):
        """Accept a response only within its ORIGINAL request's short lease.

        Same-id turn responses may renew an existing operation. Older turn
        ids cannot overwrite a newer stop. Stop overrides even an old id,
        and that stopped operation cannot restart with the same turn id.
        """
        if not isinstance(command, dict):
            self.stop("invalid_command")
            return False
        if command.get("action") == "stop":
            command_id = command.get("command_id")
            if isinstance(command_id, int) and not isinstance(command_id, bool):
                self.last_command_id = max(self.last_command_id, command_id)
            self.stopped_command_id = max(self.stopped_command_id, self.last_command_id)
            self.stop("backend_stop")
            return True
        command_id = command.get("command_id")
        target = command.get("target_yaw")
        speed = command.get("max_speed")
        lease = command.get("lease_seconds")
        if (command.get("action") != "turn"
                or not isinstance(command_id, int) or isinstance(command_id, bool)
                or command_id < self.last_command_id
                or command_id <= self.stopped_command_id
                or not all(finite_number(x) for x in (target, speed, lease, request_started, now))
                or speed <= 0 or lease <= 0 or request_started > now):
            self.stop("invalid_or_old_command")
            return False
        deadline = request_started + min(float(lease), 0.6)
        if now >= deadline:
            self.stop("response_arrived_after_lease")
            return False
        self.last_command_id = command_id
        self.target_yaw = wrap_angle(target)
        self.command_speed = min(float(speed), self.max_speed)
        self.deadline = deadline
        return True

    def step(self, now):
        fault = self.safety_fault(now)
        if fault:
            self.stop(fault)
        elif self.target_yaw is None:
            self.last_output = Output(0.0, self.last_output.reason)
        elif now >= self.deadline:
            self.stop("command_lease_expired")
        else:
            error = wrap_angle(self.target_yaw - self.yaw)
            if abs(error) <= self.tolerance:
                self.last_output = Output(0.0, "target_reached")
            else:
                speed = max(-self.command_speed, min(self.command_speed, self.gain * error))
                self.last_output = Output(speed, "turning")
        return self.last_output

    def telemetry(self, now, transport_fault=None):
        return {
            "yaw": self.yaw,
            "odom_age": self.age(self.odom_time, now),
            "scan_age": self.age(self.scan_time, now),
            "clearance": self.clearance,
            "moving": abs(self.measured_speed) > 0.025 or abs(self.last_output.angular_z) > 0,
            "fault": self.safety_fault(now) or transport_fault,
        }
