"""Supervised, stationary 100 mm tower commissioning; no automatic retries."""
import hashlib
import json
import math
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
OLD = [275., 18.56, -45.25]
TOWER = [260., 18.56, -45.25]
FEED = [175., 150., -45.25]
HIGH = 169.75


def interpolate(a, b):
    n = max(1, math.ceil(math.dist(a, b)/3))
    return [[x+(y-x)*i/n for x, y in zip(a, b)] for i in range(1, n+1)]


def empty_route(start):
    return [('empty_raise', start[:2]+[HIGH], 300),
            ('empty_feed_high', FEED[:2]+[HIGH], 300),
            ('empty_tower_high', TOWER[:2]+[HIGH], 300),
            ('empty_third_height', TOWER[:2]+[154.75], 120),
            ('empty_exit_up', TOWER[:2]+[HIGH], 300),
            ('empty_exit_side', FEED[:2]+[HIGH], 300),
            ('empty_feed_above', FEED[:2]+[20.], 300)]


def carrying_route(layer):
    source = OLD if layer == 0 else FEED
    down = TOWER[:2]+[TOWER[2]+100*layer]
    clearance = max(20., down[2]+15.)
    return [('source_above', source[:2]+[20.], 300),
            ('pickup', source, 120),
            ('lift', source[:2]+[clearance], 300),
            ('carry', TOWER[:2]+[clearance], 300),
            ('support_pending', down, 120)]


def exit_route(layer):
    clearance = max(20., TOWER[2]+100*layer+15.)
    points = [('exit_up', TOWER[:2]+[clearance], 300),
              ('exit_side', FEED[:2]+[clearance], 300)]
    if layer < 2:
        points += [('feed_above', FEED[:2]+[20.], 300)]
    else:
        points += [('observer_high', [180., -60., clearance], 300),
                   ('observer', [180., -60., 115.], 300)]
    return points


def preflight(start, route, query):
    for phase, target, _ in route:
        for p in [start]+interpolate(start, target):
            if not (90 <= p[0] <= 290 and -65 <= p[1] <= 210 and -50 <= p[2] <= 175):
                raise RuntimeError('Tower envelope rejected: '+phase)
            if query('M2222 X%.2f Y%.2f Z%.2f S0' % tuple(p)) != 'V1':
                raise RuntimeError('Tower IK rejected: %s %s' % (phase, p))
        start = target


