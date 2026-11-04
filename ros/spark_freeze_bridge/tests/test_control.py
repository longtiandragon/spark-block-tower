"""Pure control tests; no ROS, network, image packages, or robot required."""

import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spark_freeze_bridge.control import YawController, scan_clearance


def command(command_id=1, target=1.0, lease=0.7, speed=0.3):
    return {"command_id": command_id, "action": "turn", "target_yaw": target,
            "max_speed": speed, "lease_seconds": lease}


class ControlTests(unittest.TestCase):
    def ready(self, dry_run=False, now=10.0, yaw=0.0):
        controller = YawController(dry_run=dry_run)
        controller.update_odom(yaw, now)
        controller.update_scan(2.0, now)
        return controller

    def test_dry_run_is_default_and_never_turns(self):
        controller = YawController()
        controller.update_odom(0.0, 10.0)
        controller.update_scan(2.0, 10.0)
        controller.accept(command(), 10.0, 10.0)
        self.assertEqual(controller.step(10.1).angular_z, 0.0)
        self.assertIn("dry_run", controller.last_output.reason)

    def test_speed_cannot_exceed_hard_cap(self):
        controller = self.ready()
        self.assertTrue(controller.accept(command(target=2.0, speed=50.0), 10.0, 10.0))
        self.assertEqual(controller.step(10.1).angular_z, 0.3)

    def test_turn_uses_shortest_wrapped_yaw_error(self):
        controller = self.ready(yaw=math.pi - 0.1)
        controller.accept(command(target=-math.pi + 0.1), 10.0, 10.0)
        self.assertAlmostEqual(controller.step(10.1).angular_z, 0.16)

    def test_reaching_target_stops_without_timed_rotation(self):
        controller = self.ready()
        controller.accept(command(target=1.0), 10.0, 10.0)
        controller.update_odom(0.98, 10.1)
        self.assertEqual(controller.step(10.1).angular_z, 0.0)
        self.assertEqual(controller.last_output.reason, "target_reached")

    def test_blocked_http_cannot_keep_turning_after_lease(self):
        controller = self.ready()
        controller.accept(command(lease=100), 10.0, 10.0)
        # Sensor callbacks remain healthy while HTTP makes NO further progress.
        controller.update_odom(0.1, 10.59)
        controller.update_scan(2.0, 10.59)
        self.assertNotEqual(controller.step(10.59).angular_z, 0.0)
        self.assertEqual(controller.step(10.61).angular_z, 0.0)
        self.assertEqual(controller.last_output.reason, "command_lease_expired")

    def test_slow_response_cannot_start_a_fresh_lease(self):
        controller = self.ready(now=11.0)
        self.assertFalse(controller.accept(command(), 10.0, 11.0))
        self.assertEqual(controller.step(11.0).angular_z, 0.0)

    def test_fresh_same_command_can_renew_a_turn(self):
        controller = self.ready()
        controller.accept(command(), 10.0, 10.01)
        controller.accept(command(), 10.4, 10.41)
        controller.update_odom(0.1, 10.8)
        controller.update_scan(2.0, 10.8)
        self.assertGreater(controller.step(10.8).angular_z, 0)

    def test_old_stop_overrides_turn_and_old_turn_cannot_override_new_stop(self):
        controller = self.ready()
        controller.accept(command(command_id=4), 10.0, 10.0)
        controller.accept({"action": "stop", "command_id": 2}, 10.1, 10.1)
        self.assertEqual(controller.step(10.1).angular_z, 0.0)
        controller.accept({"action": "stop", "command_id": 5}, 10.1, 10.1)
        self.assertFalse(controller.accept(command(command_id=4), 10.2, 10.2))
        self.assertEqual(controller.step(10.2).angular_z, 0.0)

    def test_stale_odom_and_stale_laser_each_stop(self):
        for missing in ("odom", "scan"):
            with self.subTest(missing=missing):
                controller = self.ready()
                controller.accept(command(), 10.4, 10.4)
                if missing == "odom":
                    controller.update_scan(2, 10.51)
                else:
                    controller.update_odom(0, 10.51)
                self.assertEqual(controller.step(10.51).angular_z, 0.0)
                self.assertIn("stale", controller.last_output.reason)

    def test_same_id_turn_cannot_restart_a_stopped_operation(self):
        controller = self.ready()
        controller.accept(command(command_id=4), 10, 10)
        controller.accept({"action": "stop", "command_id": 4}, 10.1, 10.1)
        self.assertFalse(controller.accept(command(command_id=4), 10.2, 10.2))
        self.assertEqual(controller.step(10.2).angular_z, 0)
        self.assertTrue(controller.accept(command(command_id=5), 10.3, 10.3))
        self.assertGreater(controller.step(10.3).angular_z, 0)

    def test_old_stop_also_blocks_renewal_of_the_interrupted_turn(self):
        controller = self.ready()
        controller.accept(command(command_id=4), 10, 10)
        controller.accept({"action": "stop", "command_id": 2}, 10.1, 10.1)
        self.assertFalse(controller.accept(command(command_id=4), 10.2, 10.2))
        self.assertEqual(controller.step(10.2).angular_z, 0)

    def test_old_source_stamp_not_refreshed_by_callback_arrival(self):
        controller = self.ready()
        controller.accept(command(), 10.0, 10.0)
        controller.update_odom(0, 10.1, source_age=3.0)
        self.assertEqual(controller.step(10.1).angular_z, 0.0)
        self.assertEqual(controller.last_output.reason, "odometry_missing_or_stale")

    def test_close_obstacle_or_invalid_scan_stops(self):
        for clearance in (0.2, None, float("nan")):
            with self.subTest(clearance=clearance):
                controller = self.ready()
                controller.accept(command(), 10.0, 10.0)
                controller.update_scan(clearance, 10.1)
                self.assertEqual(controller.step(10.1).angular_z, 0.0)

    def test_malformed_nonfinite_and_future_commands_fail_closed(self):
        invalid = [None, {"action": "forward"}, command(target=float("nan")),
                   command(speed=-1), command(lease=float("inf")), command(command_id=True)]
        for payload in invalid:
            with self.subTest(payload=payload):
                controller = self.ready()
                controller.accept(command(), 10, 10)
                self.assertFalse(controller.accept(payload, 10, 10.1))
                self.assertEqual(controller.step(10.1).angular_z, 0.0)
        controller = self.ready()
        self.assertFalse(controller.accept(command(), 10.5, 10.1))

    def test_network_failure_explicit_stop(self):
        controller = self.ready()
        controller.accept(command(), 10, 10)
        controller.stop("network_error")
        self.assertEqual(controller.step(10.1).angular_z, 0.0)


class ScanTests(unittest.TestCase):
    def test_rear_obstacle_is_included(self):
        ranges = [2.0] * 360
        ranges[180] = 0.2
        self.assertEqual(scan_clearance(ranges, 0.05, 10, math.pi/180), 0.2)

    def test_all_invalid_is_not_free_space(self):
        for ranges in ([float("nan")] * 360, [0.0] * 360, [-1.0] * 360, []):
            self.assertIsNone(scan_clearance(ranges, 0.05, 10, math.pi/180))

    def test_partial_scan_is_rejected(self):
        self.assertIsNone(scan_clearance([5.0] * 90, 0.05, 10, math.pi/180))

    def test_no_return_inf_has_finite_range_max(self):
        self.assertEqual(scan_clearance([float("inf")] * 360, 0.05, 10, math.pi/180), 10)


if __name__ == "__main__":
    unittest.main()
