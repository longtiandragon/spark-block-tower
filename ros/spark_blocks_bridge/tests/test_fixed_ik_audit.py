import pytest
from spark_blocks_bridge.fixed_runner import inspect_ik, inspect_payload_geometry

CONFIG=dict(feed=[175.,150.,-43.04],tower_xy=[240.,-29.23],down_z=[-43.04,56.96,154.01])


@pytest.mark.parametrize('shape',['single','tower2','tower3','pyramid3'])
def test_audit_issues_only_ik_queries_and_never_claims_physical_acceptance(shape):
    calls=[]
    def query(command):
        calls.append(command)
        assert command.startswith('M2222 ') and command.endswith(' S0')
        return 'V1'
    result=inspect_ik(shape,[178.,-62.,115.],CONFIG,query)
    assert result['passed'] and result['checked_points']==len(calls)
    assert not result['motion_sent'] and not result['physical_verified'] and not result['collision_verified']
    if shape=='pyramid3':
        placements=[v for v in result['stages'] if v['phase']=='place']
        assert [v['target'][1] for v in placements]==[-55.,55.,0.]


def test_unreachable_sample_fails_audit_without_motion_or_retries():
    calls=[]
    def query(cmd):
        calls.append(cmd)
        return 'V0' if len(calls)==5 else 'V1'
    r=inspect_ik('pyramid3',[178.,-62.,115.],CONFIG,query)
    assert not r['passed'] and r['failure_count']==1 and r['motion_sent'] is False


def test_malformed_reply_aborts_without_retry():
    calls=[]
    def query(cmd):
        calls.append(cmd)
        return 'ok'
    with pytest.raises(RuntimeError,match='Unrecognized'):inspect_ik('pyramid3',[178.,-62.,115.],CONFIG,query)
    assert len(calls)==1


@pytest.mark.parametrize('shape',['single','tower2','tower3','pyramid3'])
def test_nominal_payload_model_requires_support_and_clearance(shape):
    r=inspect_payload_geometry(shape,CONFIG)
    assert r['passed'],r['failures']
    assert not r['arm_links_checked'] and not r['real_scene_checked']
    if shape=='pyramid3':assert r['steps'][2]['supports']==[0,1]


def test_bad_tower_height_fails_support_and_payload_clearance():
    bad={**CONFIG,'down_z':[-43.04,30.,154.01]}
    r=inspect_payload_geometry('tower2',bad)
    assert not r['passed'] and r['failure_count']>0
