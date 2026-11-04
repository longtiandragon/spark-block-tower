import copy
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np
import pytest
from test_fixed_runner import station
from spark_blocks_bridge.fixed_layout import layout_profile, match_slots, CANDIDATE_XY
from spark_blocks_bridge.fixed_observation import verify_lift, verify_placement, FileVision


def commissioned_fixture(r, private):
    """Synthetic commissioning only. Never copy this configuration to hardware."""
    slots=[dict(command_xyz=p+[-43.04],measured_xyz=p+[-43.04],pickup_verified=True,evidence_id='fixture') for p in CANDIDATE_XY]
    profile=dict(version='fixture-v1',slots=slots,camera_verified=True,ik_verified=True,
                 collision_verified=True,vision_verified=True,placement_rois=[[100,100,180,180]]*3)
    r.config['fixed_slots']=profile
    (private/'fixed-site.json').write_text(json.dumps(r.config))
    r.profile_hash=hashlib.sha256((private/'fixed-site.json').read_bytes()).hexdigest()
    return profile


@pytest.fixture
def automatic(station):
    r,_,commands,flags,private,_=station
    profile=commissioned_fixture(r,private)
    r.capture=lambda *a,**k:dict(frame=None,at=100,sequence='fixture')
    def proof(kind,before,command):
        commands.append('verified:'+kind)
        return dict(id=('%032x'%(r.layer*2+(1 if kind=='grasp' else 2))),kind=kind,verified=True,
                    run=r.run_id,command=command,layer=r.layer,profile_sha256=r.profile_hash)
    r.verify_images=proof
    index=[0]
    def go(action,**extra):
        index[0]+=1;command='%032x'%index[0]
        (private/'fixed-lease.json').write_text(json.dumps(dict(run='e'*32,command=command,at=100.)))
        return r.execute(action,'e'*32,command,3,'tower3',[s['command_xyz'] for s in profile['slots']],
                         profile_sha256=r.profile_hash,slot_version=profile['version'],**extra)
    return r,go,commands,flags,private,profile


def test_fixed_layout_requires_independent_physical_evidence(station):
    r,_,_,_,private,_=station
    with pytest.raises(ValueError):layout_profile(r.config)
    p=commissioned_fixture(r,private)
    assert match_slots([s['command_xyz'] for s in p['slots']][::-1],r.config)[1] is None
    moved=[s['command_xyz'][:] for s in p['slots']];moved[0][0]+=4
    with pytest.raises(ValueError):match_slots(moved,r.config)
    for key in ('camera_verified','ik_verified','collision_verified','vision_verified'):
        original=p[key];p[key]=False
        with pytest.raises(ValueError):layout_profile(r.config)
        p[key]=original
    p['slots'][0]['measured_xyz']=None
    with pytest.raises(ValueError):layout_profile(r.config)


def test_candidate_file_cannot_enable_motion():
    path=Path(__file__).parents[3]/'config/fixed-slots.candidate.json'
    with pytest.raises(ValueError):layout_profile(json.loads(path.read_text()))


def test_three_blocks_require_grasp_then_placement_for_every_count(automatic):
    r,go,commands,flags,_,_=automatic
    go('prepare0')
    for i in range(3):
        result=go('build%d'%i)
        assert result['grasp']['layer']==result['placement']['layer']==i
        assert result['grasp']['command']==result['placement']['command']
        assert result['grasp']['verified'] and result['placement']['verified']
        assert r.layer==i+1
    assert commands.count('M2231 V1')==3 and commands.count('M2231 V0')==3
    assert r.phase=='complete' and not flags['pump']


def test_failed_grasp_never_translates_or_releases(automatic):
    r,go,commands,flags,_,_=automatic
    go('prepare0');commands.clear()
    def fail(*args):raise ValueError('empty pickup')
    r.verify_images=fail
    with pytest.raises(ValueError,match='empty pickup'):go('build0')
    events=[json.loads(line)['phase'] for line in (r.directory/'events.jsonl').read_text().splitlines()]
    assert 'probe_lift_complete' in events and 'carry' not in events
    assert flags['pump'] and 'M2231 V0' not in commands and r.layer==0


def test_failed_placement_does_not_advance_layer(automatic):
    r,go,commands,flags,_,_=automatic
    proof=r.verify_images
    def verify(kind,*args):
        if kind=='placement':raise ValueError('placement ambiguous')
        return proof(kind,*args)
    r.verify_images=verify
    go('prepare0')
    with pytest.raises(ValueError,match='placement ambiguous'):go('build0')
    assert r.layer==0 and r.phase=='stopped' and not flags['pump']


