"""Single reviewed 100 mm tower profile, driven by leased console actions.

Operator feeding and support confirmation remain explicit. No arbitrary XYZ API.
"""
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
try:
    from .fixed_layout import layout_profile, match_slots
    from .fixed_observation import FileVision, verify_lift, verify_placement
except ImportError:  # Standalone deployment beside the exclusive serial console.
    from fixed_layout import layout_profile, match_slots
    from fixed_observation import FileVision, verify_lift, verify_placement


def points(a, b):
    n = max(1, math.ceil(math.dist(a,b)/3))
    return [[x+(y-x)*i/n for x,y in zip(a,b)] for i in range(1,n+1)]


def station_target(structure,index,config):
    counts={'single':1,'tower2':2,'tower3':3,'pyramid3':3}
    if structure not in counts or type(index) is not int or not 0<=index<counts[structure]:
        raise ValueError('Invalid structure target')
    if structure=='pyramid3':
        down=[[240.,-55.,-43.04],[240.,55.,-43.04],[240.,0.,56.96]][index]
        return [175.,180.,-43.04],down,[20.,20.,71.96][index]
    return config['feed'],config['tower_xy']+[config['down_z'][index]],[20.,71.96,171.96][index]


def station_build_route(structure,index,config):
    source,down,high=station_target(structure,index,config)
    return [('feed_safe',source[:2]+[115.],300),('feed_above',source[:2]+[20.],300),
            ('pickup',source,120),('lift',source[:2]+[high],300),
            ('carry',down[:2]+[high],300),('place',down,120)]


def station_exit_route(structure,index,config):
    feed,down,high=station_target(structure,index,config)
    count={'single':1,'tower2':2,'tower3':3,'pyramid3':3}[structure]
    route=[('exit_up',down[:2]+[high],300),('exit_side',feed[:2]+[high],300),('feed_safe',feed[:2]+[115.],300)]
    if index==count-1:
        route.append(('observer',[180.,-60.,115.],300))
    return route


def inspect_payload_geometry(structure,config):
    """Nominal axis-aligned 100 mm cubes; excludes arm links and real scene."""
    count={'single':1,'tower2':2,'tower3':3,'pyramid3':3}[structure]
    placed,failures,steps=[],[],[]
    ground=-43.04
    for i in range(count):
        source,down,_=station_target(structure,i,config)
        bottom=down[2]-ground
        supports=[j for j,b in enumerate(placed) if abs(bottom-(b[2]-ground+100))<=3
                  and abs(down[0]-b[0])<100 and abs(down[1]-b[1])<100]
        if bottom>3 and not supports:
            failures.append(dict(index=i,reason='upper block lacks support'))
        if structure=='pyramid3' and i==2:
            if len(supports)!=2 or any((100-abs(down[0]-placed[j][0]))*(100-abs(down[1]-placed[j][1]))<4000 for j in supports):
                failures.append(dict(index=i,reason='bridge overlap below 40 percent per support'))
        route=station_build_route(structure,i,config)
        # Only loaded lift, carry and descent. Empty arm needs separate link model.
        previous=source
        for phase,target,_ in route[3:]:
            for p in [previous]+points(previous,target):
                payload_bottom=p[2]-ground
                for j,b in enumerate(placed):
                    obstacle_bottom=b[2]-ground
                    horizontal=abs(p[0]-b[0])<103 and abs(p[1]-b[1])<103
                    vertical=payload_bottom<obstacle_bottom+100+3 and payload_bottom+100>obstacle_bottom-3
                    intended_contact=phase=='place' and j in supports and payload_bottom>=obstacle_bottom+100-3
                    if horizontal and vertical and not intended_contact:
                        failures.append(dict(index=i,phase=phase,obstacle=j,xyz=p,reason='payload clearance below 3 mm'))
                        break
            previous=target
        steps.append(dict(index=i,target=down,supports=supports))
        placed.append(down)
    return dict(passed=not failures,failures=failures[:20],failure_count=len(failures),steps=steps,
                model='axis_aligned_100mm_cubes_3mm_clearance',arm_links_checked=False,
                real_scene_checked=False,physical_verified=False)


