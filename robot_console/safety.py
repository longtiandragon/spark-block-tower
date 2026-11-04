"""Pure-Python validation and lease gate, independent of ROS and HTTP."""
import math
import threading
import time


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def workspace_limits(limits):
    if (not isinstance(limits, list) or len(limits) != 3 or
            any(not isinstance(b, list) or len(b) != 2 or not all(finite(v) for v in b) or b[0] >= b[1] for b in limits)):
        raise ValueError("Three finite workspace bounds are required")
    return limits


def pose(values, limits):
    workspace_limits(limits)
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError("Arm pose requires three coordinates in mm")
    for value, bounds in zip(values, limits):
        if not finite(value) or not bounds[0] <= value <= bounds[1]:
            raise ValueError("Arm pose outside commissioned limits")
    return list(values)


def placement_reference(slot, limits):
    """A separately measured encoder reference never changes the motion target."""
    nominal = pose(slot.get('down'), limits)
    reference = slot.get('release_reference')
    if reference is None:
        return nominal
    if (not isinstance(reference, dict) or reference.get('verified') is not True
            or pose(reference.get('command_xyz_mm'), limits) != nominal):
        raise ValueError('release_reference must be verified for this exact placement command')
    measured = pose(reference.get('measured_xyz_mm'), limits)
    if math.dist(measured, nominal) > 3:
        raise ValueError('release_reference differs from nominal position by more than 3 mm')
    return measured


def validate_config(c):
    if not isinstance(c, dict):
        raise ValueError("Site configuration must be an object")
    if c.get("commissioned") is not True:
        raise ValueError("Complete site calibration and set commissioned=true")
    if 'course_profile' in c:
        from .course import apply_course_reference
        if c['course_profile'] != 'warehouse_10cm':
            raise ValueError('Unknown course_profile')
        c = apply_course_reference(c)
    if 'height_model' in c:
        from .manipulation import calibrated_heights
        c = calibrated_heights(c, c['height_model'])
    if c.get("arm_stop_verified") is not True or not str(c.get("arm_stop_service", "")).startswith("/"):
        raise ValueError("A tested arm stop service retaining vacuum is required")
    if c.get('operation_mode', 'mobile') not in ('mobile', 'stationary'):
        raise ValueError('operation_mode must be mobile or stationary')
    arm = c.get('arm_feedback', {})
    if (not isinstance(arm, dict) or arm.get('physically_verified') is not True
            or not isinstance(arm.get('topic'), str) or not arm['topic'].startswith('/')
            or not finite(arm.get('max_age_s')) or not .05 <= arm['max_age_s'] <= .5
            or not finite(arm.get('tolerance_mm')) or not .1 <= arm['tolerance_mm'] <= 2
            or not finite(arm.get('timeout_s')) or not 1 <= arm['timeout_s'] <= 120):
        raise ValueError('Verified measured arm_feedback topic, freshness, tolerance and timeout required')
    mode = c.get('verification_mode', 'operator')
    if mode not in ('operator', 'sensor'):
        raise ValueError('verification_mode must be operator or sensor')
    if mode == 'sensor':
        monitor = c.get('grasp_monitor', {})
        if (not isinstance(monitor, dict) or monitor.get('physically_verified') is not True
                or not isinstance(monitor.get('topic'), str) or not monitor['topic'].startswith('/')
                or not finite(monitor.get('max_age_s')) or not .05 <= monitor['max_age_s'] <= .5
                or not finite(monitor.get('acquire_timeout_s')) or not .2 <= monitor['acquire_timeout_s'] <= 5):
            raise ValueError('Sensor mode requires verified measured suction evidence, topic, freshness and timeout')
    limits = workspace_limits(c.get("limits"))
    for key in ("rest", "carry"):
        pose(c.get(key), limits)
    for key, maximum in (() if c.get("operation_mode") == "stationary" else (("dock_xy_tolerance_m", .025), ("dock_yaw_tolerance_rad", .035))):
        if not finite(c.get(key)) or not 0 < c[key] <= maximum:
            raise ValueError("Measured docking tolerance required (<=25 mm / 2 degrees)")
    coeff = c.get("pixel_to_arm", [])
    if not isinstance(coeff, list) or len(coeff) != 4 or not all(finite(v) for v in coeff):
        raise ValueError("pixel_to_arm requires the four measured thefile.txt coefficients")
    size = c.get("image_size", [])
    if (not isinstance(size, list) or len(size) != 2
            or any(not isinstance(v, int) or isinstance(v, bool) or not 8 <= v <= 1920 for v in size)
            or size[0]*size[1] > 1920*1080):
        raise ValueError("Calibration image_size must be [width,height]")
    sources, slots = c.get("sources", []), c.get("slots", {})
    if not isinstance(sources, list) or not 1 <= len(sources) <= 9 or not isinstance(slots, dict) or not slots:
        raise ValueError("Configure 1..9 pickup stations and at least one destination slot")
    for s in sources:
        if not isinstance(s, dict):
            raise ValueError("Each source must be an object")
        offset = s.get('pickup_xy_offset_mm', [0, 0])
        if (not isinstance(offset, list) or len(offset) != 2
                or any(not finite(v) or abs(v) > 25 for v in offset)):
            raise ValueError('Calibrated pickup_xy_offset_mm must be two finite values within 25 mm')
        if c.get("operation_mode") != "stationary" and (not isinstance(s.get("map_pose"), list) or len(s["map_pose"]) != 3 or not all(finite(v) for v in s["map_pose"])):
            raise ValueError("Pickup station requires [map_x,map_y,yaw]")
        roi = s.get("roi", [])
        if not isinstance(roi, list) or len(roi) != 4 or not all(finite(v) and 0 <= v <= 1 for v in roi) or roi[0] >= roi[2] or roi[1] >= roi[3]:
            raise ValueError("Source ROI must be normalized [left,top,right,bottom]")
        if int(roi[0]*size[0]) >= int(roi[2]*size[0]) or int(roi[1]*size[1]) >= int(roi[3]*size[1]):
            raise ValueError("Source ROI must cover at least one image pixel on each axis")
        for key in ("approach_z", "pickup_z"):
            if not finite(s.get(key)) or not limits[2][0] <= s[key] <= limits[2][1]:
                raise ValueError("Measured pickup heights must be within limits")
        if s["approach_z"] <= s["pickup_z"]:
            raise ValueError("Approach must be above pickup")
    for key, s in slots.items():
        if key not in {"%d-%d-%d" % (r, col, layer) for r in range(3) for col in range(3) for layer in range(3)}:
            raise ValueError("Invalid slot key")
        if not isinstance(s, dict):
            raise ValueError("Each slot must be an object")
        above, down = pose(s.get("above"), limits), pose(s.get("down"), limits)
        placement_reference(s, limits)
        if above[:2] != down[:2] or above[2] <= down[2]:
            raise ValueError("Each layer requires a vertical approach and retreat")
        for name in ("transfer", "retreat"):
            if not isinstance(s.get(name), list):
                raise ValueError("Explicit commissioned transfer/retreat waypoint lists required")
            for waypoint in s[name]:
                pose(waypoint, limits)
    for key, s in slots.items():
        row, col, layer = map(int, key.split('-'))
        if layer:
            lower = slots.get('%d-%d-%d' % (row, col, layer-1))
            if lower is None or s['down'][2] <= lower['down'][2]:
                raise ValueError("Each tower layer needs a calibrated lower layer and a strictly increasing release height")
    return c


