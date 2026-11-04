"""No ROS, network or hardware: exercise the actual adapter dispatch/watchdog."""
import copy
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path
import pytest


@pytest.fixture
def agent(tmp_path,monkeypatch):
    monkeypatch.setitem(sys.modules,'rospy',types.SimpleNamespace(logerr=lambda _:None))
    monkeypatch.setitem(sys.modules,'cv_bridge',types.SimpleNamespace(CvBridge=object))
    monkeypatch.setitem(sys.modules,'sensor_msgs',types.ModuleType('sensor_msgs'))
    monkeypatch.setitem(sys.modules,'sensor_msgs.msg',types.SimpleNamespace(Image=object))
    spec=importlib.util.spec_from_file_location('tested_fixed_agent',Path(__file__).parents[1]/'scripts/fixed_tower_agent.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    now=[100.];monkeypatch.setattr(m.time,'monotonic',lambda:now[0])
    a=m.Agent.__new__(m.Agent)
    a.base=tmp_path;(tmp_path/'private').mkdir()
    a.id='c'*32;a.lock=threading.RLock();a.done=threading.Event()
    a.busy=a.armed=False;a.fault=None;a.last_ack=100.;a.frame_at=100.
    a.backend_run=None;a.result=None;a.seen={};a.console_protocol=True
    a.structures=['single','tower2','tower3','pyramid3']
    calls=[];stopped=threading.Event();called=threading.Event()
    def worker(cmd):
        with a.lock:
            calls.append(cmd);a.busy=False
        called.set()
    a.worker=worker;a.send_stop=lambda:stopped.set()
    cmd=dict(id='b'*32,run='a'*32,action='build',layer=0,levels=3,structure='tower3')
    packet=dict(run='a'*32,armed=True,stop=False,command=cmd)
    return m,a,now,packet,calls,called,stopped


def test_dispatch_once_and_repeated_poll_only_renews_same_command(agent):
    _,a,_,data,calls,called,_=agent
    a.accept_poll(data);assert called.wait(1)
    a.accept_poll(copy.deepcopy(data))
    assert len(calls)==1
    lease=json.loads((a.base/'private/fixed-lease.json').read_text())
    assert lease['run']==data['run'] and lease['command']==data['command']['id']


def test_expired_lease_cannot_be_revived_by_new_poll(agent):
    _,a,now,data,_,called,_=agent
    a.armed=True;a.backend_run=data['run'];now[0]=101.01;a.frame_at=now[0]
    with pytest.raises(RuntimeError,match='expired before renewal'):a.accept_poll(data)
    assert a.last_ack==100 and not called.is_set()
    assert not (a.base/'private/fixed-lease.json').exists()


@pytest.mark.parametrize('mutation',[{'structure':'pyramid3'},{'layer':1},{'action':'release'}])
def test_mutating_received_identity_does_not_renew_or_replay(agent,mutation):
    _,a,_,data,calls,called,_=agent
    a.accept_poll(data);assert called.wait(1)
    changed=copy.deepcopy(data);changed['command'].update(mutation)
    with pytest.raises(RuntimeError,match='identity changed'):a.accept_poll(changed)
    assert len(calls)==1


@pytest.mark.parametrize('change',[{'armed':'false'},{'run':'bad'}, {'command':{'id':'bad'}}])
def test_malformed_response_cannot_dispatch(agent,change):
    _,a,_,data,_,called,_=agent
    data.update(change)
    with pytest.raises(RuntimeError):a.accept_poll(data)
    assert not called.is_set()


def test_hardware_lock_and_protocol_guard_apply_at_dispatch(agent):
    _,a,_,data,_,called,_=agent
    (a.base/'private/fixed-hardware.lock').touch()
    with pytest.raises(RuntimeError,match='Hardware lock'):a.accept_poll(data)
    (a.base/'private/fixed-hardware.lock').unlink()
    a.console_protocol=False
    with pytest.raises(RuntimeError,match='protocol'):a.accept_poll(data)
    assert not called.is_set()


def test_active_run_switch_is_rejected(agent):
    _,a,_,data,_,_,_=agent
    a.armed=True;a.backend_run='d'*32
    with pytest.raises(RuntimeError,match='session changed'):a.accept_poll(data)


def test_watchdog_stops_while_motion_worker_is_blocked(agent):
    m,a,now,data,_,_,stopped=agent
    release=threading.Event();entered=threading.Event()
    def console(*args):
        entered.set();release.wait(2);return {'details':{}}
    a.console=console;a.worker=types.MethodType(m.Agent.worker,a)
    a.accept_poll(data);assert entered.wait(1)
    watch=threading.Thread(target=a.watchdog);watch.start()
    now[0]=101.1;a.frame_at=now[0]
    try:
        assert stopped.wait(.5), 'Independent watchdog waited for the blocked worker'
        assert a.fault is not None and a.busy
    finally:
        a.done.set();release.set();watch.join(1)
    # A late successful RPC response after stopping must not become success.
    deadline=threading.Event()
    for _ in range(100):
        if a.result is not None:break
        deadline.wait(.01)
    assert a.result is not None and a.result['ok'] is False
