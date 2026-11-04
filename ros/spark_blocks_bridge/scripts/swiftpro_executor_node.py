#!/usr/bin/env python3
"""Exclusive, opt-in SwiftPro executor. Never launched by default blocks.launch."""
import json
import threading
import time
import subprocess
from pathlib import Path
import serial
import rosgraph
import rospy
from std_msgs.msg import String
from std_srvs.srv import Trigger, TriggerResponse
from spark_blocks_bridge.swiftpro import SerialLink, SwiftExecutor
from spark_blocks_bridge.safety import workspace_limits


def main():
    rospy.init_node('spark_blocks_arm')
    if rospy.get_param('~enabled', False) is not True:
        raise RuntimeError('Executor disabled; requires explicit reviewed commissioning launch')
    limits = workspace_limits(rospy.get_param('~limits'))
    state = rosgraph.Master(rospy.get_name()).getSystemState()
    publishers, subscribers, services = map(dict, state)
    if (any(publishers.get(t) or subscribers.get(t) for t in ('/position_write_topic', '/pump_topic'))
            or subscribers.get('/spark_blocks/arm_command')
            or publishers.get('/spark_blocks/measured_arm') or services.get('/spark_blocks/arm_stop')):
        raise RuntimeError('Existing arm driver/console must be inspected and released first')
    # exclusive=True prevents two pyserial owners; no auto-enable, homing or pump reset.
    device = rospy.get_param('~device', '/dev/uarm')
    if not Path(device).exists():
        raise RuntimeError('Configured serial device is missing')
    owner = subprocess.run(['fuser', str(Path(device).resolve())], capture_output=True, timeout=2)
    if owner.returncode != 1:
        raise RuntimeError('Serial ownership is occupied or unconfirmed')
    port = serial.Serial(port=None, baudrate=115200, timeout=.01, write_timeout=.1, exclusive=True)
    port.dtr = False
    port.rts = False
    port.port = device
    port.open()
    # This controller resets on open: observed startup/power report at 3.25 s.
    # Allow boot to finish before the first query; keep runtime query deadlines
    # unchanged and never retry actuator commands to compensate for startup.
    time.sleep(5)
    publisher = rospy.Publisher('/spark_blocks/measured_arm', String, queue_size=1)
    def emit(data):
        msg = String()
        msg.data = json.dumps(data)
        publisher.publish(msg)
    executor = SwiftExecutor(SerialLink(port), limits, emit, lambda: rospy.Time.now().to_sec(),
                             tolerance=rospy.get_param('~arm_feedback/tolerance_mm', .8))
    executor.identify()
    executor.sample()
    done = threading.Event()

    def reject(exc):
        rospy.logerr(str(exc))
        try:
            executor.stop(str(exc))
        except Exception as stop_exc:
            rospy.logerr('Serial stop failed: '+str(stop_exc))

    def callback(method, msg):
        try:
            method(json.loads(msg.data))
        except Exception as exc:
            reject(exc)

    def stop(_):
        before = rospy.Time.now().to_sec()
        try:
            executor.stop('Explicit stop request; restart only after scene inspection')
            deadline = time.monotonic()+1
            while time.monotonic() < deadline and not done.is_set():
                with executor.lock:
                    measured = executor.latest
                    if (measured and measured['stamp'] > before and not measured['moving']
                            and measured['powered'] and measured['attached'] == [True]*3
                            and time.monotonic()-executor.sample_time <= .3):
                        return TriggerResponse(success=True, message='Fresh measured stop; vacuum retained')
                done.wait(.02)
            return TriggerResponse(success=False, message='Stop sent; physical stop unconfirmed')
        except Exception as exc:
            return TriggerResponse(success=False, message=str(exc))

    rospy.Subscriber('/spark_blocks/arm_command', String, lambda msg: callback(executor.accept, msg), queue_size=1)
    rospy.Subscriber('/spark_blocks/arm_lease', String, lambda msg: callback(executor.heartbeat, msg), queue_size=1)
    rospy.Service('/spark_blocks/arm_stop', Trigger, stop)

    def watchdog():
        while not done.wait(.02):
            try:
                executor.watchdog()
            except Exception as exc:
                reject(exc)
    threading.Thread(target=watchdog, daemon=True).start()
    def shutdown():
        done.set()
        try:
            executor.stop('Node shutdown; preserve vacuum')
        except Exception:
            pass
    rospy.on_shutdown(shutdown)
    try:
        while not rospy.is_shutdown():
            try:
                executor.step()
            except Exception as exc:
                rospy.logerr_throttle(2, str(exc))
            done.wait(.04)
    finally:
        shutdown()
        port.close()


if __name__ == '__main__':
    main()