def test_staging_and_changed_profile_reject_before_moving(automatic):
    r,go,commands,_,_,_=automatic
    with pytest.raises(RuntimeError,match='staging'):go('prepare0',staging={'origin':[1,2,3]})
    assert not any(isinstance(c,str) and c.startswith('G0') for c in commands)
    with pytest.raises(RuntimeError,match='profile identity'):
        r.execute('prepare0','e'*32,'a'*32,3,'tower3',[s['command_xyz'] for s in r.config['fixed_slots']['slots']],profile_sha256='0'*64,slot_version='fixture-v1')


def test_pickup_does_not_borrow_tower_release_height(automatic):
    r,go,_,_,_,_=automatic
    go('prepare0')
    original=r.measured
    r.measured=lambda:[*original()[:2],original()[2]+2.1]
    r.config['automatic_release_references']['measured_xyz'][0][2]=-41.26
    with pytest.raises(RuntimeError,match='independently verified slot'):
        r.move('pickup',r.sources[0],120,'%032x'%1)


def test_historical_positive_and_unchanged_negative_are_separate_evidence():
    folder=Path(__file__).parents[3]/'data/deployments/20261104-executor'
    a=cv2.imread(str(folder/'trial-before.jpg'));b=cv2.imread(str(folder/'trial-raised.jpg'))
    if a is None or b is None:pytest.skip('Historical photos not distributed with minimal package')
    assert verify_lift(a,b,[389.5,218])['verified']
    # This is a constructed static negative, not a recorded failed physical run.
    with pytest.raises(ValueError):verify_lift(a,a,[389.5,218])
    with pytest.raises(ValueError):verify_lift(a,np.zeros_like(a),[389.5,218])
    shifted=cv2.warpAffine(a,np.float32([[1,0,10],[0,1,0]]),(640,480))
    with pytest.raises(ValueError):verify_lift(a,shifted,[389.5,218])


def test_placement_needs_changed_target_empty_source_and_stability():
    before=np.zeros((480,640,3),np.uint8)
    hsv=np.zeros_like(before);hsv[100:180,100:180]=[104,160,170]
    after=cv2.cvtColor(hsv,cv2.COLOR_HSV2BGR)
    assert verify_placement(before,[after]*3,[100,100,180,180],[400,300])['verified']
    with pytest.raises(ValueError):verify_placement(after,[after]*3,[100,100,180,180],[400,300])
    occupied=after.copy();occupied[275:325,375:425]=after[110,110]
    with pytest.raises(ValueError):verify_placement(before,[occupied]*3,[100,100,180,180],[400,300])
    with pytest.raises(ValueError):verify_placement(before,[after,before,after],[100,100,180,180],[400,300])


def test_frame_reader_rejects_stale_and_pre_motion_frames(tmp_path,monkeypatch):
    import spark_blocks_bridge.fixed_observation as observation
    private=tmp_path/'private';private.mkdir()
    frame=np.zeros((480,640,3),np.uint8)
    np.savez(private/'fixed-frame.npz',frame=frame,at=99.,sequence='old')
    clock=[100.]
    monkeypatch.setattr(observation.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(observation.time,'sleep',lambda delay:clock.__setitem__(0,clock[0]+.1))
    vision=FileVision(tmp_path)
    with pytest.raises(RuntimeError):vision.capture(lambda:None,100.)
    clock[0]=100.
    np.savez(private/'fixed-frame.npz',frame=frame,at=99.9,sequence='new')
    assert vision.capture(lambda:None,99.8)['sequence']=='new'
    with pytest.raises(RuntimeError):vision.capture(lambda:None,100.)


def test_release_reference_error_keeps_suction(automatic):
    r,go,commands,flags,_,_=automatic
    go('prepare0')
    original=r.measured
    def feedback():
        p=original()
        if r.pumping and abs(p[2]+43.04)<.1 and abs(p[0]-240)<1:p[2]+=1.
        return p
    r.measured=feedback
    with pytest.raises(RuntimeError,match='reference not reached'):go('build0')
    assert flags['pump'] and 'M2231 V0' not in commands and r.layer==0


def test_candidate_preview_queries_ik_only_even_while_locked(station):
    r,_,commands,_,private,_=station
    commissioned_fixture(r,private)
    r.config['fixed_slots']['ik_verified']=False
    (private/'fixed-hardware.lock').touch()
    result=r.preview_slots()
    assert result['passed'] and result['motion_sent'] is False and result['checked_points']>500
    assert all(isinstance(c,str) and (c.startswith('M2222 ') or c.startswith('M2221 ') or c in ('M2200','P2200')) for c in commands)
    assert r.sources==[]


def test_observation_retreat_is_bounded_and_never_commands_pump(station):
    r,_,commands,_,private,_=station
    preview=r.observe_scene('e'*32,'b'*32,True)
    assert preview['passed'] and not any(c.startswith('G0') for c in commands)
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='e'*32,command='b'*32,at=100.)))
    result=r.observe_scene('e'*32,'b'*32)
    assert result['phase']=='observation_complete'
    assert not any(isinstance(c,str) and c.startswith('M2231') for c in commands)
    with pytest.raises(RuntimeError):r.observe_scene('e'*32,'b'*32)