class LeaseGate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.deadline = -1e9
        self.armed = False
        self.command_id = None
        self.session = None
        self.latched = False
        self.response_valid = False
        self.last_sent_at = float('-inf')

    def accept(self, response, sent_at):
        with self.lock:
            now = self.clock()
            self.response_valid = False
            # Validate the whole envelope before updating a live command or lease.
            if (not isinstance(response, dict) or not isinstance(response.get('armed'), bool)
                    or not isinstance(response.get('session'), str) or not 1 <= len(response['session']) <= 64
                    or not finite(sent_at) or not finite(now) or sent_at > now or sent_at < self.last_sent_at):
                self.stop()
                return False
            command = response.get('command')
            stop_id = response.get('stop_id')
            if (stop_id is not None and (not isinstance(stop_id, str) or not 1 <= len(stop_id) <= 64)):
                self.stop()
                return False
            if command is not None and (not isinstance(command, dict)
                    or not isinstance(command.get('id'), str) or not 1 <= len(command['id']) <= 64
                    or command.get('session', response['session']) != response['session']):
                self.stop()
                return False
            if (response['armed'] and ('command' not in response or stop_id is not None)) or (not response['armed'] and command is not None):
                self.stop()
                return False
            ttl = response.get('lease_seconds', 0)
            if response['armed'] and (not finite(ttl) or not 0 < ttl <= .65):
                self.stop()
                return False
            self.response_valid = True
            self.last_sent_at = sent_at
            if self.armed and now >= self.deadline:
                self.stop()
            session = response["session"]
            if self.session != session:
                # A new session can clear the latch only while explicitly disarmed.
                if response["armed"]:
                    self.stop()
                    return False
                self.session, self.latched = session, False
            if not response["armed"]:
                self.armed, self.command_id = False, None
                return False
            if now-sent_at >= ttl or self.latched:
                self.stop()
                return False
            self.armed, self.deadline = True, sent_at+ttl
            self.command_id = command["id"] if command else None
            return True

    def stop(self):
        with self.lock:
            self.armed, self.latched, self.command_id = False, True, None

    def valid(self, command_id=None):
        with self.lock:
            return (self.armed and not self.latched and self.clock() < self.deadline
                    and (command_id is None or command_id == self.command_id))
