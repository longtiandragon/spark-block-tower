import hashlib
import json
import re
import time
import pytest
from spark_blocks_bridge.fixed_runner import FixedRunner


@pytest.fixture
def station(tmp_path, monkeypatch):
    private=tmp_path/'private';private.mkdir(exist_ok=True)
    calibration=tmp_path/'thefile.txt';calibration.write_text('reviewed calibration')
    config=dict(profile='station-20261104-three-layer', enabled=True, feed=[175.,150.,-43.04],
                tower_xy=[240.,-29.23], down_z=[-43.04,56.96,154.01], high=171.96,
                calibration_sha256=hashlib.sha256(calibration.read_bytes()).hexdigest(),
                automatic_release_references=dict(verified=True,measured_xyz=[[240.,-29.23,-43.04],[240.,-29.23,56.96],[240.,-29.23,154.01]]))
    (private/'fixed-site.json').write_text(json.dumps(config))
    pos=[180.,-60.,115.];commands=[]
    flags={'pump':False,'fail_loaded':False,'offset_carry':False,'reject':False,'attached':True}
    def query(cmd):
        commands.append(cmd)
        if cmd.startswith('M2222'):return 'V0' if flags['reject'] else 'V1'
        if cmd=='P2234':return 'V1'
        if cmd.startswith('M2203'):return 'V1' if flags['attached'] else 'V0'
        if cmd=='M2200':return 'V0'
        if cmd=='P2200':return 'B90 L60 R30'
        if cmd.startswith('M2221'):return 'X%.2f Y%.2f Z%.2f'%tuple(pos)
        if cmd=='M2231 V1':flags['pump']=True
        if cmd=='M2231 V0':flags['pump']=False
        if cmd.startswith('G0'):
            if flags['pump'] and flags['fail_loaded']:raise RuntimeError('Injected loaded failure')
            pos[:]=[float(v) for v in re.findall(r'[XYZ](-?[\d.]+)',cmd)]
            if flags['pump'] and flags['offset_carry'] and pos[:2]==[240.,-29.23] and pos[2]>60:
                pos[1]-=4.1;flags['offset_carry']=False
        return ''
    monkeypatch.setattr('spark_blocks_bridge.fixed_runner.time.sleep',lambda _:None)
    monkeypatch.setattr('spark_blocks_bridge.fixed_runner.time.monotonic',lambda:100.)
    r=FixedRunner(tmp_path,query,lambda:dict(power='V1',moving='V0',attached=['V1']*3),lambda:None,lambda:pos[:],lambda b:commands.append(b),calibration)
    sequence=[0]
    def execute(action,levels=3):
        if action=='build0' and r.phase=='new':
            execute('prepare0',levels)
        sequence[0]+=1;command='%032x'%sequence[0]
        (private/'fixed-lease.json').write_text(json.dumps(dict(run='a'*32,command=command,at=time.monotonic())))
        return r.execute(action,'a'*32,command,levels)
    return r,execute,commands,flags,private,calibration


@pytest.mark.parametrize('levels',[1,2,3])
def test_selected_structure_and_each_support_release(station,levels):
    r,execute,cmd,flags,_,_=station
    for layer in range(levels):
        execute('build%d'%layer,levels)
        assert r.phase=='awaiting_support' and flags['pump']
        with pytest.raises(RuntimeError,match='stage'):execute('build%d'%layer,levels)
        execute('release%d'%layer,levels)
        assert not flags['pump']
    assert r.phase=='complete'
    assert cmd.count('M2231 V0')==levels


def test_loaded_error_never_cleans_up_pump(station):
    r,execute,cmd,flags,_,_=station
    flags['fail_loaded']=True
    with pytest.raises(RuntimeError,match='Injected'):execute('build0')
    assert flags['pump'] and 'M2231 V0' not in cmd and r.phase=='stopped'


def test_single_clearance_horizontal_correction(station):
    r,execute,cmd,flags,_,_=station
    execute('build0');execute('release0')
    flags['offset_carry']=True
    execute('build1')
    assert r.phase=='awaiting_support' and flags['pump']
    assert (r.directory/'correction-1').exists()


def test_lock_and_changed_calibration_reject_all_motion(station):
    r,execute,cmd,_,private,calibration=station
    (private/'fixed-hardware.lock').touch()
    with pytest.raises(RuntimeError,match='locked'):execute('build0')
    assert not any(isinstance(c,str) and c.startswith('G0') for c in cmd)
    (private/'fixed-hardware.lock').unlink()
    calibration.write_text('changed')
    with pytest.raises(RuntimeError,match='calibration'):execute('build0')


def test_expired_or_other_command_lease_rejected(station):
    r,_,cmd,_,private,_=station
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='a'*32,command='b'*32,at=time.monotonic()-2)))
    with pytest.raises(RuntimeError,match='lease'):r.execute('build0','a'*32,'b'*32)
    assert not any(isinstance(c,str) and c.startswith('G0') for c in cmd)


def test_profile_change_mid_run_and_structure_change_rejected(station):
    r,execute,_,_,private,_=station
    execute('build0',2)
    with pytest.raises(RuntimeError,match='structure'):execute('release0',3)
    with (private/'fixed-site.json').open('a') as f:f.write(' ')
    with pytest.raises(RuntimeError,match='profile changed'):execute('release0',2)


def test_pyramid_cannot_execute_without_physical_verification(station):
    r,_,cmd,_,_,_=station
    with pytest.raises(RuntimeError,match='physically verified'):
        r.execute('build0','a'*32,'b'*32,3,'pyramid3')
    assert not any(isinstance(c,str) and c.startswith('G0') for c in cmd)


def test_pyramid_geometry_has_two_bases_and_top_at_seam(station):
    r,_,_,_,_,_=station
    r.structure='pyramid3'
    a,b,c=[r.target(i)[1] for i in range(3)]
    assert a[2]==b[2] and b[1]-a[1]==110
    assert c[1]==(a[1]+b[1])/2 and c[2]>a[2]
    assert 100-(b[1]-a[1])/2==45  # 45 mm nominal overlap on each support.


def test_synthetic_verified_pyramid_executes_two_bases_before_bridge(station):
    old,_,cmd,flags,private,calibration=station
    config=json.loads((private/'fixed-site.json').read_text())
    config['verified_structures']=['pyramid3']  # Test fixture, never written to robot.
    (private/'fixed-site.json').write_text(json.dumps(config))
    r=FixedRunner(private.parent,old.query,old.status,old.halt,old.xyz,old.send,calibration)
    (private/'fixed-lease.json').write_text(json.dumps(dict(run='a'*32,command='f'*32,at=100.)))
    r.execute('prepare0','a'*32,'f'*32,3,'pyramid3')
    for i in range(3):
        for action in ('build','release'):
            command=('%032x'%(i*2+(1 if action=='build' else 2)))
            (private/'fixed-lease.json').write_text(json.dumps(dict(run='a'*32,command=command,at=100.)))
            result=r.execute(action+str(i),'a'*32,command,3,'pyramid3')
            assert result['phase']==('awaiting_support' if action=='build' else 'complete' if i==2 else 'awaiting_feed')
            assert flags['pump']==(action=='build')
    assert r.phase=='complete' and cmd.count('M2231 V0')==3
