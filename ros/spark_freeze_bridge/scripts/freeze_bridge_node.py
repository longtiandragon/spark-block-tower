#!/usr/bin/env python3
"""ROS Noetic bridge. Hardware integration has NOT been tested.

ROS callbacks only copy observations. A dedicated 20 Hz WALL-CLOCK loop
publishes velocity; HTTP tick and JPEG encode/upload each have their own
worker and Session, so network waits cannot block the local stop loop.
"""

import math
import os
import threading
import time
from urllib.parse import urlparse

import cv2
import requests
import rospy
from cv_bridge import CvBridge
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Image, LaserScan

from spark_freeze_bridge.control import YawController, finite_number, scan_clearance
from spark_freeze_bridge.audio import AudioWorker


class FreezeBridge:
    def __init__(self):
        self.base_url = rospy.get_param("~backend_url", "http://127.0.0.1:8765").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("backend_url must be an HTTP(S) URL without embedded credentials")
        token = os.environ.get("SPARK_BRIDGE_TOKEN", "")
        if not token:
            raise ValueError("Set SPARK_BRIDGE_TOKEN to the backend's bridge token before launch")
        self.headers = {"X-Bridge-Token": token}
        self.audio = AudioWorker(self.base_url, self.headers,
            rospy.get_param("~audio_cache_dir", "~/.cache/spark-freeze/speech"),
            device=rospy.get_param("~audio_device", ""), enabled=rospy.get_param("~audio_enabled", False))
        dry_run = rospy.get_param("~dry_run", True)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a ROS boolean parameter")
        self.controller = YawController(
            dry_run=dry_run,
            max_speed=float(rospy.get_param("~max_speed", 0.3)),
            min_clearance=float(rospy.get_param("~min_clearance", 0.45)),
        )
        self.frame_rate = float(rospy.get_param("~frame_rate", 5.0))
        if not finite_number(self.frame_rate) or not 0 < self.frame_rate <= 10:
            raise ValueError("frame_rate must be > 0 and <= 10")
        self.lock = threading.Lock()
        self.image_lock = threading.Lock()
        self.done = threading.Event()
        self.transport_fault = None
        self.last_frame_delivered = None
        self.latest_image = None
        self.image_sequence = 0
        self.cv_bridge = CvBridge()
        # Dry-run does not publish to the real base at all.
        output_topic = "/spark_freeze_bridge/dry_run_cmd_vel" if dry_run else rospy.get_param("~cmd_vel_topic", "/cmd_vel")
        self.velocity = rospy.Publisher(output_topic, Twist, queue_size=1, latch=False)
        self.subscribers = [
            rospy.Subscriber(rospy.get_param("~odom_topic", "/odom"), Odometry, self.on_odom, queue_size=1),
            rospy.Subscriber(rospy.get_param("~scan_topic", "/scan"), LaserScan, self.on_scan, queue_size=1),
            rospy.Subscriber(rospy.get_param("~camera_topic", "/camera/color/image_raw"), Image,
                             self.on_image, queue_size=1, buff_size=2**24),
        ]
        rospy.on_shutdown(self.shutdown)
        self.workers = [
            threading.Thread(target=self.audio.run, name="robot-audio", daemon=True),
            threading.Thread(target=self.control_loop, name="local-yaw-watchdog", daemon=True),
            threading.Thread(target=self.tick_loop, name="backend-tick", daemon=True),
            threading.Thread(target=self.camera_loop, name="latest-jpeg-upload", daemon=True),
        ]
        for worker in self.workers:
            worker.start()
        rospy.logwarn("SPARK Freeze hardware adapter is UNVERIFIED; dry_run=%s, velocity topic=%s", dry_run, output_topic)

    @staticmethod
    def source_age(message):
        stamp = message.header.stamp.to_sec()
        age = rospy.Time.now().to_sec() - stamp
        if not finite_number(stamp) or stamp <= 0 or not finite_number(age) or age < -0.1:
            return float("nan")
        return max(0.0, age)

    def on_odom(self, message):
        q = message.pose.pose.orientation
        values = (q.x, q.y, q.z, q.w)
        norm = math.sqrt(sum(x*x for x in values))
        yaw = float("nan")
        if all(finite_number(x) for x in values) and 0.5 < norm < 1.5:
            x, y, z, w = (x / norm for x in values)
            yaw = math.atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))
        with self.lock:
            self.controller.update_odom(yaw, time.monotonic(), self.source_age(message), message.twist.twist.angular.z)

    def on_scan(self, message):
        clearance = scan_clearance(message.ranges, message.range_min, message.range_max, message.angle_increment)
        with self.lock:
            self.controller.update_scan(clearance, time.monotonic(), self.source_age(message))

    def on_image(self, message):
        with self.image_lock:
            self.latest_image = message
            self.image_sequence += 1

    def control_loop(self):
        while not self.done.is_set() and not rospy.is_shutdown():
            started = time.monotonic()
            try:
                # Serialize the last check AND publish with a stop response.
                with self.lock:
                    camera_fault = self.camera_fault(started)
                    if camera_fault:
                        self.controller.stop(camera_fault)
                    output = self.controller.step(started)
                    twist = Twist()  # ALL linear axes and non-z angular axes remain zero.
                    twist.angular.z = output.angular_z
                    self.velocity.publish(twist)
            except Exception as error:
                self.done.set()
                self.publish_stop("control_loop_failed")
                rospy.logerr("Local control loop failed: %s", type(error).__name__)
                rospy.signal_shutdown("Local control loop failed")
                return
            self.done.wait(max(0.0, 0.05 - (time.monotonic() - started)))
        self.publish_stop("control_loop_exit")

    @staticmethod
    def session():
        session = requests.Session()
        session.trust_env = False  # Do not send robot tokens through ambient HTTP proxies.
        return session

    def camera_fault(self, now):
        if self.last_frame_delivered is None or now - self.last_frame_delivered > 0.7:
            return "camera_not_delivered_or_stale"
        return None

    def tick_loop(self):
        with self.session() as session:
            while not self.done.is_set() and not rospy.is_shutdown():
                started = time.monotonic()
                try:
                    with self.lock:
                        payload = self.controller.telemetry(started, self.camera_fault(started) or self.transport_fault)
                    response = session.post(self.base_url + "/api/robot/tick", json=payload,
                                            headers=self.headers, timeout=(0.2, 0.3), allow_redirects=False)
                    if response.status_code != 200:
                        raise ValueError("tick_http_status_%s" % response.status_code)
                    command = response.json()
                    with self.lock:
                        if self.done.is_set():
                            self.controller.stop("shutdown")
                        elif self.controller.accept(command, started, time.monotonic()):
                            self.transport_fault = None
                        else:
                            self.transport_fault = "invalid_or_expired_backend_command"
                except Exception as error:
                    # Log the class only: never leak token-bearing requests.
                    self.publish_stop("backend_unavailable:%s" % type(error).__name__)
                    rospy.logwarn_throttle(5, "Backend tick failed (%s); local motion stopped", type(error).__name__)
                self.done.wait(max(0.0, 0.2 - (time.monotonic() - started)))

    def camera_loop(self):
        last_sequence = -1
        with self.session() as session:
            while not self.done.is_set() and not rospy.is_shutdown():
                started = time.monotonic()
                with self.image_lock:
                    message, sequence = self.latest_image, self.image_sequence
                if message is not None and sequence != last_sequence:
                    last_sequence = sequence
                    try:
                        age = self.source_age(message)
                        if not finite_number(age) or age > 0.5:
                            raise ValueError("stale_camera_frame")
                        frame = self.cv_bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
                        height, width = frame.shape[:2]
                        scale = min(1.0, 960.0 / max(width, height))
                        if scale < 1.0:
                            frame = cv2.resize(frame, (max(1, int(width*scale)), max(1, int(height*scale))))
                        ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                        if not ok:
                            raise ValueError("jpeg_encode_failed")
                        response = session.post(self.base_url + "/api/robot/frame", data=jpeg.tobytes(),
                                                headers=dict(self.headers, **{"Content-Type": "image/jpeg"}),
                                                timeout=(0.2, 0.4), allow_redirects=False)
                        if not 200 <= response.status_code < 300:
                            raise ValueError("frame_http_status_%s" % response.status_code)
                        with self.lock:
                            # Count image acquisition freshness, not a delayed upload response.
                            self.last_frame_delivered = started - age
                    except Exception as error:
                        # Frame loss must never silently keep a running game turning.
                        self.publish_stop("camera_upload_failed:%s" % type(error).__name__)
                        rospy.logwarn_throttle(5, "Camera frame not delivered (%s)", type(error).__name__)
                self.done.wait(max(0.0, 1.0/self.frame_rate - (time.monotonic() - started)))

    def publish_stop(self, reason):
        with self.lock:
            self.controller.stop(reason)
            self.transport_fault = reason
            try:
                self.velocity.publish(Twist())
            except Exception:
                pass  # ROS transport may already be closed during shutdown.

    def shutdown(self):
        self.done.set()
        self.publish_stop("shutdown")
        self.audio.close()
        for subscriber in self.subscribers:
            subscriber.unregister()


def main():
    rospy.init_node("spark_freeze_bridge")
    try:
        bridge = FreezeBridge()
        rospy.spin()
    except Exception as error:
        rospy.logfatal("SPARK Freeze bridge cannot start: %s", str(error))
        raise


if __name__ == "__main__":
    main()
