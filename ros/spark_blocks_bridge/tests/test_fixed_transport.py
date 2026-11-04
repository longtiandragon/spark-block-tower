import json
import pytest
from spark_blocks_bridge.fixed_transport import read_command


class Input:
    def __init__(self,parts):self.parts=list(parts)
    def recv(self,n):
        if not self.parts:return b''
        value=self.parts.pop(0)
        if len(value)>n:self.parts.insert(0,value[n:]);value=value[:n]
        return value


def test_three_source_command_over_old_256_byte_limit_roundtrips_fragmented():
    payload=dict(action='fixed_prepare0',run='a'*32,command='b'*32,levels=3,structure='tower3',
                 sources=[[294.9891087823114,18.95274755870244,-43.04],[281.4026133258557,159.1568203349577,-43.04],[195.84522118499476,112.06811829028028,-43.04]])
    raw=(json.dumps(payload)+'\n').encode()
    assert len(raw)>256
    for chunk in (1,64,256,1024):
        assert read_command(Input([raw[i:i+chunk] for i in range(0,len(raw),chunk)]))==payload


def test_partial_command_is_never_parsed_or_dispatched():
    with pytest.raises(ValueError,match='before newline'):read_command(Input([b'{"action":"fixed_build0"}']))
    assert read_command(Input([])) is None


def test_oversize_or_pipelined_commands_rejected():
    with pytest.raises(ValueError,match='exceeds'):read_command(Input([b'a'*9000]))
    with pytest.raises(ValueError,match='one console'):read_command(Input([b'{"action":"status"}\n{"action":"fixed_build0"}\n']))
