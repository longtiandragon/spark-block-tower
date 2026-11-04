"""Independent latched serial stop writer. Receipt is NOT physical stop proof."""
import json
import os
import socket
import threading


class StopWriter:
    def __init__(self, port):
        self.port = port
        self.lock = threading.Lock()
        self.latched = False

    def send(self, data):
        with self.lock:
            if self.latched:
                for line in data.decode('ascii').splitlines():
                    words = line.split()
                    if words and words[0].startswith('#'):
                        words = words[1:]
                    if not words or words[0] not in {'M410', 'M2000', 'M400', 'M2200', 'M2203', 'M2221', 'M2222',
                                                    'P2200', 'P2201', 'P2203', 'P2220', 'P2231', 'P2233', 'P2234', 'G4'}:
                        raise RuntimeError('Independent stop latched; inspect scene before restarting console')
            self.port.write(data)

    def stop(self):
        with self.lock:
            # Latch even if serial write fails; never let a queued move restart.
            self.latched = True
            self.port.write(b'M410\nM2000\n')


def serve_stop(path, writer):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(str(path), 0o600)
    server.listen(2)

    def loop():
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(.5)
                try:
                    raw = connection.recv(256)
                    if json.loads(raw).get('action') != 'stop':
                        raise ValueError('Only stop is accepted on independent channel')
                    writer.stop()
                    result = {'stop_sent': True, 'latched': True, 'physical_stop_confirmed': False}
                except Exception as exc:
                    result = {'stop_sent': False, 'error': str(exc), 'physical_stop_confirmed': False}
                try:
                    connection.sendall((json.dumps(result)+'\n').encode())
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()
    return server
