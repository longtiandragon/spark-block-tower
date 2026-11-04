import pytest
import json
from fastapi.testclient import TestClient
from spark_freeze.block_app import create_block_app


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('SPARK_FIXED_TOWER','1')
    monkeypatch.setenv('SPARK_CONTROL_TOKEN','control-token-for-fixed-api')
    monkeypatch.setenv('SPARK_BRIDGE_TOKEN','bridge-token-for-fixed-api')
    app=create_block_app(tmp_path)
    app.state.fixed_tower.clock=lambda:100.
    with TestClient(app) as client:
        yield client


def test_authenticated_http_structure_to_three_layer_command_chain(api):
    control={'X-Control-Token':'control-token-for-fixed-api'}
    bridge={'X-Bridge-Token':'bridge-token-for-fixed-api'}
    agent=dict(id='a'*32,ready=True,stopped=True,fault=None,structures=['single','tower2','tower3'])
    assert api.post('/api/fixed-tower/robot/tick',json=agent).status_code==401
    assert api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge).status_code==200
    def act(action,checked=True):
        state=api.get('/api/fixed-tower/state').json()
        payload=dict(action=action,session=state['session'],revision=state['revision'],operator='o'*32,checked=checked,structure='tower3')
        assert api.post('/api/fixed-tower/heartbeat',json=payload,headers=control).status_code==200
        return api.post('/api/fixed-tower/control',json=payload,headers=control)
    assert act('start',False).status_code==409
    assert act('start').json()['phase']=='preparing'
    cmd=api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge).json()['command']
    assert cmd['action']=='prepare'
    assert act('feed').status_code==409
    assert api.post('/api/fixed-tower/robot/result',json=dict(id=agent['id'],command_id=cmd['id'],ok=True,details={'prepared':True}),headers=bridge).status_code==200
    assert act('support').status_code==409
    for i in range(3):
        assert act('feed').status_code==200
        cmd=api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge).json()['command']
        assert cmd['layer']==i and cmd['structure']=='tower3'
        result=dict(id=agent['id'],command_id=cmd['id'],ok=True,details={'fixture':'synthetic'})
        assert api.post('/api/fixed-tower/robot/result',json=result,headers=bridge).status_code==200
        assert api.post('/api/fixed-tower/robot/result',json=result,headers=bridge).status_code==409
        assert act('support').status_code==200
        cmd=api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge).json()['command']
        assert cmd['action']=='release'
        result['command_id']=cmd['id']
        assert api.post('/api/fixed-tower/robot/result',json=result,headers=bridge).status_code==200
    state=api.get('/api/fixed-tower/state').json()
    assert state['phase']=='completed' and state['placed']==3 and len(state['receipts'])==7


def test_locked_real_agent_and_unverified_shape_never_issue_command(api):
    bridge={'X-Bridge-Token':'bridge-token-for-fixed-api'}
    control={'X-Control-Token':'control-token-for-fixed-api'}
    agent=dict(id='a'*32,ready=False,stopped=True,fault='hardware locked')
    api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge)
    state=api.get('/api/fixed-tower/state').json()
    payload=dict(action='start',session=state['session'],revision=state['revision'],operator='o'*32,checked=True,structure='pyramid3')
    api.post('/api/fixed-tower/heartbeat',json=payload,headers=control)
    assert api.post('/api/fixed-tower/control',json=payload,headers=control).status_code==409
    assert api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge).json()['command'] is None
    agent.update(ready=True,fault=None)
    api.post('/api/fixed-tower/robot/tick',json=agent,headers=bridge)
    assert api.post('/api/fixed-tower/control',json=payload,headers=control).status_code==409


def test_readonly_plan_exposes_two_supports_without_enabling_motion(api):
    r=api.get('/api/fixed-tower/plan/pyramid3')
    assert r.status_code==200
    plan=r.json()
    assert [s['layer'] for s in plan['steps']]==[0,0,1]
    assert plan['steps'][2]['supports']==[0,1]
    assert not plan['physical_verified'] and not plan['motion_enabled']
    assert plan['ik_audit'] is None
    assert api.get('/api/fixed-tower/state').json()['phase']=='idle'


def test_nonfinite_source_age_cannot_poison_state(api):
    from spark_freeze.block_app import FixedAgent
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FixedAgent(id='a'*32,ready=True,stopped=True,source_age=float('inf'))
    assert api.get('/api/fixed-tower/state').status_code==200


def test_audit_is_historical_until_current_fingerprints_match(api):
    t=api.app.state.fixed_tower
    folder=t.path/'ik';folder.mkdir()
    report=dict(structure='pyramid3',motion_sent=False,source='robot_firmware_readonly_ik',passed=True,
                checked_points=1000,failure_count=0,calibration_sha256='a'*64,profile_sha256='b'*64)
    (folder/'pyramid3.json').write_text(json.dumps(report))
    assert api.get('/api/fixed-tower/plan/pyramid3').json()['ik_audit']['matches_current'] is False
    agent=dict(id='a'*32,ready=False,stopped=True,calibration_sha256='a'*64,profile_sha256='b'*64)
    api.post('/api/fixed-tower/robot/tick',json=agent,headers={'X-Bridge-Token':'bridge-token-for-fixed-api'})
    plan=api.get('/api/fixed-tower/plan/pyramid3').json()
    assert plan['ik_audit']['matches_current'] and not plan['motion_enabled']
    assert not plan['ik_audit']['physical_verified'] and not plan['ik_audit']['collision_verified']
    report['motion_sent']=True
    (folder/'pyramid3.json').write_text(json.dumps(report))
    assert api.get('/api/fixed-tower/plan/pyramid3').json()['ik_audit'] is None
