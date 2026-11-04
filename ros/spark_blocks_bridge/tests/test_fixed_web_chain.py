"""Selected structure -> backend -> actual agent -> runner -> fake firmware.

All hardware and onsite confirmations are test fixtures. No sockets/ROS/robot.
"""
import hashlib
import json
import threading
import types
import pytest
from spark_freeze.fixed_tower import FixedTower
from spark_blocks_bridge.fixed_runner import FixedRunner
from test_fixed_agent import agent
from test_fixed_runner import station
from test_fixed_slots import automatic


@pytest.mark.parametrize('shape,count',[('single',1),('tower2',2),('tower3',3),('pyramid3',3)])
def test_selected_shape_traverses_real_dispatch_and_runner(agent,station,tmp_path,shape,count):
    module,a,_,_,_,_,_=agent
    original,_,serial,flags,private,calibration=station
    config=json.loads((private/'fixed-site.json').read_text())
    config['verified_structures']=[shape]  # Synthetic fixture, not a robot site file.
    (private/'fixed-site.json').write_text(json.dumps(config))
    runner=FixedRunner(private.parent,original.query,original.status,original.halt,original.xyz,original.send,calibration)
    a.base=private.parent
    # Windows open handles prevent replace-on-read; Linux robot uses atomic
    # rename. Serialize only fixture file IO, not the simulated motion worker.
    check=runner.check
    def fixture_check(command):
        with a.lock:check(command)
    runner.check=fixture_check
    def console(action,run,command,levels,structure,sources=None,staging=None):
        return {'ok':True,'details':runner.execute(action[6:],run,command,levels,structure,sources,staging)}
    a.console=console
    a.worker=types.MethodType(module.Agent.worker,a)
    task=FixedTower(tmp_path/'web',enabled=True,clock=lambda:100.)
    telemetry=dict(id=a.id,ready=True,stopped=True,fault=None,structures=[shape])
    task.poll(telemetry);task.heartbeat('o'*32)
    def act(action):
        task.control(action,task.session,task.revision,'o'*32,True,count,shape)
    act('start')
    wait=threading.Event()
    for action,i in [('prepared',0)]+[(action,i) for i in range(count) for action in ('feed','support')]:
        if True:
            if action!='prepared':act(action)
            packet=task.poll(telemetry)
            assert packet['command']['structure']==shape
            a.accept_poll(packet)
            # Repeated transport polls must not spawn a second physical worker.
            a.accept_poll(packet)
            for _ in range(1000):
                with a.lock:
                    result=a.result
                if result is not None:break
                wait.wait(.01)
            assert result is not None and result['ok'],result
            task.result(result['id'],result['command_id'],result['ok'],result['details'])
            with a.lock:a.result=None
            if action=='prepared':
                assert task.phase=='awaiting_feed' and not flags['pump']
            elif action=='feed':
                assert task.phase=='awaiting_support' and flags['pump']
                assert task.placed==i
            else:
                assert task.placed==i+1 and not flags['pump']
    assert task.phase=='completed' and runner.phase=='complete'
    assert len(a.seen)==2*count+1 and len(task.receipts)==2*count+1
    assert serial.count('M2231 V1')==count and serial.count('M2231 V0')==count
    report=task.snapshot();report['test_evidence']='synthetic_full_chain_not_hardware_acceptance'
    (tmp_path/'synthetic-chain.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


@pytest.mark.parametrize('entry',['web','debug'])
def test_fixed_automatic_entrypoints_share_agent_runner_and_evidence(agent,automatic,tmp_path,entry):
    import importlib.util
    from pathlib import Path
    module,a,_,_,_,_,_=agent
    runner,_,serial,flags,private,profile=automatic
    a.base=private.parent;a.site_hash=runner.profile_hash;a.slot_version=profile['version']
    checked=runner.check
    def serialized_check(command):
        with a.lock:checked(command)
    runner.check=serialized_check
    def console(action,run,command,levels,structure,sources=None,staging=None,**identity):
        return {'ok':True,'details':runner.execute(action[6:],run,command,levels,structure,sources,staging,**identity)}
    a.console=console;a.worker=types.MethodType(module.Agent.worker,a)
    task=FixedTower(tmp_path/'automatic-web',True,lambda:100.)
    telemetry=dict(id=a.id,ready=True,stopped=True,fault=None,structures=['tower3'],
                   sources=[s['command_xyz'] for s in profile['slots']],source_age=.1,
                   slots_ready=True,slot_version=profile['version'],profile_sha256=runner.profile_hash)
    task.poll(telemetry);task.heartbeat('o'*32)
    web=dict(action='start',session=task.session,revision=task.revision,operator='o'*32,
             checked=True,levels=3,structure='tower3',automatic=True)
    if entry=='debug':
        path=Path(__file__).parents[3]/'tools/fixed_tower_client.py'
        spec=importlib.util.spec_from_file_location('fixed_client',path)
        client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
        payload=client.start_payload(task.snapshot(),'o'*32)
        assert payload==web
    else:payload=web
    task.control(**payload)
    wait=threading.Event()
    for _ in range(4):
        packet=task.poll(telemetry)
        a.accept_poll(packet);a.accept_poll(packet)
        for _ in range(1000):
            with a.lock:result=a.result
            if result is not None:break
            wait.wait(.01)
        assert result and result['ok'],result
        task.result(result['id'],result['command_id'],True,result['details'])
        with a.lock:a.result=None
    assert task.phase=='completed' and task.placed==3
    assert runner.phase=='complete' and not flags['pump']
    assert serial.count('verified:grasp')==serial.count('verified:placement')==3
    assert len(a.seen)==4
