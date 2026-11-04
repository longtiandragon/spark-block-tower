"""Bounded empty-arm movement for the installed course's 20-point calibration.

Only fixed course/approach poses are accepted. No pump-on or release commands.
This module is loaded by the existing exclusive serial commissioning console.
"""
import math
import json
import threading
import time
from pathlib import Path

FEED = 300.0  # mm/min = 5 mm/s; vendor driver uses 10000
TARGETS = {
    'cal_pick_align': [169.73, 18.56, 115.0],
    'cal_pick_above': [169.73, 18.56, 20.0],
    'cal_center_align': [169.73, 18.56, -33.0],
    'cal_center_contact': [169.73, 18.56, -45.25],
    'cal_view_clear': [180.0, -60.0, 115.0],
    'cal_approach_0': [180.0, 0.0, 115.0],
    'cal_approach_1': [180.0, 0.0, 20.0],
    'cal_approach_2': [180.0, 200.0, 20.0],
    'cal_approach_3': [180.0, 200.0, -110.0],
}
for i in range(20):
    TARGETS['cal_point_%02d' % i] = [180.0 + i * 5, 200.0 - i * 10, -110.0]


def path(start, target):
    distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(start, target)))
    count = max(1, int(math.ceil(distance / 5.0)))
    return [[a + (b - a) * i / count for a, b in zip(start, target)] for i in range(count + 1)]


def check_path(start, target, query):
    for p in path(start, target):
        if not (90 <= p[0] <= 290 and -65 <= p[1] <= 210 and -115 <= p[2] <= 150):
            raise RuntimeError('Outside fixed calibration envelope: %r' % p)
        if query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(p)) != 'V1':
            raise RuntimeError('Firmware rejected path at %r' % p)


def run(action, query, status, halt, xyz, send_bytes):
    before = status()
    if before['power'] != 'V1' or before['moving'] != 'V0' or before['attached'] != ['V1'] * 3 or before['tip'] != 'V0':
        raise RuntimeError('Requires powered, attached, stationary arm')
    if action == 'cal_preview':
        # P2220 is only used while stationary. No halt/motion/pump commands here.
        start = xyz()
        checked = []
        for name, target in TARGETS.items():
            check_path(start, target, query)
            checked.append({'action': name, 'target': target})
            start = target
        return {'reachable': True, 'feed_mm_min': FEED, 'checked': checked, 'motion_commands_sent': False}
    if action not in TARGETS:
        raise ValueError('Unknown calibration action')
    # This route was explicitly cleared and contains no grasp operations.
    query('M2231 V0')
    halt()
    # SwiftPro 3.2.0 quick_stop discards planner blocks for 5000 ISR ticks.
    # A zero moving flag/M400 does not wait out that discard window. Dwell
    # before any new G0 so a freshly stopped arm cannot silently lose steps.
    query('G4 P800')
    start = xyz()
    target = TARGETS[action]
    if action in ('cal_center_align', 'cal_center_contact'):
        profile = Path(__file__).resolve().parent / 'private' / 'center-target.json'
        if profile.exists():
            p = json.loads(profile.read_text())
            values = [p.get('x'), p.get('y'), p.get('pickup_z'), p.get('issued_at')]
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                    or not 150 <= values[0] <= 195 or not -20 <= values[1] <= 50
                    or not -46 <= values[2] <= -43 or not 0 <= time.time()-values[3] <= 600):
                raise RuntimeError('Invalid/stale declared centering target')
            target = [values[0], values[1], values[2] + (12.25 if action == 'cal_center_align' else 0)]
    check_path(start, target, query)
    fired = threading.Event()

    def stop_deadline():
        fired.set()
        send_bytes(b'M410\nM2000\n')

    timer = None
    try:
        # Firmware G0 may block its acknowledgement while filling the planner.
        # Keep each physical command <= 5 mm, await completion, then inspect
        # power/contact before issuing the next segment. Never retry a segment.
        for waypoint in path(start, target)[1:]:
            timer = threading.Timer(4.0, stop_deadline)
            timer.start()
            query('G0 X%.2f Y%.2f Z%.2f F%.0f' % tuple(waypoint + [FEED]))
            # Firmware M2200 only describes the current stepper block and can
            # briefly report zero while queued planner work remains. M400 is
            # the firmware's actual planner/stepper completion barrier.
            query('M400')
            while query('M2200') != 'V0':
                if fired.is_set():
                    raise RuntimeError('Calibration motion timed out')
                time.sleep(.05)
            timer.cancel()
            timer.join()
            if fired.is_set():
                raise RuntimeError('Calibration deadline stop fired')
            if query('P2234') != 'V1' or query('P2233') != 'V0':
                raise RuntimeError('Power lost or tip contact during calibration')
        if fired.is_set():
            raise RuntimeError('Calibration deadline stop fired')
        halt()
        measured = xyz()
        error = math.sqrt(sum((a - b) ** 2 for a, b in zip(measured, target)))
        if error > 4.0:
            raise RuntimeError('Measured endpoint differs by %.2f mm: %r' % (error, measured))
        return {'start': start, 'target': target, 'measured_after_stop': measured,
                'endpoint_error_mm': error, 'feed_mm_min': FEED, 'deadline_fired': fired.is_set()}
    finally:
        try:
            send_bytes(b'M410\nM2000\n')
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