def inspect_ik(structure,start,config,query):
    """Read-only sampled IK audit; no stop, enable, motion or pump commands."""
    count={'single':1,'tower2':2,'tower3':3,'pyramid3':3}[structure]
    previous=list(start)
    checked,failures,stages=0,[],[]
    for index in range(count):
        for phase,target,_ in station_build_route(structure,index,config)+station_exit_route(structure,index,config):
            before=checked
            for p in [previous]+points(previous,target):
                if not all(type(v) in (float,int) and math.isfinite(v) for v in p):
                    raise ValueError('Invalid audit coordinate')
                if not (90<=p[0]<=290 and -100<=p[1]<=210 and -50<=p[2]<=175):
                    raise ValueError('Audit point outside configured envelope')
                reply=query('M2222 X%.2f Y%.2f Z%.2f S0'%tuple(p))
                if reply not in ('V0','V1'):
                    raise RuntimeError('Unrecognized IK response; do not retry')
                checked+=1
                if reply=='V0':failures.append(dict(index=index,phase=phase,xyz=p))
            stages.append(dict(index=index,phase=phase,target=target,checked=checked-before))
            previous=target
    geometry=inspect_payload_geometry(structure,config)
    return dict(structure=structure,passed=not failures,checked_points=checked,
                failure_count=len(failures),failures=failures[:20],stages=stages,
                payload_geometry=geometry,
                motion_sent=False,physical_verified=False,collision_verified=False)


