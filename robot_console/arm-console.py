"""Local commissioning console with one explicitly requested bounded stop test.

Keep one serial connection open so repeated diagnostics do not reset the board.
Only the robot user can reach the Unix socket inside the private directory.
"""
import json
import uuid
import os
import re
import socket
import subprocess
import sys
import time
import threading
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOCKET = str(BASE / 'private' / 'arm-console.sock')
STOP_SOCKET = str(BASE / 'private' / 'arm-stop.sock')
DEVICE = '/dev/uarm'


def client(action):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(240)
        sock.connect(STOP_SOCKET if action == 'stop' else SOCKET)
        sock.sendall((json.dumps({'action': action}) + '\n').encode())
        data = b''
        while not data.endswith(b'\n'):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        print(data.decode(), end='')


def serve():
    console_session = uuid.uuid4().hex
    import serial
    if Path(SOCKET).exists() or Path(STOP_SOCKET).exists():
        raise SystemExit('Console socket already exists; inspect owner first')
    if not Path(DEVICE).exists():
        raise SystemExit('Configured uArm device is absent; inspect USB first')
    if subprocess.run(['fuser', str(Path(DEVICE).resolve())], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0:
        raise SystemExit('Serial port already owned')
    port = serial.Serial(port=None, baudrate=115200, timeout=.05, write_timeout=.5, exclusive=True)
    port.dtr = False
    port.rts = False
    port.port = DEVICE
    port.open()
    time.sleep(5)
    startup = port.read(port.in_waiting).decode('ascii', errors='replace')
    print(json.dumps({'startup': startup}), flush=True)
    seq = 0
    from console_stop import StopWriter, serve_stop
    stop_writer = StopWriter(port)
    if (BASE/'private/fixed-hardware.lock').exists():
        stop_writer.stop()  # A deployment/restart must not clear the stop latch.
    stop_server = serve_stop(STOP_SOCKET, stop_writer)

    def send_bytes(data):
        stop_writer.send(data)

    def query(command):
        nonlocal seq
        seq += 1
        send_bytes(('#%d %s\n' % (seq, command)).encode())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            line = port.readline().decode('ascii', errors='replace').strip()
            if line.startswith('$%d ' % seq):
                print(json.dumps({'command': command, 'reply': line}), flush=True)
                if ' ok' not in line.lower():
                    raise RuntimeError(line)
                return line.split(' ', 2)[2] if len(line.split(' ', 2)) > 2 else ''
        raise RuntimeError('No matching acknowledgement: ' + command)

    def status():
        return {'power': query('P2234'), 'moving': query('M2200'),
                'pump': query('P2231'), 'tip': query('P2233'), 'controller_position': query('P2220'),
                'measured_angles': query('P2200'),
                'attached': [query('M2203 N%d' % i) for i in range(3)]}

    def halt():
        query('M2000')
        deadline = time.monotonic() + 3
        while query('M2200') != 'V0':
            if time.monotonic() >= deadline:
                raise RuntimeError('Firmware still reports moving; use physical stop')
            time.sleep(.05)

    def xyz():
        angles = [float(v) for v in re.findall(r'[BLR](-?\d+(?:\.\d+)?)', query('P2200'))]
        if len(angles) != 3:
            raise RuntimeError('Invalid measured angle reply')
        reply = query('M2221 B%.2f L%.2f R%.2f' % tuple(angles))
        values = [float(v) for v in re.findall(r'[XYZ](-?\d+(?:\.\d+)?)', reply)]
        if len(values) != 3:
            raise RuntimeError('Invalid position reply')
        return values

    def pump_hold_test():
        # Only for the explicitly supervised ground-supported/empty-cup test.
        if query('M2200') != 'V0' or query('P2231') != 'V0':
            raise RuntimeError('Pump test requires stationary arm and pump initially off')
        query('M2231 V1')
        try:
            time.sleep(.8)
            before = query('P2231')
            if before not in {'V1', 'V2'}:
                raise RuntimeError('Pump did not report running')
            halt()
            after = query('P2231')
            if after not in {'V1', 'V2'}:
                raise RuntimeError('Pump did not stay on across stop')
            return {'before_stop': before, 'after_stop': after, 'loaded_test': False}
        finally:
            # This action has not lifted anything; return this test to pump-off.
            query('M2231 V0')

    def pump_diagnostic():
        # Unloaded, stationary diagnostic only. Never interpret sound/current as grip.
        before = status()
        if before['power'] != 'V1' or before['moving'] != 'V0' or before['pump'] != 'V0':
            raise RuntimeError('Diagnostic requires powered, stationary, unloaded arm')
        samples = []
        off_adc = query('P2241 N9')  # FW 3.2.0 PUMP_FEEDBACK = A9
        started = time.monotonic()
        timed_out = []
        def force_off():
            timed_out.append(True)
            send_bytes(b'M2231 V0\n')
        timer = threading.Timer(2, force_off)
        timer.start()
        try:
            query('M2231 V1')
            for _ in range(5):
                time.sleep(.2)
                samples.append({'elapsed_s': time.monotonic() - started,
                                'pump': query('P2231'), 'adc_a9': query('P2241 N9')})
                if timed_out:
                    break
        finally:
            try:
                query('M2231 V0')
            finally:
                timer.join()  # redundant timed off remains armed even on query failure
        return {'off_adc_a9': off_adc, 'samples': samples, 'off_adc_a9_after': query('P2241 N9'),
                'loaded_test': False, 'grip_verified': False}

    def vertical_step(up=False, fine=False):
        before = status()
        if before['power'] != 'V1' or before['moving'] != 'V0' or before['attached'] != ['V1'] * 3:
            raise RuntimeError('Arm must be powered, attached and stationary')
        if not up and before['pump'] != 'V0':
            raise RuntimeError('Touch calibration requires unloaded arm with pump off')
        if not up and before['tip'] == 'V1':
            return {'contact': True, 'moved': False, 'status': before}
        halt()
        query('G4 P800')
        start = xyz()
        x, y, z = start
        if not (120 <= x <= 300 and abs(y) <= 100 and -60 <= z <= 150):
            raise RuntimeError('Outside the commissioning envelope')
        delta = 2 if up else (-0.5 if fine else -2)
        target = [x, y, round(z + delta, 2)]
        count = max(1, int(abs(delta) * 2))
        for i in range(count + 1):
            level = z + delta * i / count
            if query('M2222 X%.2f Y%.2f Z%.2f S0' % (x, y, level)) != 'V1':
                raise RuntimeError('Firmware rejected vertical path')
        timer_fired = []
        def deadline_stop():
            timer_fired.append(True)
            send_bytes(b'M410\nM2000\n')
        timer = threading.Timer(abs(delta) + 2, deadline_stop)
        contact = False
        timer.start()
        try:
            query('G0 X%.2f Y%.2f Z%.2f F60' % tuple(target))
            query('M400')
            # Poll the physical tip sensor while descending; never queue moves.
            deadline = time.monotonic() + abs(delta) + 2
            while True:
                if not up and query('P2233') == 'V1':
                    contact = True
                    break
                if query('M2200') == 'V0':
                    break
                if time.monotonic() >= deadline or timer_fired:
                    raise RuntimeError('Motion deadline reached')
                time.sleep(.03)
            halt()
            after = status()
            return {'start_xyz': start, 'target_xyz': target, 'contact': contact,
                    'deadline_stop': bool(timer_fired), 'after': after,
                    'physical_observation_required': True}
        finally:
            try:
                send_bytes(b'M410\nM2000\n')
            finally:
                timer.cancel()
                timer.join()

    def micro_stop():
        before = status()
        if (before['power'] != 'V1' or before['moving'] != 'V0' or before['pump'] != 'V0'
                or before['attached'] != ['V1', 'V1', 'V1']):
            raise RuntimeError('Requires stationary, attached, powered, unloaded arm')
        query('M2000')
        position = [float(v) for v in re.findall(r'[XYZ](-?\d+(?:\.\d+)?)', query('P2220'))]
        if len(position) != 3:
            raise RuntimeError('Invalid starting pose')
        x, y, z = position
        if not (120 <= x <= 300 and -100 <= y <= 100 and -50 <= z <= 150):
            raise RuntimeError('Starting pose outside bounded commissioning region')
        for dz in range(6):
            if query('M2222 X%.2f Y%.2f Z%.2f S0' % (x, y, z + dz)) != 'V1':
                raise RuntimeError('Firmware rejects the short vertical path')
        target = [x, y, round(z + 5, 2)]
        started = time.monotonic()
        stop_sent = []
        def deadline_stop():
            send_bytes(b'M410\nM2000\n')
            stop_sent.append(time.monotonic() - started)
        timer = threading.Timer(.7, deadline_stop)
        samples = []
        timer.start()
        try:
            query('G0 X%.2f Y%.2f Z%.2f F60' % tuple(target))
            for _ in range(3):
                time.sleep(.1)
                samples.append({'elapsed_s': time.monotonic() - started, 'moving': query('M2200')})
            timer.join()
            time.sleep(.2)
            query('M2000')
            after = status()
            if after['moving'] != 'V0' or after['pump'] != 'V0':
                raise RuntimeError('Stop or pump-off state unconfirmed; use physical stop')
            return {'before': before, 'start_xyz': position, 'target_xyz': target,
                    'feed_mm_min': 60, 'stop_sent_elapsed_s': stop_sent,
                    'motion_samples': samples, 'after': after,
                    'physical_observation_required': True}
        finally:
            # Always issue a stop; never release pump or motors, or move back.
            try:
                send_bytes(b'M410\nM2000\n')
            finally:
                timer.join()

    assert query('P2201') == 'VSwiftPro'
    assert query('P2203') == 'V3.2.0'
    spec = importlib.util.spec_from_file_location('calibration_motion', BASE / 'calibration-motion.py')
    calibration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibration)
    pick_spec = importlib.util.spec_from_file_location('pick_cycle', BASE / 'pick-cycle.py')
    pick_cycle = importlib.util.module_from_spec(pick_spec)
    pick_spec.loader.exec_module(pick_cycle)
    fixed_runner = None
    onsite_runner = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(SOCKET)
        os.chmod(SOCKET, 0o600)
        server.listen(2)
        print(json.dumps({'ready': True, 'status': status()}), flush=True)
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(3)
                try:
                    from fixed_transport import read_command
                    request_data = read_command(connection)
                    if request_data is None:
                        continue
                    action = request_data['action']
                    if action in ('onsite_resume_empty','onsite_resume_released'):
                        if onsite_runner is not None:raise RuntimeError('Onsite executor already initialized')
                        from onsite_tower import OnsiteTower
                        onsite_runner=OnsiteTower(BASE,query,status,halt,xyz,send_bytes)
                        details=getattr(onsite_runner,'resume_released' if action=='onsite_resume_released' else 'resume_empty')(request_data['run'],request_data['command'],request_data['previous_run'])
                        result={'ok':True,'action':action,'status':status(),'details':details,'fixed_protocol':2}
                        connection.sendall((json.dumps(result)+'\n').encode())
                        continue
                    if action in {'onsite_begin','onsite_pickup','onsite_carry','onsite_release'}:
                        if action=='onsite_begin':
                            if onsite_runner is not None:raise RuntimeError('Onsite executor already initialized')
                            from onsite_tower import OnsiteTower
                            onsite_runner=OnsiteTower(BASE,query,status,halt,xyz,send_bytes)
                            details=onsite_runner.begin(request_data['run'],request_data['command'],request_data['sources'])
                        else:
                            if onsite_runner is None or request_data['run']!=onsite_runner.run_id:raise RuntimeError('No matching onsite session')
                            details=onsite_runner.step(action[7:],request_data['command'],request_data.get('evidence'))
                        result={'ok':True,'action':action,'status':status(),'details':details,'fixed_protocol':2}
                        connection.sendall((json.dumps(result)+'\n').encode())
                        continue
                    if action=='fixed_confirmed_contact_lift':
                        if fixed_runner is None:
                            from fixed_runner import FixedRunner
                            fixed_runner=FixedRunner(BASE,query,status,halt,xyz,send_bytes)
                        details=fixed_runner.confirmed_contact_lift(request_data['run'],request_data['command'],request_data['expected'])
                        result={'ok':True,'action':action,'status':status(),'details':details,'fixed_protocol':2}
                        connection.sendall((json.dumps(result)+'\n').encode())
                        continue
                    if action=='fixed_contact_trial':
                        if fixed_runner is None:
                            from fixed_runner import FixedRunner
                            fixed_runner=FixedRunner(BASE,query,status,halt,xyz,send_bytes)
                        details=fixed_runner.contact_trial(request_data['run'],request_data['command'])
                        result={'ok':True,'action':action,'status':status(),'details':details,'fixed_protocol':2}
                        connection.sendall((json.dumps(result)+'\n').encode())
                        continue
                    if action in {'fixed_trial_preview','fixed_trial'}:
                        if fixed_runner is None:
                            from fixed_runner import FixedRunner
                            fixed_runner=FixedRunner(BASE,query,status,halt,xyz,send_bytes)
                        details=fixed_runner.trial_source(request_data['run'],request_data['command'],request_data['source'],action.endswith('_preview'))
                        result={'ok':True,'action':action,'status':status(),'details':details,'fixed_protocol':2}
                        connection.sendall((json.dumps(result)+'\n').encode())
                        continue
                    if action in {'fixed_observe_preview','fixed_observe'}:
                        if fixed_runner is None:
                            from fixed_runner import FixedRunner
                            fixed_runner=FixedRunner(BASE,query,status,halt,xyz,send_bytes)
                        details=fixed_runner.observe_scene(request_data['run'],request_data['command'],action.endswith('_preview'))
                        result={'ok':True,'action':action,'status':status(),'details':details,'fixed_protocol':2}
                        connection.sendall((json.dumps(result)+'\n').encode())
                        continue
                    # The web deployment has one motion path. Historical
                    # commissioning scripts remain evidence, not alternate RPCs.
                    if action not in {'status','stop','fixed_preview','fixed_prepare0','fixed_build0','fixed_build1','fixed_build2','fixed_release0','fixed_release1','fixed_release2'}:
                        raise ValueError('Legacy motion RPC disabled; use the shared fixed web workflow')
                    if action not in {'status', 'stop', 'release', 'attach', 'micro_stop', 'pump_hold_test', 'pump_diagnostic', 'touch_step', 'touch_fine', 'lift_step', 'grip', 'pump_off', 'cal_preview', 'pick_cycle', 'transfer_preview', 'transfer_once', 'tower_preview', 'tower_empty', 'tower_relocate', 'tower_release', 'tower_layer1', 'tower_layer2'} | set(calibration.TARGETS) | {'fixed_preview','fixed_prepare0','fixed_build0','fixed_build1','fixed_build2','fixed_release0','fixed_release1','fixed_release2'}:
                        raise ValueError('Unsupported action')
                    if action in {'release', 'attach'}:
                        if query('M2200') != 'V0' or query('P2231') != 'V0':
                            raise RuntimeError('Requires stationary unloaded arm with pump off')
                        query('M2019' if action == 'release' else 'M17')
                    elif action == 'stop':
                        halt()
                    elif action == 'grip':
                        if query('M2200') != 'V0' or query('P2233') != 'V1':
                            raise RuntimeError('Grip requires stationary verified tip contact')
                        query('M2231 V1')
                        time.sleep(.8)
                    elif action == 'pump_off':
                        if query('M2200') != 'V0':
                            raise RuntimeError('Pump release requires stationary arm')
                        query('M2231 V0')
                    if action.startswith('fixed_'):
                        if fixed_runner is None:
                            from fixed_runner import FixedRunner
                            fixed_runner = FixedRunner(BASE, query, status, halt, xyz, send_bytes)
                        details = fixed_runner.preview_slots() if action=='fixed_preview' else fixed_runner.execute(action[6:], request_data['run'], request_data['command'], request_data.get('levels',3), request_data.get('structure'), request_data.get('sources',[]), request_data.get('staging'), request_data.get('profile_sha256'), request_data.get('slot_version'))
                    elif action.startswith('tower_'):
                        ts = importlib.util.spec_from_file_location('tower_cycle', BASE / 'tower-cycle.py')
                        tower = importlib.util.module_from_spec(ts)
                        ts.loader.exec_module(tower)
                        details = tower.run(action[6:], query, status, halt, xyz, send_bytes, console_session)
                    elif action in ('transfer_preview', 'transfer_once'):
                        ts = importlib.util.spec_from_file_location('transfer_cycle', BASE / 'transfer-cycle.py')
                        transfer = importlib.util.module_from_spec(ts)
                        ts.loader.exec_module(transfer)
                        details = transfer.run(action == 'transfer_preview', query, status, halt, xyz, send_bytes)
                    else:
                        details = (calibration.run(action, query, status, halt, xyz, send_bytes) if action.startswith('cal_') else
                               pick_cycle.run(query, status, halt, xyz, send_bytes) if action == 'pick_cycle' else
                               micro_stop() if action == 'micro_stop' else
                               pump_diagnostic() if action == 'pump_diagnostic' else
                               pump_hold_test() if action == 'pump_hold_test' else
                               vertical_step() if action == 'touch_step' else
                               vertical_step(fine=True) if action == 'touch_fine' else
                               vertical_step(True) if action == 'lift_step' else None)
                    result = {'ok': True, 'action': action, 'status': status(), 'details': details, 'fixed_protocol': 2}
                except Exception as error:
                    result = {'ok': False, 'error': str(error)}
                try:
                    connection.sendall((json.dumps(result) + '\n').encode())
                except (BrokenPipeError, ConnectionResetError):
                    print(json.dumps({'client_disconnected': True, 'action_result_preserved': result}), flush=True)
                print(json.dumps(result), flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['serve']:
        serve()
    elif len(sys.argv) == 2:
        client(sys.argv[1])
    else:
        raise SystemExit('Use serve/status/stop/release/attach')
