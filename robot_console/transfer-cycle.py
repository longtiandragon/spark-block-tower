"""Supervised single-block A->B transfer using accepted course/site settings.

This commissioning path has no navigation or tower motion. All TCP positions
are preflighted; no pump-off cleanup is allowed when supported placement is
uncertain. A successful sequence still needs an actual block observation.
"""
import json
import math
import threading
import time
from pathlib import Path
import yaml

BASE = Path(__file__).resolve().parent
SOURCE = [174.73, 18.56, -45.25]
DESTINATION = [275.0, 18.56, -45.25]
CLEARANCE_Z = 20.0


def supported_feedback_reference(target):
    profile = BASE/'private'/'stationary-pickup.confirmed.yaml'
    if not profile.exists():
        return target
    data = yaml.safe_load(profile.read_text())
    placement = data.get('placement_parameters', {})
    if placement.get('support_operator_confirmed') is not True:
        return target
    command = placement.get('command_xyz')
    reference = placement.get('supported_feedback_reference_xyz')
    if (data.get('block_size_mm') != [100, 100, 100]
            or not isinstance(command, list) or not isinstance(reference, list)
            or len(command) != 3 or len(reference) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in command+reference)
            or math.dist(command, target) > .01 or math.dist(reference, target) > 3):
        raise RuntimeError('Supported feedback reference does not match this station/geometry')
    return reference


def interpolate(start, target):
    count = max(1, int(math.ceil(math.dist(start, target)/3.0)))
    return [[a+(b-a)*i/count for a, b in zip(start, target)] for i in range(1, count+1)]


def route(start):
    return [('clear_source', [start[0], start[1], CLEARANCE_Z], 300),
            ('align_pickup', SOURCE[:2]+[CLEARANCE_Z], 300),
            ('pickup_height', SOURCE, 120),
            ('raise_block', SOURCE[:2]+[CLEARANCE_Z], 300),
            ('translate_block', DESTINATION[:2]+[CLEARANCE_Z], 300),
            ('placement_height', DESTINATION, 120),
            ('exit_up', DESTINATION[:2]+[CLEARANCE_Z], 300),
            ('observation_pose', [180., -60., 115.], 300)]


def preflight(start, query):
    previous = start
    for phase, target, feed in route(start):
        for p in [previous]+interpolate(previous, target):
            if not (90 <= p[0] <= 290 and -65 <= p[1] <= 210 and -60 <= p[2] <= 150):
                raise RuntimeError('Transfer path outside bounds')
            if query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(p)) != 'V1':
                raise RuntimeError('Unreachable transfer path: '+phase)
        previous = target


def run(preview, query, status, halt, xyz, send_bytes):
    state = status()
    if state['power'] != 'V1' or state['moving'] != 'V0' or state['attached'] != ['V1']*3:
        raise RuntimeError('Requires powered, attached and stationary arm')
    start = xyz()
    if not preview and not (155 <= start[0] <= 190 and 0 <= start[1] <= 40 and -51 <= start[2] <= 30):
        raise RuntimeError('Source pose changed; do not replay transfer')
    preflight(start, query)
    if preview:
        tower = []
        for layer in range(3):
            down = DESTINATION[:2]+[DESTINATION[2]+100*layer]
            above = down[:2]+[down[2]+15]
            reachable = query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(down)) == 'V1'
            above_reachable = query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(above)) == 'V1'
            tower.append({'layer': layer, 'release_xyz': down, 'release_ik_reachable': reachable,
                          'above_15mm_xyz': above, 'above_15mm_ik_reachable': above_reachable,
                          'inside_current_tested_z_envelope': above[2] <= 150,
                          'physical_layer_verified': False})
        closer = []
        for x in (260., 250., 240., 225.):
            upper = [x, DESTINATION[1], DESTINATION[2]+200+15]
            closer.append({'base_xy': [x, DESTINATION[1]], 'third_layer_above_xyz': upper,
                           'third_layer_above_ik_reachable': query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(upper)) == 'V1',
                           'physical_path_verified': False})
        return {'reachable': True, 'source': SOURCE, 'destination': DESTINATION,
                'source_to_destination_mm': math.dist(SOURCE, DESTINATION),
                'clearance_z': CLEARANCE_Z, 'motion_sent': False, 'tower_ik_only': tower,
                'closer_base_candidates_ik_only': closer}
    if (BASE/'private'/'transfer.cancel').exists():
        raise RuntimeError('Transfer cancellation guard is set')
    with (BASE/'private'/'transfer.started').open('x') as marker:
        marker.write(json.dumps({'started_at': time.time(), 'source': SOURCE, 'destination': DESTINATION}))
    pump_may_be_on = False
    phases = []
    outcome = {'ok': False, 'source': SOURCE, 'destination': DESTINATION, 'phases': phases,
               'physical_transfer_observation_required': True}

    def progress(phase, details=None):
        data = {'phase': phase, 'pump_command_may_be_on': pump_may_be_on,
                'wall_time': time.time(), 'details': details}
        (BASE/'transfer-progress.json').write_text(json.dumps(data, indent=2))
        print(json.dumps({'transfer_progress': data}), flush=True)

    def move(target, feed, phase, release=False):
        # A new stop must finish its buffer-discard window before fresh motion.
        query('G4 P800')
        actual_start = xyz()
        progress(phase, {'start': actual_start, 'target': target})
        for waypoint in interpolate(actual_start, target):
            if (BASE/'private'/'transfer.cancel').exists():
                raise RuntimeError('Transfer cancelled; retain suction')
            if query('P2234') != 'V1':
                raise RuntimeError('Power lost during transfer')
            fired = threading.Event()

            def timed_stop():
                fired.set()
                send_bytes(b'M410\nM2000\n')

            timer = threading.Timer(4, timed_stop)
            timer.start()
            try:
                query('G0 X%.2f Y%.2f Z%.2f F%d' % tuple(waypoint+[feed]))
                query('M400')
                while query('M2200') != 'V0':
                    if fired.is_set():
                        raise RuntimeError('Transfer segment timed out')
                    time.sleep(.02)
                if fired.is_set():
                    raise RuntimeError('Transfer segment incomplete or timed out')
            finally:
                timer.cancel()
                timer.join()
        halt()
        measured = xyz()
        if math.dist(measured, target) > 3:
            raise RuntimeError('Transfer endpoint differs from target')
        reference = supported_feedback_reference(target) if release else target
        if release and (abs(measured[2]-reference[2]) > .8 or math.dist(measured[:2],reference[:2]) > 2):
            raise RuntimeError('Supported placement height unconfirmed; retain suction')
        result = {'phase': phase, 'target': target, 'measured': measured,
                  'supported_feedback_reference': reference if release else None}
        phases.append(result)
        progress(phase+'_complete', result)

    try:
        halt()
        for phase, target, feed in route(start):
            if phase == 'raise_block':
                pump_may_be_on = True
                query('M2231 V1')
                progress('suction_enabled')
                time.sleep(1.5)
            move(target, feed, phase, release=(phase == 'placement_height'))
            if phase == 'placement_height':
                query('M2231 V0')
                pump_may_be_on = False
                progress('released_at_supported_height')
                time.sleep(1)
        outcome['ok'] = True
        return outcome
    except Exception as exc:
        outcome['error'] = str(exc)
        raise
    finally:
        send_bytes(b'M410\nM2000\n')
        outcome['pump_command_may_be_on'] = pump_may_be_on
        (BASE/'transfer-result.json').write_text(json.dumps(outcome, indent=2))
        progress('complete' if outcome['ok'] else 'stopped_requires_attention', outcome)
