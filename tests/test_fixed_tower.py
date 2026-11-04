import pytest
from spark_freeze.fixed_tower import FixedTower
from spark_freeze.blocks import BlockError


@pytest.fixture
def task(tmp_path):
    now=[100.]
    t=FixedTower(tmp_path,True,lambda:now[0])
    def ping():
        t.heartbeat('o'*32)
        return t.poll(dict(id='a'*32,ready=True,stopped=True,fault=None))
    def act(action,**kwargs):
        result=t.control(action,t.session,t.revision,'o'*32,checked=True,**kwargs)
        if action=='start':
            assert t.phase=='preparing' and t.command['action']=='prepare'
            t.result('a'*32,t.command['id'],True,{'prepared':True})
        return result
    ping()
    return t,now,ping,act


def test_three_layer_web_workflow_and_duplicate_results(task):
    t,_,ping,act=task
    act('start')
    for layer in range(3):
        assert t.phase=='awaiting_feed'
        act('feed');cmd=ping()['command'];assert cmd['layer']==layer
        t.result('a'*32,cmd['id'],True,{'physical_observation_required':True})
        assert t.phase=='awaiting_support' and t.placed==layer
        with pytest.raises(BlockError):t.result('a'*32,cmd['id'],True,{})
        act('support');cmd=ping()['command'];assert cmd['action']=='release'
        t.result('a'*32,cmd['id'],True,{})
    assert t.phase=='completed' and t.placed==3


def test_expired_operator_cannot_revive_motion(task):
    t,now,ping,act=task
    act('start');act('feed');now[0]+=2.1
    ping()
    assert t.phase=='stopped' and t.command is None
    with pytest.raises(BlockError):act('feed')


def test_wrong_owner_and_stale_revision_block_start(task):
    t,_,_,act=task
    with pytest.raises(BlockError):t.control('start',t.session,t.revision-1,'o'*32,True)
    act('start')
    with pytest.raises(BlockError):t.heartbeat('x'*32)
    with pytest.raises(BlockError):t.poll(dict(id='b'*32,ready=True,stopped=True))


def test_failure_and_stop_cannot_reset_or_release(task):
    t,_,ping,act=task
    act('start');act('feed');cmd=ping()['command']
    t.result('a'*32,cmd['id'],False,{'vacuum_retained':True})
    for a in ('reset','support','feed','start'):
        with pytest.raises(BlockError):act(a)


def test_disabled_entry_cannot_start(tmp_path):
    t=FixedTower(tmp_path)
    with pytest.raises(BlockError):t.control('start',t.session,t.revision,'o'*32,True)


def test_automatic_run_freezes_all_sources_and_skips_manual_feeding(task):
    t,_,ping,_=task
    sources=[[302.,181.,-43.04],[155.,235.,-43.04],[213.,136.,-43.04]]
    t.agent.update(sources=sources,source_age=.1,slots_ready=True,slot_version='fixture-v1',profile_sha256='f'*64)
    t.control('start',t.session,t.revision,'o'*32,True,3,'tower3',True)
    assert t.phase=='preparing' and t.command['sources']==sources
    t.result('a'*32,t.command['id'],True,{})
    for i in range(3):
        assert t.phase=='building' and t.command['sources']==sources
        command=t.command['id']
        proofs={kind:dict(id=('%032x'%(i*2+j+1)),kind=kind,verified=True,run=t.session,
                         command=command,layer=i,profile_sha256='f'*64) for j,kind in enumerate(('grasp','placement'))}
        t.result('a'*32,command,True,dict(automatic_released=True,profile_sha256='f'*64,slot_version='fixture-v1',**proofs))
    assert t.phase=='completed' and t.placed==3


def test_unverified_pyramid_is_preview_only(task):
    t,_,_,_=task
    with pytest.raises(BlockError,match='尚未完成现场'):
        t.control('start',t.session,t.revision,'o'*32,True,3,'pyramid3')
    assert t.phase=='idle'


def test_verified_fixture_pyramid_requires_two_bases_before_top(task):
    t,_,ping,_=task
    t.agent['structures']=['pyramid3']
    t.control('start',t.session,t.revision,'o'*32,True,3,'pyramid3')
    assert t.phase=='preparing'
    t.result('a'*32,t.command['id'],True,{})
    assert [v['layer'] for v in t.snapshot()['structures']['pyramid3']['cells']]==[0,0,1]
    for i in range(3):
        t.control('feed',t.session,t.revision,'o'*32,True)
        cmd=ping()['command'];assert cmd['structure']=='pyramid3' and cmd['layer']==i
        t.result('a'*32,cmd['id'],True,{})
        assert t.placed==i
        t.control('support',t.session,t.revision,'o'*32,True)
        cmd=ping()['command'];t.result('a'*32,cmd['id'],True,{})
    assert t.phase=='completed'


@pytest.mark.parametrize('damage',['missing','wrong_run','wrong_layer','wrong_profile','same_evidence','unverified','malformed'])
def test_automatic_receipts_cannot_count_without_bound_evidence(task,damage):
    t,_,_,_=task
    t.agent.update(sources=[[310,100,-43.04],[190,220,-43.04],[175,100,-43.04]],
                   source_age=.1,slots_ready=True,slot_version='fixture-v1',profile_sha256='f'*64)
    t.control('start',t.session,t.revision,'o'*32,True,3,'tower3',True)
    t.result('a'*32,t.command['id'],True,{})
    command=t.command['id']
    proof=lambda kind,identity:dict(kind=kind,id=identity,verified=True,run=t.session,command=command,layer=0,profile_sha256='f'*64)
    details=dict(automatic_released=True,profile_sha256='f'*64,slot_version='fixture-v1',
                 grasp=proof('grasp','1'*32),placement=proof('placement','2'*32))
    if damage=='missing':del details['grasp']
    elif damage=='wrong_run':details['grasp']['run']='0'*32
    elif damage=='wrong_layer':details['placement']['layer']=1
    elif damage=='wrong_profile':details['profile_sha256']='0'*64
    elif damage=='same_evidence':details['placement']['id']='1'*32
    elif damage=='unverified':details['grasp']['verified']=False
    else:details['grasp']=None
    t.result('a'*32,command,True,details)
    assert t.phase=='stopped' and t.placed==0 and t.command is None
    with pytest.raises(BlockError):t.result('a'*32,command,True,details)


def test_slot_profile_change_during_task_stops(task):
    t,_,_,_=task
    t.agent.update(sources=[[310,100,-43.04],[190,220,-43.04],[175,100,-43.04]],
                   source_age=.1,slots_ready=True,slot_version='fixture-v1',profile_sha256='f'*64)
    t.control('start',t.session,t.revision,'o'*32,True,3,'tower3',True)
    telemetry=dict(t.agent,profile_sha256='0'*64)
    t.poll(telemetry)
    assert t.phase=='stopped'
