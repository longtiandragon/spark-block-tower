#!/usr/bin/env python3
"""Robot-local execution; HTTP cannot block the independent velocity/lease gate.

Requires move_base cmd_vel REMAPPED to /spark_blocks/nav_cmd_vel. The bridge
alone forwards it to /cmd_vel while a leased navigation command is active.
Does not start drivers, change map, or cancel goals belonging to other clients.
"""
import json
import math
import os
import threading
import time
import uuid
from urllib.parse import urlparse

import actionlib
import cv2
import numpy as np
import requests
import rosgraph
import rospy
import tf
from cv_bridge import CvBridge
from geometry_msgs.msg import Twist
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Image, LaserScan
from std_srvs.srv import Trigger
from std_msgs.msg import String

from spark_blocks_bridge.safety import LeaseGate, pose, validate_config, placement_reference
from spark_blocks_bridge.manipulation import GraspMonitor
from spark_blocks_bridge.arm_feedback import ArmFeedback
from spark_blocks_bridge.course import color_bounds


class BlocksBridge:
    def __init__(self):
        self.url = rospy.get_param('~backend_url', 'http://127.0.0.1:8768').rstrip('/')
        parsed = urlparse(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid backend URL')
        token = os.getenv('SPARK_BRIDGE_TOKEN', '')
        if len(token) < 16:
            raise ValueError('SPARK_BRIDGE_TOKEN must contain at least 16 characters')
        self.headers = {'X-Bridge-Token': token}
        self.id = uuid.uuid4().hex
        self.headers['X-Bridge-Id'] = self.id
        self.gate = LeaseGate()
        self.dry = rospy.get_param('~dry_run', True)
        if not isinstance(self.dry, bool):
            raise ValueError('dry_run must be boolean')
        self.config = rospy.get_param('~', {})
        self.config_error = None
        try:
            self.config = validate_config(self.config)
        except (ValueError, TypeError, KeyError) as exc:
            self.config_error = str(exc)
        if not self.dry and self.config_error:
            raise ValueError(self.config_error)
        self.tf = tf.TransformListener()
        self.cv = CvBridge()
        self.done = threading.Event()
        self.lock = self.gate.lock
        self.frame_lock = threading.Lock()
        self.frame = None
        self.frame_stamp = 0
        self.frame_seq = 0
        self.sensor_stamps = {}
        self.scan_age = self.odom_age = 1e6
        self.scan_time = self.odom_time = -1e9
        self.clearance = 0
        self.moving = False
        self.nav = Twist()
        self.nav_time = -1e9
        self.navigation_command_id = None
        self.owned_goal = False
        self.stop_request = None
        self.busy = False
        self.active = None
        self.seen = set()
        self.result = None
        self.fault = None
        self.stop_pending = False
        self.arm_stopped = True
        self.stop_output_failed = False
        self.watchdog_fault = None
        self.ownership_checked = -1e9
        self.ownership_ok = False
        self.carrying = False
        self.arm_last_motion_id = None
        self.arm_feedback = None
        if not self.dry:
            af = self.config['arm_feedback']
            self.arm_feedback = ArmFeedback(af['max_age_s'], af['tolerance_mm'])
        self.grasp_monitor = None
        if not self.dry and self.config.get('verification_mode') == 'sensor':
            self.grasp_monitor = GraspMonitor(self.config['grasp_monitor']['max_age_s'])
        self.client = actionlib.SimpleActionClient('/move_base', MoveBaseAction)
        if not self.dry:
            publishers = dict(rosgraph.Master(rospy.get_name()).getSystemState()[0])
            if any(publishers.get(topic) for topic in ('/cmd_vel', '/position_write_topic', '/pump_topic', '/spark_blocks/arm_command', '/spark_blocks/arm_lease')):
                raise RuntimeError('Actuators already have publishers; coordinate ownership before starting Blocks')
            rospy.wait_for_service(self.config['arm_stop_service'], timeout=2)
        # Dry-run does not create ANY real actuator publisher or service proxy.
        self.vel = rospy.Publisher('/spark_blocks/dry_run_cmd_vel' if self.dry else '/cmd_vel', Twist, queue_size=1)
        self.arm = None if self.dry else rospy.Publisher('/spark_blocks/arm_command', String, queue_size=1)
        self.pump = self.arm
        self.arm_lease = None if self.dry else rospy.Publisher('/spark_blocks/arm_lease', String, queue_size=1)
        self.arm_lease_seq = 0
        self.stop_arm = None if self.dry else rospy.ServiceProxy(self.config['arm_stop_service'], Trigger)
        rospy.Subscriber(rospy.get_param('~camera_topic', '/camera/rgb/image_raw'), Image, self.image, queue_size=1, buff_size=2**24)
        rospy.Subscriber(rospy.get_param('~scan_topic', '/scan'), LaserScan, self.scan, queue_size=1)
        rospy.Subscriber(rospy.get_param('~odom_topic', '/odom'), Odometry, self.odom, queue_size=1)
        rospy.Subscriber('/spark_blocks/nav_cmd_vel', Twist, self.navigation_velocity, queue_size=1)
        if self.arm_feedback is not None:
            rospy.Subscriber(self.config['arm_feedback']['topic'], String, self.measured_arm, queue_size=1)
        if self.grasp_monitor is not None:
            rospy.Subscriber(self.config['grasp_monitor']['topic'], String, self.grasp_feedback, queue_size=1)
        rospy.on_shutdown(self.shutdown)
        for target in (self.watchdog, self.network, self.upload_loop):
            threading.Thread(target=target, daemon=True).start()

    def stationary(self):
        return self.config.get('operation_mode', 'mobile') == 'stationary'

    def measured_arm(self, msg):
        try:
            data = json.loads(msg.data)
        except (ValueError, TypeError):
            data = None
        self.arm_feedback.update(data, rospy.Time.now().to_sec())

    def age(self, msg):
        stamp = msg.header.stamp.to_sec()
        age = rospy.Time.now().to_sec()-stamp
        return max(0, age) if stamp > 0 and math.isfinite(age) and age >= -.1 else 1e6

    def image(self, msg):
        # Same lock order as watchdog/ages: control lock before camera lock.
        with self.lock, self.frame_lock:
            if not self.new_sensor_sample('image', msg):
                return
            previous_age = max(self.age(self.frame), time.monotonic()-self.frame_stamp) if self.frame is not None else 1e6
            if self.gate.armed and previous_age > .5:
                self.stop('Camera stream interrupted; inspect before restarting')
            self.frame, self.frame_stamp = msg, time.monotonic()
            self.frame_seq += 1

    def new_sensor_sample(self, topic, msg):
        # Called under the stream's lock. Callback traffic alone is not evidence
        # of new data, especially with a paused ROS clock or a latched publisher.
        stamp = msg.header.stamp.to_sec()
        if (not math.isfinite(stamp) or stamp <= 0 or self.age(msg) > .5
                or stamp <= self.sensor_stamps.get(topic, 0)):
            return False
        self.sensor_stamps[topic] = stamp
        return True

    def scan(self, msg):
        values = [r for r in msg.ranges if math.isfinite(r) and msg.range_min <= r <= msg.range_max]
        with self.lock:
            if not self.new_sensor_sample('scan', msg):
                return
            if self.gate.armed and self.scan_age+time.monotonic()-self.scan_time > .5:
                self.stop('Scan stream interrupted; inspect before restarting')
            self.scan_age, self.scan_time = self.age(msg), time.monotonic()
            self.clearance = min(values) if values else 0

    def odom(self, msg):
        t = msg.twist.twist
        values = [t.linear.x, t.linear.y, t.angular.z]
        with self.lock:
            if not self.new_sensor_sample('odom', msg):
                return
            if self.gate.armed and self.odom_age+time.monotonic()-self.odom_time > .5:
                self.stop('Odometry stream interrupted; inspect before restarting')
            self.odom_age, self.odom_time = self.age(msg), time.monotonic()
            self.moving = not all(math.isfinite(v) and abs(v) < .015 for v in values)

    def navigation_velocity(self, msg):
        with self.lock:
            self.nav, self.nav_time = msg, time.monotonic()

    def current_pose(self):
        stamp = self.tf.getLatestCommonTime('map', 'base_footprint')
        if abs((rospy.Time.now()-stamp).to_sec()) > .5:
            raise RuntimeError('Localization TF stale')
        xyz, q = self.tf.lookupTransform('map', 'base_footprint', stamp)
        values = [xyz[0], xyz[1], tf.transformations.euler_from_quaternion(q)[2]]
        if not all(math.isfinite(v) for v in values):
            raise RuntimeError('Invalid localization')
        return values

    def ages(self):
        now = time.monotonic()
        with self.frame_lock:
            camera_age = self.age(self.frame) if self.frame is not None else 1e6
            camera_age = max(camera_age, now-self.frame_stamp)
        return camera_age, self.scan_age+now-self.scan_time, self.odom_age+now-self.odom_time

    def sensor_fault(self):
        monitor = getattr(self, 'arm_feedback', None)
        if not self.dry and (monitor is None or not monitor.available()):
            return 'Measured arm feedback stale or unavailable'
        if self.stationary() and self.moving:
            return 'Base moved in stationary workspace; inspect calibration'
        if getattr(self, 'carrying', False) and self.grasp_monitor is not None:
            fault = self.grasp_monitor.fault()
            if fault:
                return fault
        if not self.dry and (not self.ownership_ok or time.monotonic()-self.ownership_checked > 2):
            return 'Exclusive actuator ownership unconfirmed; check velocity remapping and other nodes'
        if not self.dry and (not self.arm.get_num_connections() or not self.pump.get_num_connections()):
            return 'Arm or vacuum driver disconnected; inspect before restarting'
        if max(self.ages()) > .5:
            return 'Camera/scan/odometry stale'
        if self.clearance < .45:
            return 'Obstacle clearance below 0.45 m'
        if not self.stationary():
            try:
                self.current_pose()
            except Exception:
                return 'Localization unavailable or stale'
        return None

    def stop(self, reason):
        with self.lock:
            self.gate.stop()
            self.navigation_command_id = None
            self.fault = reason
            # Each stop channel must be attempted even if another ROS output fails.
            failures = []
            try:
                self.vel.publish(Twist())
            except Exception:
                failures.append('base stop')
            if self.owned_goal and not self.dry:
                try:
                    self.client.cancel_goal()  # only this client's current goal
                    self.owned_goal = False
                except Exception:
                    failures.append('navigation cancel')
            self.stop_output_failed = bool(failures)
            if failures:
                self.fault = 'Stop output unconfirmed: ' + ', '.join(failures)
            if self.stop_pending:
                return
            self.stop_pending, self.arm_stopped = True, False
        # Service waiting is outside the watchdog and dispatch locks.
        def arm_stop():
            success = self.dry
            monitor = getattr(self, 'arm_feedback', None)
            requested_at = float('inf')
            try:
                requested_at = rospy.Time.now().to_sec()
            except Exception:
                pass
            try:
                if self.stop_arm:
                    success = bool(self.stop_arm().success)
                    deadline = time.monotonic()+1.
                    while success and monitor is not None and not monitor.stopped(requested_at) and time.monotonic() < deadline:
                        time.sleep(.02)
                    success = success and monitor is not None and monitor.stopped(requested_at)
            except Exception:
                success = False
            with self.lock:
                self.arm_stopped = success
                self.stop_pending = False
                if not success:
                    self.fault = 'Arm stop unconfirmed; use physical stop and inspect load'
        try:
            threading.Thread(target=arm_stop, daemon=True).start()
        except Exception:
            with self.lock:
                # No worker will clear this flag. Keep the stop unconfirmed but
                # allow a later explicit stop request to retry the service.
                self.stop_pending = False
                self.arm_stopped = False
                self.fault = 'Arm stop worker unavailable; use physical stop and retry after inspection'

    def watchdog(self):
        while not self.done.wait(.05) and not rospy.is_shutdown():
            try:
                with self.lock:
                    armed = self.gate.armed
                    fault = self.sensor_fault() if armed else None
                    expired = armed and not self.gate.valid()
                    if fault or expired:
                        self.stop(fault or 'Backend lease expired')
                    cmd = Twist()
                    if not self.stationary() and self.active and self.navigation_command_id == self.active['id'] and self.gate.valid(self.active['id']) and not fault and time.monotonic()-self.nav_time < .2:
                        # Never pass NaN or unbounded planner output to the base.
                        if math.isfinite(self.nav.linear.x) and math.isfinite(self.nav.angular.z):
                            cmd.linear.x = max(-.10, min(.10, self.nav.linear.x))
                            cmd.angular.z = max(-.15, min(.15, self.nav.angular.z))
                    if self.dry or self.ownership_ok or armed:
                        self.vel.publish(cmd)
                    if not self.dry:
                        self.publish_arm_lease()
            except Exception:
                with self.lock:
                    self.watchdog_fault = 'Local watchdog failed; keep stopped and restart bridge after inspection'
                    self.stop(self.watchdog_fault)
                return  # Network stays alive to report failure, but cannot dispatch.

    def session(self):
        s = requests.Session()
        s.trust_env = False
        s.headers.update(self.headers)
        return s

    def post(self, session, route, **kwargs):
        response = session.post(self.url+'/api/blocks/robot/'+route, timeout=(.2, .35), allow_redirects=False, **kwargs)
        response.raise_for_status()
        return response.json()

    def network(self):
        session = self.session()
        while not self.done.is_set() and not rospy.is_shutdown():
            try:
                if not self.dry and time.monotonic()-self.ownership_checked > .7:
                    publishers = dict(rosgraph.Master(rospy.get_name()).getSystemState()[0])
                    own = rospy.get_name()
                    # Read-only ownership check. Never kill other users' nodes.
                    self.ownership_ok = all(publishers.get(topic) == [own] for topic in
                                            ('/cmd_vel', '/spark_blocks/arm_command', '/spark_blocks/arm_lease'))
                    self.ownership_ok = self.ownership_ok and not any(publishers.get(t) for t in ('/position_write_topic', '/pump_topic'))
                    self.ownership_ok = self.ownership_ok and (self.stationary() or len(publishers.get('/spark_blocks/nav_cmd_vel', [])) == 1)
                    self.ownership_checked = time.monotonic()
                try:
                    location = self.current_pose()
                except Exception:
                    location = None
                with self.lock:
                    camera, scan, odom = self.ages()
                    ready = (not self.watchdog_fault and not self.stop_output_failed and not self.config_error and not self.dry and self.ownership_ok and (self.stationary() or location is not None) and
                             self.arm_feedback is not None and self.arm_feedback.available() and
                             bool(self.arm.get_num_connections()) and bool(self.pump.get_num_connections()) and
                             not self.stop_pending and self.arm_stopped)
                    payload = dict(bridge_id=self.id, dry_run=self.dry, ready=ready, operation_mode=self.config.get('operation_mode', 'mobile'),
                        stopped=not self.busy and not self.moving and self.arm_stopped and not self.stop_pending and
                                (self.dry or self.arm_feedback.stopped()),
                        fault=self.watchdog_fault or self.fault or self.config_error or (None if self.dry or self.ownership_ok else 'Other actuator publishers or missing navigation remap'), pose=location, camera_age=camera, scan_age=scan,
                        odom_age=odom, clearance=self.clearance,
                        slots=list(self.config.get('slots', {})), sources=len(self.config.get('sources', [])),
                        stop_ack=self.stop_acknowledgement())
                sent = time.monotonic()
                response = self.post(session, 'tick', json=payload)
                with self.lock:
                    was_armed = self.gate.armed
                    old_session = self.gate.session
                    accepted = self.gate.accept(response, sent)
                    if not self.gate.response_valid:
                        self.stop('Invalid or out-of-order backend response')
                        raise ValueError('Invalid backend response')
                    if old_session != self.gate.session and not response['armed'] and not response.get('stop_id') and self.arm_stopped and not self.busy:
                        self.fault = None
                        self.stop_request = None
                    if response.get('stop_id') and response['stop_id'] != self.stop_request:
                        self.stop_request = response['stop_id']
                        self.stop('Backend requested stop')
                    elif was_armed and not self.gate.armed:
                        self.stop('Backend disarmed')
                    command = response.get('command')
                    if accepted and command and command['id'] not in self.seen and not self.busy and not self.stop_pending and not self.watchdog_fault and not self.stop_output_failed:
                        self.seen.add(command['id'])
                        self.busy, self.active = True, command
                        try:
                            threading.Thread(target=self.execute, args=(command,), daemon=True).start()
                        except Exception:
                            self.busy, self.active = False, None
                            self.stop('Action worker unavailable; inspect before restarting')
                            raise
                    result = self.result
                if result:
                    try:
                        self.post(session, 'result', json=result)
                        with self.lock:
                            if self.result is result:
                                self.result = None
                    except requests.HTTPError as exc:
                        if exc.response.status_code in (409, 422):
                            # A rejection is only harmless if a subsequent heartbeat
                            # already showed this command had been consumed/cancelled.
                            self.reject_result(result['command_id'])
                            with self.lock:
                                if self.result is result:
                                    self.result = None
                        else:
                            raise
            except Exception:
                # Do not renew lease on transport errors. Local watchdog owns stopping.
                pass
            self.done.wait(.08)

    def reject_result(self, command_id):
        with self.lock:
            if self.gate.armed and self.gate.command_id == command_id:
                self.stop('Current action result rejected; inspect before restarting')

    def stop_acknowledgement(self):
        if (self.stop_request and not self.busy and not self.moving and self.arm_stopped
                and not self.stop_pending and not self.stop_output_failed and self.ages()[2] <= .5
                and (getattr(self, 'dry', False) or (getattr(self, 'arm_feedback', None) is not None and self.arm_feedback.stopped()))):
            return self.stop_request
        return None

    def frame_copy(self):
        with self.frame_lock:
            msg, sequence, received = self.frame, self.frame_seq, self.frame_stamp
        if msg is None or self.age(msg) > .5 or time.monotonic()-received > .5:
            raise RuntimeError('Fresh camera frame required')
        bgr = self.cv.imgmsg_to_cv2(msg, 'bgr8')
        # Conversion may be delayed while newer messages keep global sensors
        # healthy. Recheck this exact frame, not the current camera cache.
        if self.age(msg) > .5 or time.monotonic()-received > .5:
            raise RuntimeError('Fresh camera frame required after conversion')
        if [bgr.shape[1], bgr.shape[0]] != self.config.get('image_size', [640, 480]):
            raise RuntimeError('Camera resolution differs from calibration')
        return bgr, sequence

    def upload_loop(self):
        session, last = self.session(), -1
        while not self.done.wait(.25) and not rospy.is_shutdown():
            try:
                with self.lock:
                    task_session = self.gate.session
                if task_session is None:
                    continue
                frame, seq = self.frame_copy()
                if seq == last:
                    continue
                ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    self.post(session, 'frame', data=jpeg.tobytes(), headers={'Content-Type': 'image/jpeg',
                              'X-Task-Session': task_session, 'X-Frame-Sequence': str(seq)})
                    last = seq
            except Exception:
                pass

    def check(self, command):
        if command.get('operation_mode', 'mobile') != self.config.get('operation_mode', 'mobile'):
            raise RuntimeError('Task operation mode differs from commissioned workspace')
        if self.dry or self.watchdog_fault or self.stop_output_failed or not self.gate.valid(command['id']) or self.done.is_set():
            raise RuntimeError('Motion lease cancelled or dry-run')
        fault = self.sensor_fault()
        if fault:
            raise RuntimeError(fault)
        if self.stationary():
            if self.moving:
                raise RuntimeError('Base moved in stationary workspace')
            return
        if command['action'] in ('pick', 'place', 'verify_pick', 'verify_place'):
            if self.moving:
                raise RuntimeError('Base moved during manipulation/inspection')
            target = (command['home'] if command['action'] in ('place', 'verify_place')
                      else self.config['sources'][command['source']]['map_pose'])
            self.check_docking(target)

    def check_docking(self, target):
        actual = self.current_pose()
        yaw_error = abs(math.atan2(math.sin(actual[2]-target[2]), math.cos(actual[2]-target[2])))
        if math.hypot(actual[0]-target[0], actual[1]-target[1]) > self.config['dock_xy_tolerance_m'] or yaw_error > self.config['dock_yaw_tolerance_rad']:
            raise RuntimeError('Docking outside commissioned tolerance; re-localize')

    def wait(self, seconds, command):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            self.check(command)
            if self.done.wait(.05):
                raise RuntimeError('Shutdown')

    def publish_arm_lease(self):
        monitor = self.arm_feedback
        if monitor is None or not monitor.available() or not self.gate.session:
            return
        self.arm_lease_seq += 1
        msg = String()
        msg.data = json.dumps(dict(driver_id=monitor.driver_id, owner=self.id, session=self.gate.session,
            seq=self.arm_lease_seq, stamp=rospy.Time.now().to_sec(), armed=self.gate.valid()))
        self.arm_lease.publish(msg)

    def executor_message(self, command, **payload):
        # Wait for the driver to acknowledge a current lease; two ROS topics
        # have no ordering guarantee. Never send motion before lease receipt.
        end = time.monotonic()+.6
        while True:
            self.check(command)
            monitor = self.arm_feedback
            with monitor.lock:
                data = monitor.sample
                if (monitor.available() and data.get('lease_owner') == self.id
                        and data.get('lease_session') == self.gate.session and data.get('lease_armed') is True):
                    break
            if time.monotonic() >= end:
                raise RuntimeError('Executor has not acknowledged the current lease')
            self.wait(.02, command)
        message = String()
        identity = uuid.uuid4().hex
        message.data = json.dumps(dict(id=identity, driver_id=monitor.driver_id, owner=self.id,
            session=self.gate.session, stamp=rospy.Time.now().to_sec(), **payload))
        return identity, message

    def move_arm(self, xyz, command):
        xyz = pose(xyz, self.config['limits'])
        if self.arm_feedback is None or not self.arm_feedback.available():
            raise RuntimeError('Measured arm feedback required before motion')
        reference = xyz
        if command['action'] == 'place':
            slot = self.config['slots'][command['slot']]
            if xyz == slot['down']:
                reference = placement_reference(slot, self.config['limits'])
        identity, msg = self.executor_message(command, action='move', xyz_mm=xyz, goal_xyz_mm=reference)
        with self.lock:
            self.check(command)
            if self.moving:
                raise RuntimeError('Base must be stationary for arm movement')
            monitor = self.arm_feedback
            if monitor is None or not monitor.available():
                raise RuntimeError('Measured arm feedback required before motion')
            monitor.begin(reference, rospy.Time.now().to_sec(), identity)
            self.arm.publish(msg)
        deadline = time.monotonic()+self.config['arm_feedback']['timeout_s']
        try:
            while True:
                self.check(command)
                if not monitor.fresh():
                    raise RuntimeError('Measured arm feedback interrupted; retain vacuum')
                if monitor.reached():
                    self.arm_last_motion_id = identity
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError('Arm arrival timed out; retain vacuum')
                if self.done.wait(.02):
                    raise RuntimeError('Shutdown during arm motion')
        except Exception as exc:
            self.stop(str(exc))
            raise

    def vacuum(self, enabled, command):
        self.check(command)
        payload = dict(action='pump', enabled=enabled)
        if not enabled:
            reference = placement_reference(self.config['slots'][command['slot']], self.config['limits'])
            if not self.arm_last_motion_id:
                raise RuntimeError('No completed placement motion; retain vacuum')
            payload.update(support_xyz_mm=reference, after_motion_id=self.arm_last_motion_id)
        identity, msg = self.executor_message(command, **payload)
        with self.lock:
            self.check(command)
            if enabled and getattr(self, 'grasp_monitor', None) is not None:
                self.grasp_requested_at = self.grasp_monitor.begin(rospy.Time.now().to_sec())
            self.pump.publish(msg)
        deadline = time.monotonic()+2
        while not self.arm_feedback.applied(identity, enabled):
            self.check(command)
            if time.monotonic() >= deadline:
                raise RuntimeError('Pump command acknowledgement missing; do not retry or release')
            self.wait(.02, command)
        if not enabled:
            self.carrying = False
        self.wait(1.0, command)

    def grasp_feedback(self, msg):
        try:
            data = json.loads(msg.data)
        except (ValueError, TypeError):
            data = None
        self.grasp_monitor.update(data, rospy.Time.now().to_sec())

    def require_grasp(self, command):
        monitor = getattr(self, 'grasp_monitor', None)
        if monitor is None:
            return  # Operator confirms the raised block before navigate_home.
        end = time.monotonic()+self.config['grasp_monitor']['acquire_timeout_s']
        while time.monotonic() < end:
            self.check(command)
            if monitor.confirmed(self.grasp_requested_at):
                self.carrying = True
                return
            self.wait(.05, command)
        raise RuntimeError('No fresh measured suction confirmation; grasp failed or unknown; vacuum retained')

    def navigate(self, target, command):
        if not isinstance(target, list) or len(target) != 3 or not all(math.isfinite(v) for v in target):
            raise RuntimeError('Invalid navigation pose')
        if not self.client.wait_for_server(rospy.Duration(.2)):
            raise RuntimeError('move_base is unavailable')
        goal = MoveBaseGoal()
        goal.target_pose.header.frame_id = 'map'
        goal.target_pose.header.stamp = rospy.Time.now()
        goal.target_pose.pose.position.x, goal.target_pose.pose.position.y = target[:2]
        q = tf.transformations.quaternion_from_euler(0, 0, target[2])
        p = goal.target_pose.pose.orientation
        p.x, p.y, p.z, p.w = q
        with self.lock:
            self.check(command)
            # A previous goal's last planner velocity must never leak into a new goal.
            self.nav, self.nav_time = Twist(), -1e9
            # Publishing can raise after handing data to the transport. Retain
            # ownership so execute()'s stop path still attempts cancellation.
            self.owned_goal = True
            self.client.send_goal(goal)
            self.navigation_command_id = command['id']
        succeeded = False
        try:
            end = time.monotonic()+120
            while time.monotonic() < end:
                self.wait(.05, command)
                if self.client.get_state() in (2, 3, 4, 5, 8, 9):
                    with self.lock:
                        self.navigation_command_id = None
                        self.vel.publish(Twist())
                    if self.client.get_state() != 3:
                        raise RuntimeError('Navigation failed')
                    self.owned_goal = False
                    self.check_docking(target)
                    self.wait(.4, command)
                    if self.moving:
                        raise RuntimeError('Base has not settled')
                    succeeded = True
                    return
            raise RuntimeError('Navigation timeout')
        finally:
            with self.lock:
                self.navigation_command_id = None
                self.vel.publish(Twist())
                if not succeeded:
                    self.client.cancel_goal()
                    self.owned_goal = False

    def blue_center(self, source, command):
        # Reload at the beginning of each detection, so the course text file
        # actually controls color selection. A configured missing/bad file
        # fails explicitly; it never silently falls back to hardcoded HSV.
        lower_hsv, upper_hsv = color_bounds(self.config.get('color_hsv_file'))
        low, high, stable, last = None, None, 0, -1
        end = time.monotonic()+20
        while time.monotonic() < end:
            self.wait(.05, command)
            frame, seq = self.frame_copy()
            if seq == last:
                continue
            last = seq
            h, w = frame.shape[:2]
            a, b, c, d = source['roi']
            x0, y0, x1, y1 = int(a*w), int(b*h), int(c*w), int(d*h)
            hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array(lower_hsv), np.array(upper_hsv))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
            contours = [v for v in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2] if cv2.contourArea(v) >= 400]
            if len(contours) != 1:
                stable, low, high = 0, None, None
                continue
            (u, v), _, _ = cv2.minAreaRect(contours[0])
            center = (u+x0, v+y0)
            # Bound the whole observation window. Pairwise comparisons alone
            # allow a continuously drifting target to be mistaken for stable.
            next_low = tuple(min(a, b) for a, b in zip(low, center)) if low else center
            next_high = tuple(max(a, b) for a, b in zip(high, center)) if high else center
            if low is not None and max(b-a for a, b in zip(next_low, next_high)) < 2:
                low, high, stable = next_low, next_high, stable+1
            else:
                low, high, stable = center, center, 0
            if stable >= 10:
                return center
        raise RuntimeError('No unique stable blue target in source ROI; adjust approach/lighting')

    def execute(self, command):
        observation, ok = '动作序列完成，尚未验证实物结果', False
        frame_id = None
        try:
            self.check(command)
            action = command['action']
            source = self.config['sources'][command['source']]
            slot = self.config['slots'][command['slot']]
            if action == 'navigate_pick':
                self.move_arm(self.config['rest'], command)
                if not self.stationary():
                    self.navigate(source['map_pose'], command)
            elif action == 'navigate_home':
                # Backend issues this only after verify_pick/operator confirmation.
                # Never enter a travel pose immediately after an unverified pick.
                if self.config.get('verification_mode') == 'sensor':
                    self.require_grasp(command)
                self.move_arm(self.config['carry'], command)
                if not self.stationary():
                    self.navigate(command['home'], command)
            elif action == 'pick':
                self.move_arm(self.config['rest'], command)
                u, v = self.blue_center(source, command)
                a, b, c, d = self.config['pixel_to_arm']
                x, y = a*v+b, c*u+d
                dx, dy = source.get('pickup_xy_offset_mm', [0, 0])
                x, y = x+dx, y+dy
                above, down = [x, y, source['approach_z']], [x, y, source['pickup_z']]
                # Validate the whole path BEFORE moving towards the object.
                for target in (above, down, self.config['carry']):
                    pose(target, self.config['limits'])
                self.move_arm(above, command)
                self.move_arm(down, command)
                self.vacuum(True, command)
                self.require_grasp(command)
                self.move_arm(above, command)
            elif action == 'place':
                for target in slot['transfer']:
                    self.move_arm(target, command)
                self.move_arm(slot['above'], command)
                self.move_arm(slot['down'], command)
                self.vacuum(False, command)
                self.move_arm(slot['above'], command)
                for target in slot['retreat']:
                    self.move_arm(target, command)
                self.move_arm(self.config['rest'], command)
            elif action in ('verify_pick', 'verify_place'):
                before = self.frame_seq
                self.wait(.4, command)
                frame, seq = self.frame_copy()
                if seq <= before:
                    raise RuntimeError('No new observation after action')
                encoded, jpeg = cv2.imencode('.jpg', frame)
                if not encoded:
                    raise RuntimeError('Camera encode failed')
                response = self.post(self.session(), 'frame', data=jpeg.tobytes(), headers={
                    'Content-Type': 'image/jpeg', 'X-Task-Session': command['session'],
                    'X-Frame-Sequence': str(seq), 'X-Command-Id': command['id']})
                frame_id = response.get('frame_id')
                if not isinstance(frame_id, str) or len(frame_id) != 32:
                    raise RuntimeError('Inspection frame was not acknowledged')
                observation = ('已上传抓取后的新画面；请现场确认积木吸稳且已抬起。' if action == 'verify_pick'
                               else '已上传放置后的新画面；请确认目标层对齐、支撑完整，塔体稳定。')
            else:
                raise RuntimeError('Unsupported command')
            self.check(command)
            ok = True
        except Exception as exc:
            observation = str(exc)[:300]
            self.stop(observation)
        finally:
            with self.lock:
                self.busy, self.active = False, None
                self.result = dict(command_id=command['id'], bridge_id=self.id, ok=ok, observation=observation, frame_id=frame_id)

    def shutdown(self):
        self.done.set()
        self.stop('Bridge shutdown')


if __name__ == '__main__':
    rospy.init_node('spark_blocks')
    BlocksBridge()
    rospy.spin()