@pytest.mark.parametrize('grasp_ok',[True,False])
def test_single_source_trial_has_no_horizontal_carry_and_fails_closed(station,monkeypatch,grasp_ok):
    r,_,commands,flags,private,calibration=station
    calibration.write_text('-1 480 -1 300')
    r.config['calibration_sha256']=hashlib.sha256(calibration.read_bytes()).hexdigest()
    (private/'fixed-site.json').write_text(json.dumps(r.config))
    r.profile_hash=hashlib.sha256((private/'fixed-site.json').read_bytes()).hexdigest()
    source=[226.,80.,-43.04]
    monkeypatch.setattr('spark_blocks_bridge.auto_sources.detect_sources',lambda *a:[dict(xyz=source)])
    r.vision.capture=lambda *a:dict(frame=None,at=100,sequence='fixture')
    def verify(*args):
        if not grasp_ok:raise ValueError('empty trial')
        return dict(verified=True)
    r.verify_images=verify
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='e'*32,command='b'*32,at=100.)))
    if grasp_ok:
        result=r.trial_source('e'*32,'b'*32,source)
        assert result['phase']=='trial_complete' and not flags['pump']
    else:
        with pytest.raises(ValueError,match='empty trial'):r.trial_source('e'*32,'b'*32,source)
        assert flags['pump'] and 'M2231 V0' not in commands
    events=[json.loads(v) for v in (r.directory/'events.jsonl').read_text().splitlines()]
    for event in events:
        if event['pumping'] and isinstance(event.get('detail'),dict) and 'target' in event['detail']:
            assert event['detail']['target'][:2]==source[:2]


@pytest.mark.parametrize('stalled',[False,True])
def test_contact_correction_is_one_shot_bounded_and_stops_on_no_feedback_motion(station,stalled):
    import time
    r,_,commands,flags,private,_=station
    source=[226.,80.,-43.04];actual=[226.,80.,-40.87]
    r.query('G0 X226 Y80 Z-43.04 F60')
    measured=r.measured
    def feedback():
        p=measured()
        if p[2]<0:p[2]+=2.17
        return actual[:] if stalled else p
    r.measured=feedback;r.xyz=feedback
    prior=private/'fixed-runs'/('d'*32);prior.mkdir(parents=True)
    (prior/'events.jsonl').write_text('\n'.join(map(json.dumps,[dict(phase='trial_pickup'),dict(phase='empty_arrival_mismatch',detail=dict(target=source,measured=actual))])))
    (private/'fixed-progress.json').write_text(json.dumps(dict(run='d'*32,phase='stopped',pumping=False,fault='Empty arrival outside axis tolerances',time=time.time())))
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='e'*32,command='b'*32,at=100.)))
    r.capture=lambda *a:dict(frame=None)
    r.verify_images=lambda *a:dict(verified=True)
    if stalled:
        with pytest.raises(RuntimeError,match='stalled'):r.contact_trial('e'*32,'b'*32)
        assert not flags['pump'] and 'M2231 V1' not in commands
    else:
        result=r.contact_trial('e'*32,'b'*32)
        assert result['phase']=='trial_complete' and not flags['pump']
    assert (prior/'contact-correction.started').exists()
    for c in commands:
        if isinstance(c,str) and c.startswith('G0'):
            import re
            assert float(re.search(r'Z(-?[\d.]+)',c)[1])>=-45.54


def test_confirmed_contact_lift_moves_only_up_and_retains_suction(station):
    r,_,commands,flags,private,_=station
    r.query('G0 X223 Y82 Z-41.74 F60');commands.clear()
    r.capture=lambda *args:dict(frame=None)
    r.vision.save=lambda *args:dict(verified=False,grasp_observation_pending=True)
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='e'*32,command='b'*32,at=100.)))
    result=r.confirmed_contact_lift('e'*32,'b'*32,[223.,82.,-41.74])
    assert result['phase']=='awaiting_grasp_observation' and flags['pump']
    assert 'M2231 V0' not in commands
    moves=[c for c in commands if isinstance(c,str) and c.startswith('G0')]
    import re
    positions=[[float(v) for v in re.findall(r'[XYZ](-?[\d.]+)',c)] for c in moves]
    assert all(p[:2]==[223.,82.] and -41.74<=p[2]<=-21.74 for p in positions)
    assert positions[-1][2]==-21.74