def run(action, query, status, halt, xyz, send_bytes, session_id):
    private = BASE/'private'
    statefile = private/'tower-state.json'
    state = json.loads(statefile.read_text()) if statefile.exists() else {'phase': 'new'}
    st = status()
    if st['power'] != 'V1' or st['moving'] != 'V0' or st['attached'] != ['V1']*3:
        raise RuntimeError('Requires powered, stationary, attached arm')
    start = xyz()
    if action == 'preview':
        preflight(start, empty_route(start), query)
        previous = FEED[:2]+[20.]
        for layer in range(3):
            route = carrying_route(layer)+exit_route(layer)
            preflight(previous, route, query)
            previous = route[-1][1]
        return {'motion_sent': False, 'ik_paths_passed': True, 'feed': FEED,
                'tower': TOWER, 'high': HIGH, 'physical_verified': False}
    profile = private/'stationary-pickup.confirmed.yaml'
    fingerprint = hashlib.sha256(json.dumps([OLD, TOWER, FEED, HIGH]).encode()
                                 + (profile.read_bytes() if profile.exists() else b'')).hexdigest()
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError('Live console session required')
    if state['phase'] != 'new' and (state.get('session_id') != session_id
            or state.get('calibration_fingerprint') != fingerprint
            or not 0 <= time.time()-state.get('time', 0) <= 600):
        raise RuntimeError('Tower session/calibration changed or expired; inspect scene before a new run')
    if (private/'tower.cancel').exists():
        raise RuntimeError('Tower cancel guard set')
    expected_phase = {'empty': 'new', 'relocate': 'empty_verified',
                      'layer1': 'ready_layer1', 'layer2': 'ready_layer2'}
    releasing = action == 'release'
    if releasing:
        if state['phase'] != 'support_pending':
            raise RuntimeError('No pending operator-confirmed support')
        layer = state['layer']
        if math.dist(start, state['measured']) > 1:
            raise RuntimeError('Pose changed since support observation')
        route = exit_route(layer)
        marker_name = 'release%d' % layer
    else:
        if action not in expected_phase or state['phase'] != expected_phase[action]:
            raise RuntimeError('Unexpected stage; never replay motion: '+state['phase'])
        if action != 'empty' and math.dist(start, state['measured']) > 3:
            raise RuntimeError('Starting pose changed; inspect scene')
        layer = {'relocate': 0, 'layer1': 1, 'layer2': 2}.get(action)
        route = empty_route(start) if action == 'empty' else carrying_route(layer)
        marker_name = action
    preflight(start, route, query)
    with (private/('tower-'+marker_name+'.started')).open('x') as f:
        json.dump({'time': time.time(), 'start': start}, f)
    pumping = releasing
    phases = []
    state.update(session_id=session_id, calibration_fingerprint=fingerprint, time=time.time())

    def save():
        temporary = statefile.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(statefile)

    def progress(phase, details=None):
        data = {'phase': phase, 'action': action, 'layer': layer, 'time': time.time(),
                'pump_command_may_be_on': pumping, 'details': details}
        (BASE/'tower-progress.json').write_text(json.dumps(data, indent=2))
        print(json.dumps({'tower_progress': data}), flush=True)

    def move(phase, target, feed):
        query('G4 P800')
        origin = xyz()
        # Recheck actual segment origin, including encoder discrepancy after stop.
        preflight(origin, [(phase, target, feed)], query)
        progress(phase, {'start': origin, 'target': target})
        for p in interpolate(origin, target):
            if (private/'tower.cancel').exists() or query('P2234') != 'V1':
                raise RuntimeError('Cancelled or power lost; retain suction')
            fired = threading.Event()
            def timeout():
                fired.set()
                send_bytes(b'M410\nM2000\n')
            timer = threading.Timer(4, timeout)
            timer.start()
            try:
                query('G0 X%.2f Y%.2f Z%.2f F%d' % tuple(p+[feed]))
                query('M400')
                while query('M2200') != 'V0':
                    if fired.is_set():
                        raise RuntimeError('Tower segment timeout')
                    time.sleep(.02)
                if fired.is_set():
                    raise RuntimeError('Tower segment timed out; do not retry')
            finally:
                timer.cancel()
                timer.join()
        halt()
        measured = xyz()
        if math.dist(measured, target) > 3:
            raise RuntimeError('Tower endpoint differs from target: '+str(measured))
        phases.append({'phase': phase, 'target': target, 'measured': measured})
        progress(phase+'_complete', phases[-1])
        return measured

    try:
        halt()
        if releasing:
            # Invoked only after root receives the operator's physical support confirmation.
            query('M2231 V0')
            pumping = False
            state.setdefault('support_confirmations', []).append(
                {'layer': layer, 'measured': start, 'time': time.time(), 'operator_confirmed': True})
            state['phase'] = 'exiting'
            save()
            progress('released_after_operator_support_confirmation')
            time.sleep(1)
        else:
            state['phase'] = 'running_'+action
            save()
        for phase, target, feed in route:
            if phase == 'lift':
                pumping = True
                query('M2231 V1')
                progress('suction_enabled')
                time.sleep(1.5)
            measured = move(phase, target, feed)
        state.update({'phase': ('ready_layer%d' % (layer+1) if layer < 2 else 'complete')
                       if releasing else 'empty_verified' if action == 'empty' else 'support_pending',
                      'layer': layer, 'measured': measured, 'phases': phases,
                      'pump_command_may_be_on': pumping, 'time': time.time()})
        save()
        progress(state['phase'], state)
        return state
    except Exception as exc:
        state.update({'phase': 'stopped_requires_attention', 'error': str(exc),
                      'pump_command_may_be_on': pumping, 'phases': phases})
        save()
        progress(state['phase'], state)
        raise
    finally:
        send_bytes(b'M410\nM2000\n')
