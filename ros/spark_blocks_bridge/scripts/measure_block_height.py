#!/usr/bin/env python3
"""Read-only RGB-D top-height measurement; does not command arm or pump."""
import argparse
import json
import threading
import time
from collections import deque
from pathlib import Path
import cv2
import numpy as np
import rospy
import message_filters
from cv_bridge import CvBridge
from sensor_msgs.msg import Image, CameraInfo
from spark_blocks_bridge.depth_height import measure_height


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roi', nargs=4, type=float, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    a, b, c, d = args.roi
    if not 0 <= a < c <= 1 or not 0 <= b < d <= 1:
        raise ValueError('Normalized ROI required')
    rospy.init_node('blocks_height_measurement', anonymous=True, disable_signals=True)
    if rospy.get_param('/camera/driver/depth_registration', False) is not True:
        raise RuntimeError('Hardware depth-to-RGB registration must be enabled and checked')
    # This installed driver leaves depth_registered/camera_info in the old
    # depth frame even after hardware registration. Registered depth pixels
    # are now in the RGB frame; use matching RGB intrinsics, not that stale K.
    info = rospy.wait_for_message('/camera/rgb/camera_info', CameraInfo, timeout=5)
    bridge, rows, stamps = CvBridge(), [], set()
    pairs, condition = deque(maxlen=2), threading.Condition()
    def paired(rgb, depth):
        with condition:
            pairs.append((rgb, depth))
            condition.notify_all()
    rgb_sub = message_filters.Subscriber('/camera/rgb/image_raw', Image, queue_size=1)
    depth_sub = message_filters.Subscriber('/camera/depth_registered/image_raw', Image, queue_size=1)
    sync = message_filters.ApproximateTimeSynchronizer([rgb_sub, depth_sub], 10, .04)
    sync.registerCallback(paired)
    for _ in range(10):
        deadline = time.monotonic()+5
        with condition:
            while not pairs:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('No synchronized RGB/depth pairs within 40 ms')
                condition.wait(remaining)
            rgb, depth = pairs.pop()
            pairs.clear()
        if (rgb.header.frame_id != depth.header.frame_id or info.header.frame_id != rgb.header.frame_id
                or depth.encoding != '32FC1' or (rgb.width, rgb.height) != (depth.width, depth.height)
                or (info.width, info.height) != (rgb.width, rgb.height)):
            raise RuntimeError('RGB/depth registration or metric encoding not established')
        now = rospy.Time.now().to_sec()
        if (any(not 0 <= now-m.header.stamp.to_sec() <= .3 for m in (rgb, depth))
                or abs((rgb.header.stamp-depth.header.stamp).to_sec()) > .06
                or depth.header.stamp.to_nsec() in stamps):
            raise RuntimeError('Unsynchronized/stale/duplicate RGB-D sample')
        stamps.add(depth.header.stamp.to_nsec())
        color = bridge.imgmsg_to_cv2(rgb, 'bgr8')
        metric = bridge.imgmsg_to_cv2(depth, 'passthrough')
        mask = cv2.inRange(cv2.cvtColor(color, cv2.COLOR_BGR2HSV),
                           np.array([95, 70, 60]), np.array([140, 255, 255]))
        roi = np.zeros_like(mask)
        roi[int(b*rgb.height):int(d*rgb.height), int(a*rgb.width):int(c*rgb.width)] = 255
        mask = cv2.erode(cv2.bitwise_and(mask, roi), None, iterations=5)
        result = measure_height(metric, info.K, mask)
        result['stamp'] = depth.header.stamp.to_sec()
        rows.append(result)
    heights = [r['height_mm'] for r in rows]
    if np.ptp(heights) > 5:
        raise RuntimeError('Object height not stable across independent frames')
    report = {'height_mm': float(np.median(heights)), 'frame_height_range_mm': float(np.ptp(heights)),
              'samples': rows, 'roi': args.roi, 'motion_sent': False, 'grip_verified': False,
              'note': 'Observed top height only; does not establish TCP-to-cup offset or suction seal'}
    with args.output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
