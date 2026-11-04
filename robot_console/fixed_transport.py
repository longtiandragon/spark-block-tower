"""Bounded newline-delimited console input; never parse a partial JSON frame."""
import json


def read_command(connection,limit=8192):
    data=b''
    while b'\n' not in data:
        part=connection.recv(min(1024,limit+1-len(data)))
        if not part:
            if not data:return None
            raise ValueError('Console command disconnected before newline')
        data+=part
        if len(data)>limit:
            raise ValueError('Console command exceeds bounded frame size')
    line,trailing=data.split(b'\n',1)
    if trailing.strip():
        raise ValueError('Only one console command per connection is allowed')
    value=json.loads(line.decode('utf8'))
    if not isinstance(value,dict) or not isinstance(value.get('action'),str):
        raise ValueError('Console command must contain an action')
    return value
