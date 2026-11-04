"""Supervised 20 mm lift/set-down after explicit physical cup-contact confirmation.

No horizontal transport. On motion/communication error stop and KEEP suction.
Pump feedback on this device is untrusted; never classify zero as pump off.
"""
import math
import threading
import time


def run(query, status, halt, xyz, send_bytes):
    before = status()
    if before['power'] != 'V1' or before['moving'] != 'V0' or before['attached'] != ['V1'] * 3:
        raise RuntimeError('Requires powered, attached, stationary arm')
    halt()
    query('G4 P800')
    origin = xyz()
    if not (155 <= origin[0] <= 180 and 10 <= origin[1] <= 30 and -51 <= origin[2] <= -32):
        raise RuntimeError('Outside the supervised course pickup neighborhood')
    # The caller's pick_cycle action is the explicit onsite contact-confirmed
    # commissioning action. No production path invokes it.
    pump_may_be_on = False
    grounded = False
    results = []

    def vertical(target):
        x, y, z = target
        if query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(target)) != 'V1':
            raise RuntimeError('Unreachable micro-lift waypoint')
        fired = threading.Event()

        def emergency_stop():
            fired.set()
            send_bytes(b'M410\nM2000\n')  # NEVER switch off suction on failure

        timer = threading.Timer(3, emergency_stop)
        timer.start()
        try:
            query('G0 X%.2f Y%.2f Z%.2f F120' % (x, y, z))
            query('M400')
            if fired.is_set() or query('P2234') != 'V1' or query('M2200') != 'V0':
                raise RuntimeError('Micro-lift completion/power not confirmed')
        finally:
            timer.cancel()
            timer.join()

    try:
        pump_may_be_on = True  # acknowledgement loss must also retain suction
        query('M2231 V1')
        time.sleep(1.5)
        # Confirmed firmware stop leaves pump command unchanged; stationary
        # stop is exercised while this block is still supported on the floor.
        halt()
        query('G4 P800')
        for dz in range(1, 21):
            vertical([origin[0], origin[1], origin[2] + dz])
        halt()
        raised = xyz()
        displacement = raised[2] - origin[2]
        if not 18.5 <= displacement <= 21.5 or math.hypot(raised[0]-origin[0], raised[1]-origin[1]) > 3:
            raise RuntimeError('Measured micro-lift differs; suction retained')
        results.append({'phase': 'raised', 'xyz': raised, 'arm_vertical_delta_mm': displacement})
        time.sleep(2)
        query('G4 P800')
        for i in range(1, 21):
            vertical([raised[0], raised[1], raised[2] + (origin[2] - raised[2]) * i / 20])
        halt()
        returned = xyz()
        if abs(returned[2] - origin[2]) > .8 or math.hypot(returned[0]-origin[0],returned[1]-origin[1]) > 3:
            raise RuntimeError('Supported set-down height unconfirmed; suction retained')
        grounded = True
        results.append({'phase': 'returned_to_contact_height', 'xyz': returned})
        query('M2231 V0')
        pump_may_be_on = False
        return {'origin': origin, 'phases': results, 'returned_to_contact_height': True,
                'pump_off_command_acknowledged': True, 'physical_block_lift_observation_required': True,
                'feedback_confirms_grip': False}
    finally:
        send_bytes(b'M410\nM2000\n')
        # No blanket pump-off cleanup: an uncertain lift must retain suction.
        if pump_may_be_on:
            print('PICK_CYCLE_REQUIRES_ATTENTION: suction retained; grounded=%r' % grounded, flush=True)