class FixedRunner:
    def __init__(self, base, query, status, halt, xyz, send, calibration_path='/home/spark/thefile.txt'):
        self.base, self.query, self.status, self.halt, self.xyz, self.send = Path(base), query, status, halt, xyz, send
        self.run_id, self.phase, self.layer = None, 'new', 0
        self.calibration_path = Path(calibration_path)
        self.levels = 3
        self.structure = 'tower3'
        self.sources=[]
        self.staging=None
        self.pumping, self.fault = False, None
        self.config = json.loads((self.base/'private/fixed-site.json').read_text())
        if self.config.get('profile') != 'station-20261104-three-layer' or self.config.get('enabled') is not True:
            raise RuntimeError('Fixed profile is not enabled')
        # This runner intentionally accepts only the physically reviewed station.
        if (self.config.get('feed') != [175.,150.,-43.04]
                or self.config.get('tower_xy') != [240.,-29.23]
                or self.config.get('down_z') != [-43.04,56.96,154.01]
                or self.config.get('high') != 171.96):
            raise RuntimeError('Fixed station geometry differs from reviewed profile')
        self.profile_hash = hashlib.sha256((self.base/'private/fixed-site.json').read_bytes()).hexdigest()
        self.vision = FileVision(self.base)

    def preview_slots(self):
        """Query-only candidate IK audit, deliberately independent of enable flags."""
        sources=[s['command_xyz'] for s in self.config.get('fixed_slots',{}).get('slots',[])]
        if len(sources)!=3:
            raise ValueError('Three candidate slots required')
        original=self.sources
        self.sources=sources
        try:
            start=self.measured()
            failures=[];count=0
            route=[('source_prepare',sources[0][:2]+[80.],300)]
            for layer in range(3):route+=self.build_route(layer)+self.exit_route(layer)
            for phase,target,_ in route:
                for p in [start]+points(start,target):
                    if not (90<=p[0]<=330 and -100<=p[1]<=260 and -50<=p[2]<=175):
                        raise ValueError('Candidate path outside envelope')
                    reply=self.query('M2222 X%.2f Y%.2f Z%.2f S0'%tuple(p))
                    if reply not in ('V0','V1'):raise RuntimeError('Unexpected IK reply')
                    count+=1
                    if reply=='V0':failures.append(dict(phase=phase,xyz=p))
                start=target
            return dict(passed=not failures,checked_points=count,failures=failures[:30],
                        failure_count=len(failures),motion_sent=False,collision_verified=False,
                        physical_verified=False,profile_sha256=self.profile_hash)
        finally:self.sources=original

    def observe_scene(self, run, command, preview=False):
        """Bounded empty-arm retreat, also callable through the leased adapter.

        This is not a grasp or commissioning success. No pump command is sent.
        """
        if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (run,command)):
            raise ValueError('Invalid observation identity')
        st=self.status()
        if st['moving']!='V0' or st['attached']!=['V1']*3 or st['power']!='V1':
            raise RuntimeError('Stationary powered arm required')
        start=self.measured()
        if not (75<=start[2]<=120 and 90<=start[0]<=330 and -100<=start[1]<=260):
            raise RuntimeError('Observation retreat requires the current empty clearance pose')
        route=[('observe_clear',[240.,-60.,80.],120),('observe_park',[180.,-60.,115.],120)]
        if preview:
            failures=[];count=0
            for phase,target,_ in route:
                for p in [start]+points(start,target):
                    reply=self.query('M2222 X%.2f Y%.2f Z%.2f S0'%tuple(p));count+=1
                    if reply!='V1':failures.append(dict(phase=phase,xyz=p,reply=reply))
                start=target
            return dict(passed=not failures,failures=failures,checked_points=count,motion_sent=False)
        if self.run_id is not None:
            raise RuntimeError('Observation requires a new executor; never interrupt a load')
        self.run_id=run;self.directory=self.base/'private/fixed-runs'/run
        self.directory.mkdir(parents=True,exist_ok=False)
        self.sources=[start]  # Use the reviewed outer empty-arm envelope.
        try:
            self.check(command)
            self.preflight(start,route,command)
            for phase,target,feed in route:self.move(phase,target,feed,command)
            self.phase='complete';self.snapshot('observation_complete')
            return self.latest
        except Exception as exc:
            self.phase='stopped';self.fault=str(exc);self.snapshot('stopped');raise
        finally:
            self.send(b'M410\nM2000\n')
            self.sources=[]

    def check(self, command):
        if (self.base/'private/fixed-hardware.lock').exists():
            raise RuntimeError('Hardware locked; onsite inspection required')
        if self.fault:
            raise RuntimeError('Fixed runner fault latched: '+self.fault)
        lease = json.loads((self.base/'private/fixed-lease.json').read_text())
        if (lease.get('run') != self.run_id or lease.get('command') != command
                or type(lease.get('at')) not in (int,float)
                or not 0 <= time.monotonic()-lease['at'] <= 1):
            raise RuntimeError('Fixed motion lease expired')
        if hashlib.sha256((self.base/'private/fixed-site.json').read_bytes()).hexdigest() != self.profile_hash:
            raise RuntimeError('Fixed profile changed during task')
        camera = self.calibration_path
        if hashlib.sha256(camera.read_bytes()).hexdigest() != self.config['calibration_sha256']:
            raise RuntimeError('Camera calibration changed')

    def trial_source(self, run, command, source, preview=False):
        """Single observed-source 20 mm trial. No carry or tower completion claim."""
        if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (run,command)):
            raise ValueError('Invalid trial identity')
        if (not isinstance(source,list) or len(source)!=3 or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in source) or
                not (110<=source[0]<=330 and -100<=source[1]<=260 and source[2]==-43.04)):
            raise ValueError('Trial source outside reviewed workspace')
        if self.run_id is not None:raise RuntimeError('Trial requires a fresh executor')
        try:
            from .auto_sources import detect_sources
        except ImportError:
            from auto_sources import detect_sources
        frame=self.vision.capture(lambda:None,time.monotonic())
        found=detect_sources(frame['frame'],list(map(float,self.calibration_path.read_text().split())))
        if sum(math.dist(p['xyz'][:2],source[:2])<=3 for p in found)!=1:
            raise RuntimeError('Trial source does not match a unique fresh observed block')
        st=self.status()
        if st['moving']!='V0' or st['power']!='V1' or st['attached']!=['V1']*3:
            raise RuntimeError('Stationary powered arm required')
        start=self.measured()
        if start[2]<75:raise RuntimeError('Trial must start at empty clearance')
        route=[('trial_above',source[:2]+[80.],120),('trial_pickup',source,120),
               ('trial_lift',source[:2]+[source[2]+20.],120)]
        if preview:
            failures=[];count=0
            for phase,target,_ in route:
                for p in [start]+points(start,target):
                    reply=self.query('M2222 X%.2f Y%.2f Z%.2f S0'%tuple(p));count+=1
                    if reply!='V1':failures.append(dict(phase=phase,xyz=p,reply=reply))
                start=target
            return dict(passed=not failures,failures=failures,checked_points=count,motion_sent=False,source=source)
        self.run_id=run;self.directory=self.base/'private/fixed-runs'/run
        self.directory.mkdir(parents=True,exist_ok=False);self.sources=[source]
        try:
            self.check(command);self.preflight(start,route,command)
            self.move(*route[0],command)
            contact=self.move(*route[1],command)
            before=self.capture(command)
            self.pumping=True;self.query('M2231 V1');time.sleep(1.5)
            self.move('trial_lift',contact[:2]+[contact[2]+20.],120,command)
            evidence=self.verify_images('grasp',before,command)
            # Return to the same contact geometry, never an unrelated ground reference.
            actual=self.move('trial_return',source,120,command)
            if abs(actual[2]-contact[2])>.8 or math.dist(actual[:2],contact[:2])>2:
                raise RuntimeError('Trial set-down differs from observed contact origin; retain suction')
            self.check(command);self.query('M2231 V0');self.pumping=False
            self.move('trial_exit',source[:2]+[80.],120,command)
            self.phase='complete';self.snapshot('trial_complete',dict(grasp=evidence,contact=contact,
                physical_tower_verified=False,returned_to_origin=True))
            return self.latest
        except Exception as exc:
            self.phase='stopped';self.fault=str(exc);self.snapshot('stopped');raise
        finally:self.send(b'M410\nM2000\n')

    def contact_trial(self, run, command):
        """One measured correction of the immediately preceding empty pickup error.

        Not a general lower/retry API. Never repeat after this run has started.
        """
        if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (run,command)):
            raise ValueError('Invalid trial identity')
        if self.run_id is not None:raise RuntimeError('Contact trial requires fresh executor')
        previous=json.loads((self.base/'private/fixed-progress.json').read_text())
        if (previous.get('phase')!='stopped' or previous.get('pumping') is not False or
                previous.get('fault')!='Empty arrival outside axis tolerances' or
                not 0<=time.time()-previous.get('time',0)<=900):
            raise RuntimeError('No recent empty pickup mismatch to correct')
        prior=self.base/'private/fixed-runs'/previous['run']
        events=[json.loads(v) for v in (prior/'events.jsonl').read_text().splitlines()]
        if not any(e['phase']=='trial_pickup' for e in events):
            raise RuntimeError('Prior mismatch was not a pickup trial')
        mismatch=next(e for e in reversed(events) if e['phase']=='empty_arrival_mismatch')['detail']
        source=mismatch['target'];start=self.measured()
        gap=start[2]-source[2]
        if (source[2]!=-43.04 or math.dist(start,mismatch['measured'])>1 or
                math.dist(start[:2],source[:2])>3 or not 1.5<=gap<=2.5):
            raise RuntimeError('Current pose differs from bounded correction case')
        marker=prior/'contact-correction.started';marker.open('x').close()
        self.run_id=run;self.directory=self.base/'private/fixed-runs'/run
        self.directory.mkdir(parents=True,exist_ok=False);self.sources=[source]
        corrected=[source[0],source[1],source[2]-gap]
        try:
            self.check(command)
            self.preflight(start,[('contact_feedback_step',corrected,60)],command)
            baseline=list(start)
            for i in range(1,math.ceil(gap/.5)+1):
                delta=min(i*.5,gap)
                target=source[:2]+[source[2]-delta]
                expected=source[:2]+[baseline[2]-delta]
                actual=self.move('contact_feedback_step',target,60,command,measured_reference=expected)
                if start[2]-actual[2]<.15:
                    raise RuntimeError('Contact correction stalled; no further downward motion')
                start=actual
            if abs(start[2]-source[2])>.8:
                raise RuntimeError('Corrected contact target not reached')
            before=self.capture(command)
            self.pumping=True;self.query('M2231 V1');time.sleep(1.5)
            self.move('trial_lift',source[:2]+[source[2]+20.],120,command)
            evidence=self.verify_images('grasp',before,command)
            self.move('trial_return_above',source[:2]+[source[2]+3.],120,command)
            # Only the same known command and feedback pair can release here.
            actual=self.move('contact_feedback_step',corrected,60,command,measured_reference=start)
            if abs(actual[2]-start[2])>.8 or math.dist(actual[:2],start[:2])>2:
                raise RuntimeError('Trial support origin not recovered')
            self.check(command);self.query('M2231 V0');self.pumping=False
            self.move('trial_exit',source[:2]+[80.],120,command)
            self.phase='complete';self.snapshot('trial_complete',dict(grasp=evidence,contact=start,
                command_contact=corrected,physical_tower_verified=False,returned_to_origin=True))
            return self.latest
        except Exception as exc:
            self.phase='stopped';self.fault=str(exc);self.snapshot('stopped');raise
        finally:self.send(b'M410\nM2000\n')

    def confirmed_contact_lift(self, run, command, expected):
        """Operator-confirmed current contact: lift only, never descend or translate."""
        if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (run,command)):
            raise ValueError('Invalid lift identity')
        if self.run_id is not None:raise RuntimeError('Lift requires a fresh executor')
        if (not isinstance(expected,list) or len(expected)!=3 or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in expected)):
            raise ValueError('Invalid observed pose')
        start=self.measured();st=self.status()
        if (st['power']!='V1' or st['moving']!='V0' or st['attached']!=['V1']*3 or
                math.dist(start,expected)>3 or not (200<=start[0]<=250 and 60<=start[1]<=100 and -45<=start[2]<=-37)):
            raise RuntimeError('Pose differs from operator-confirmed contact')
        self.run_id=run;self.directory=self.base/'private/fixed-runs'/run
        self.directory.mkdir(parents=True,exist_ok=False)
        self.sources=[start]
        try:
            self.check(command)
            target=start[:2]+[start[2]+20.]
            self.preflight(start,[('confirmed_trial_lift',target,120)],command)
            before=self.capture(command)
            self.pumping=True;self.query('M2231 V1');time.sleep(1.5)
            actual=self.move('confirmed_trial_lift',target,120,command)
            if not 17<=actual[2]-start[2]<=23 or math.dist(actual[:2],start[:2])>3:
                raise RuntimeError('Measured trial displacement rejected; retain suction')
            after=self.capture(command)
            evidence=self.vision.save(self.directory,'operator_trial',[before,after],
                dict(verified=False,operator_confirmed_contact=True,grasp_observation_pending=True,
                     origin=start,raised=actual),run,command,0,self.profile_hash)
            self.phase='awaiting_grasp_observation'
            self.snapshot(self.phase,evidence)
            return self.latest
        except Exception as exc:
            self.phase='stopped';self.fault=str(exc);self.snapshot('stopped');raise
        finally:self.send(b'M410\nM2000\n')

    def measured(self):
        if self.query('M2200') != 'V0':
            raise RuntimeError('Arm not stationary')
        angles = [float(v) for v in re.findall(r'[BLR](-?\d+(?:\.\d+)?)',self.query('P2200'))]
        if len(angles) != 3:
            raise RuntimeError('Invalid measured angles')
        p = [float(v) for v in re.findall(r'[XYZ](-?\d+(?:\.\d+)?)',self.query('M2221 B%.2f L%.2f R%.2f'%tuple(angles)))]
        if len(p) != 3 or not all(math.isfinite(v) for v in p):
            raise RuntimeError('Invalid measured position')
        return p

    def preflight(self, start, route, command):
        for _, target, _ in route:
            for p in [start]+points(start,target):
                self.check(command)
                if not (90<=p[0]<=(330 if self.sources else 290) and -100<=p[1]<=(260 if self.sources else 210) and -50<=p[2]<=175):
                    raise RuntimeError('Fixed path outside envelope')
                if self.query('M2222 X%.2f Y%.2f Z%.2f S0'%tuple(p)) != 'V1':
                    raise RuntimeError('Fixed path unreachable')
            start = target

    def snapshot(self, phase, detail=None):
        data = dict(run=self.run_id, layer=self.layer, phase=phase, pumping=self.pumping,
                    fault=self.fault, detail=detail, time=time.time())
        with (self.directory/'events.jsonl').open('a') as f:
            f.write(json.dumps(data)+'\n')
        self.latest = data
        temp = self.base/'private/fixed-progress.tmp'
        temp.write_text(json.dumps(data))
        temp.replace(self.base/'private/fixed-progress.json')

    def move(self, phase, target, feed, command, correction=False, measured_reference=None,supervised_arrival=False):
        self.check(command)
        self.query('G4 P800')
        origin = self.xyz()
        self.preflight(origin, [(phase,target,feed)], command)
        self.snapshot(phase, dict(start=origin,target=target))
        for p in points(origin,target):
            self.check(command)
            if self.query('P2234') != 'V1' or any(self.query('M2203 N%d'%i) != 'V1' for i in range(3)):
                raise RuntimeError('Arm power or motor hold lost')
            fired = threading.Event()
            def stop():
                fired.set()
                self.send(b'M410\nM2000\n')
            timer = threading.Timer(4,stop)
            timer.start()
            try:
                self.query('G0 X%.2f Y%.2f Z%.2f F%d'%tuple(p+[feed]))
                self.query('M400')
                while self.query('M2200') != 'V0':
                    self.check(command)
                    if fired.is_set():
                        raise RuntimeError('Segment timeout')
                    time.sleep(.02)
                if fired.is_set():
                    raise RuntimeError('Segment timeout')
            finally:
                timer.cancel()
                timer.join()
        self.halt()
        readings = []
        for _ in range(3):
            self.check(command)
            readings.append(self.measured())
            time.sleep(.05)
        if max(math.dist(a,b) for a in readings for b in readings)>1:
            raise RuntimeError('Unstable measured arrival')
        actual = readings[-1]
        arrival=target if measured_reference is None else measured_reference
        if measured_reference is not None and phase not in ('contact_feedback_step','clearance_feedback_once'):
            raise RuntimeError('Measured correction reference restricted to bounded contact step')
        xy, z = math.dist(actual[:2],arrival[:2]), abs(actual[2]-arrival[2])
        if measured_reference is not None and (xy>2 or z>.8):
            raise RuntimeError('Contact correction did not track the measured increment')
        if self.sources and phase == 'pickup':
            reference=layout_profile(self.config)['slots'][self.layer]['measured_xyz']
            reference_z=reference[2]
            z=abs(actual[2]-reference_z)
            if z>.8 or math.dist(actual[:2],reference[:2])>2:
                self.snapshot('pickup_reference_mismatch',dict(target=target,measured=actual,reference_z=reference_z))
                raise RuntimeError('Pickup outside independently verified slot reference')
        if (self.pumping or supervised_arrival) and math.dist(actual,arrival)>3:
            # One small horizontal correction at the known clearance plane only.
            if phase == 'carry' and not correction and 3<xy<=5 and z<=.5:
                marker = self.directory/('correction-%d'%self.layer)
                marker.open('x').close()
                return self.move('carry_correction_once', target[:2]+[actual[2]],120,command,True)
            raise RuntimeError('Loaded arrival outside 3 mm tolerance')
        if not self.pumping and not supervised_arrival and (xy>5 or z>2):
            self.snapshot('empty_arrival_mismatch',dict(target=target,measured=actual))
            raise RuntimeError('Empty arrival outside axis tolerances')
        self.snapshot(phase+'_complete',dict(target=target,measured=actual))
        return actual

    def build_route(self, layer):
        if self.sources:
            source=self.sources[layer]
            down=station_target(self.structure,layer,self.config)[1]
            high=max(80.,station_target(self.structure,layer,self.config)[2])
            return [('source_above',source[:2]+[80.],300),('pickup',source,120),
                    ('probe_lift',source[:2]+[source[2]+20.],120),
                    ('lift',source[:2]+[high],300),('carry',down[:2]+[high],300),('place',down,120)]
        return station_build_route(self.structure,layer,self.config)

    def exit_route(self, layer):
        if self.sources:
            down=station_target(self.structure,layer,self.config)[1]
            high=max(80.,station_target(self.structure,layer,self.config)[2])
            if layer+1<len(self.sources):
                next_source=self.sources[layer+1]
                return [('exit_up',down[:2]+[high],300),('next_source_above',next_source[:2]+[high],300)]
            return [('exit_up',down[:2]+[high],300),('exit_side',[175.,150.,high],300),
                    ('observer_low',[175.,150.,115.],300),('observer',[180.,-60.,115.],300)]
        return station_exit_route(self.structure,layer,self.config)

    def target(self, index):
        return station_target(self.structure,index,self.config)

    def automatic_reference(self,index):
        references=self.config.get('automatic_release_references',{})
        if references.get('verified') is not True or self.structure=='pyramid3':
            raise RuntimeError('Automatic release heights are not field verified for this structure')
        rows=references.get('measured_xyz',[])
        if len(rows)!=3:
            raise RuntimeError('Missing automatic release references')
        reference=rows[index]
        nominal=self.target(index)[1]
        if (not isinstance(reference,list) or len(reference)!=3
                or any(type(v) not in (float,int) or not math.isfinite(v) for v in reference)
                or math.dist(reference,nominal)>3):
            raise RuntimeError('Automatic release reference invalid')
        return reference

    def release_at_reference(self,command,reference=None):
        reference=reference if reference is not None else self.automatic_reference(self.layer)
        for _ in range(3):
            self.check(command)
            actual=self.measured()
            if math.dist(actual[:2],reference[:2])>2 or abs(actual[2]-reference[2])>.8:
                raise RuntimeError('Automatic supported-height reference not reached; vacuum retained')
            time.sleep(.05)
        self.check(command)
        self.query('M2231 V0')
        self.pumping=False
        self.snapshot('automatic_release',dict(measured=actual,reference=reference,
                      verification='field_calibrated_geometry',contact_sensor=False))
        time.sleep(.5)

    def capture(self, command, after=None):
        return self.vision.capture(lambda:self.check(command), time.monotonic() if after is None else after)

    def source_pixel(self):
        k1,b1,k2,b2=map(float,self.calibration_path.read_text().split())
        x,y,_=self.sources[self.layer]
        return [(y-b2)/k2,(x-b1-5)/k1]

    def verify_images(self, kind, before, command):
        self.snapshot('grasp_verifying' if kind=='grasp' else 'placement_verifying')
        captures=[before]
        result=None
        try:
            for _ in range(3):
                captures.append(self.capture(command))
                if kind=='grasp':
                    result=verify_lift(before['frame'],captures[-1]['frame'],self.source_pixel())
            if kind=='placement':
                if self.layer==2:
                    until=time.monotonic()+5
                    while time.monotonic()<until:
                        # Check the whole five-second dwell, not just its endpoints.
                        captures.append(self.capture(command))
                        verify_placement(before['frame'],[c['frame'] for c in captures[1:]],
                                         layout_profile(self.config)['placement_rois'][self.layer],self.source_pixel())
                result=verify_placement(before['frame'],[c['frame'] for c in captures[1:]],
                                        layout_profile(self.config)['placement_rois'][self.layer],self.source_pixel())
        except Exception as exc:
            self.vision.save(self.directory,kind,captures,dict(verified=False,error=str(exc)),
                             self.run_id,command,self.layer,self.profile_hash)
            raise
        evidence=self.vision.save(self.directory,kind,captures,result,self.run_id,command,self.layer,self.profile_hash)
        self.snapshot(kind+'_verified',evidence)
        return evidence

    def execute(self, action, run, command, levels=3, structure=None,sources=None,staging=None,profile_sha256=None,slot_version=None):
        structure = structure or {1:'single',2:'tower2',3:'tower3'}.get(levels)
        if structure not in ('single','tower2','tower3','pyramid3'):
            raise RuntimeError('Unsupported fixed structure')
        if structure not in self.config.get('verified_structures',['single','tower2','tower3']):
            raise RuntimeError('Structure has not been physically verified at this site')
        if not inspect_payload_geometry(structure,self.config)['passed']:
            raise RuntimeError('Nominal payload geometry check failed')
        expected_count = {'single':1,'tower2':2,'tower3':3,'pyramid3':3}[structure]
        if levels != expected_count:
            raise RuntimeError('Structure block count differs')
        if type(levels) is not int or levels not in (1,2,3):
            raise RuntimeError('Unsupported fixed structure')
        sources=sources or []
        if staging is not None:
            raise RuntimeError('Automatic staging is disabled; keep tower footprint empty')
        if sources:
            if structure!='tower3' or levels!=3:
                raise RuntimeError('Fixed automatic mode supports only tower3')
            profile=layout_profile(self.config)
            if profile_sha256!=self.profile_hash or slot_version!=profile['version']:
                raise RuntimeError('Fixed slot profile identity changed or missing')
            if sources != [s['command_xyz'] for s in profile['slots']]:
                raise RuntimeError('Automatic source coordinates differ from fixed slots')
        if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (run,command)):
            raise RuntimeError('Invalid fixed command identity')
        if self.run_id != run:
            if self.run_id and self.phase != 'complete':
                raise RuntimeError('Cannot replace incomplete run')
            directory = self.base/'private/fixed-runs'/run
            directory.mkdir(parents=True,exist_ok=False)
            self.run_id,self.directory,self.phase,self.layer = run,directory,'awaiting_prepare',0
            self.levels = levels
            self.structure = structure
            self.sources=sources
            self.staging=staging
            self.pumping,self.fault = False,None
        if levels != self.levels or structure != self.structure or sources!=self.sources or staging!=self.staging:
            raise RuntimeError('Cannot change structure during a run')
        self.check(command)
        expected = 'prepare0' if self.phase=='awaiting_prepare' else 'build%d'%self.layer if self.phase=='awaiting_feed' else 'release%d'%self.layer if self.phase=='awaiting_support' else None
        if action != expected:
            raise RuntimeError('Fixed stage mismatch; never replay commands')
        st = self.status()
        if st['power']!='V1' or st['moving']!='V0' or st['attached']!=['V1']*3:
            raise RuntimeError('Powered attached stationary arm required')
        marker = self.directory/(action+'-'+command)
        marker.open('x').close()
        building = action.startswith('build')
        preparing = action=='prepare0'
        route = [('observer_prepare',[180.,-60.,115.],300),('feed_ready',self.target(0)[0][:2]+[115.],300)] if preparing else self.build_route(self.layer) if building else self.exit_route(self.layer)
        if preparing and self.sources:
            route=[('source_prepare',self.sources[0][:2]+[80.],300)]
            previous=self.xyz()
            for i in range(self.levels):
                self.automatic_reference(i)
                full=self.build_route(i)+self.exit_route(i)
                self.preflight(previous,full,command)
                previous=full[-1][1]
        self.preflight(self.xyz(),route,command)
        try:
            before=grasp_before=None
            grasp_evidence=placement_evidence=None
            self.phase = 'building' if building else 'exiting'
            self.halt()
            if not building and not preparing:
                self.check(command)
                # The web command is issued only after explicit support confirmation.
                if math.dist(self.measured(),self.support)>1:
                    raise RuntimeError('Pose changed since support observation')
                self.query('M2231 V0')
                self.pumping=False
                time.sleep(1)
            for phase,target,feed in route:
                self.check(command)
                if phase == ('probe_lift' if self.sources else 'lift'):
                    self.pumping=True
                    self.query('M2231 V1')
                    time.sleep(1.5)
                actual=self.move(phase,target,feed,command)
                if self.sources and phase=='source_above':
                    before=self.capture(command)
                if self.sources and phase=='pickup':
                    # Compare contact-height -> 20 mm lift, as in the recorded
                    # successful trial; approach-height arm occlusion differs.
                    grasp_before=self.capture(command)
                if self.sources and phase=='probe_lift':
                    grasp_evidence=self.verify_images('grasp',grasp_before,command)
            if preparing:
                self.phase='awaiting_feed'
            elif building:
                self.support=actual
                if self.sources:
                    if not grasp_evidence or grasp_evidence.get('verified') is not True:
                        raise RuntimeError('Missing verified grasp; release rejected')
                    self.release_at_reference(command)
                    for phase,target,feed in self.exit_route(self.layer):
                        actual=self.move(phase,target,feed,command)
                    placement_evidence=self.verify_images('placement',before,command)
                    self.layer+=1
                    self.phase='complete' if self.layer==self.levels else 'awaiting_feed'
                else:self.phase='awaiting_support'
            else:
                self.layer+=1
                self.phase='complete' if self.layer==self.levels else 'awaiting_feed'
            self.snapshot(self.phase,dict(measured=actual))
            if building and self.sources:
                self.latest['automatic_released']=True
                self.latest.update(grasp=grasp_evidence,placement=placement_evidence,
                                   profile_sha256=self.profile_hash,slot_version=slot_version)
            return self.latest
        except Exception as exc:
            self.fault,self.phase=str(exc),'stopped'
            self.snapshot(self.phase)
            raise
        finally:
            self.send(b'M410\nM2000\n')  # Never drop vacuum during error cleanup.
