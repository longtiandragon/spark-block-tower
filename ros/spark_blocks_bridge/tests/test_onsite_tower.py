import json
import pytest
from test_fixed_runner import station
from spark_blocks_bridge.onsite_tower import OnsiteTower


def test_complete_supervised_sequence_keeps_same_executor_and_serial_owner(station):
    old,_,commands,flags,private,calibration=station
    old.query('G0 X222 Y82 Z-22 F120')
    r=OnsiteTower(private.parent,old.query,old.status,old.halt,old.xyz,old.send,calibration)
    r.capture=lambda *a:dict(frame=None)
    r.vision.save=lambda *a:dict(verified=False)
    index=0
    def call(action):
        nonlocal index
        index+=1;command='%032x'%index
        (private/'fixed-lease.json').write_text(json.dumps(dict(run='a'*32,command=command,at=100.)))
        if action=='begin':return r.begin('a'*32,command,[[324,13,-43.04],[320,172,-43.04]])
        return r.step(action,command,'operator_confirmed')
    call('begin')
    for i in range(3):
        if i:call('pickup')
        assert r.phase=='awaiting_grasp' and flags['pump']
        with pytest.raises(RuntimeError):call('release')
        call('carry');assert r.phase=='awaiting_support' and flags['pump']
        call('release');assert not flags['pump']
    assert r.phase=='complete' and r.job==3
    assert commands.count('M2231 V1')==3 and commands.count('M2231 V0')==4


def test_empty_clearance_corrects_feedback_without_relaxing_arrival(station):
    old,_,commands,flags,private,calibration=station
    r=OnsiteTower(private.parent,old.query,old.status,old.halt,old.xyz,old.send,calibration)
    r.run_id='a'*32;r.directory=private/'fixed-runs'/r.run_id;r.directory.mkdir(parents=True)
    r.sources=[[324,13,-43.04]]
    original=r.measured
    r.measured=lambda:[original()[0],original()[1]+3.,original()[2]+2.5]
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='a'*32,command='b'*32,at=100.)))
    arrived=r._move('approach',[324.,13.,80.],120,'b'*32)
    assert arrived==[324.,13.,80.] and not flags['pump']
    records=[json.loads(v) for v in (r.directory/'events.jsonl').read_text().splitlines()]
    assert sum(v['phase']=='clearance_feedback_correction' for v in records)==1
