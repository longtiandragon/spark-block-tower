"""Supervised three-block sequence with resumable grasp/support checkpoints.

Uses the same serial owner, motion primitive, lease and stop path as FixedRunner.
No restart between holding a load and continuing. No automatic retries.
"""
import json
import math
import re
import time
from pathlib import Path
try:
    from .fixed_runner import FixedRunner, points
except ImportError:
    from fixed_runner import FixedRunner, points


class OnsiteTower(FixedRunner):
    def begin(self, run, command, sources):
        if self.run_id is not None:raise RuntimeError('Onsite run already exists')
        if not re.fullmatch('[a-f0-9]{32}',run) or not re.fullmatch('[a-f0-9]{32}',command):
            raise ValueError('Invalid identity')
        if len(sources)!=2 or any(len(p)!=3 or not (110<=p[0]<=330 and -100<=p[1]<=260 and p[2]==-43.04) for p in sources):
            raise ValueError('Two visible source blocks required')
        start=self.measured()
        if not (215<=start[0]<=230 and 75<=start[1]<=90 and -27<=start[2]<=-17):
            raise RuntimeError('Not at the operator-supported first block')
        self.run_id=run;self.directory=self.base/'private/fixed-runs'/run
        self.directory.mkdir(parents=True,exist_ok=False)
        self.sources=sources+[start]
        self.jobs=[dict(source=sources[0],target=[240.,-29.23,-43.04],high=80.,layer=0),
                   dict(source=sources[1],target=[240.,-29.23,56.96],high=80.,layer=1),
                   dict(source=start,target=[240.,-29.23,154.01],high=171.96,layer=2)]
        self.job=0;self.phase='ready';self.pumping=True  # Previous run may still hold suction.
        self.check(command)
        # Read-only preflight of every finite route before any pump/move.
        first_exit=[('supported_exit',start[:2]+[80.],120)]
        self.preflight(start,first_exit,command)
        previous=first_exit[-1][1]
        for i,job in enumerate(self.jobs):
            route=self.approach_route(job)+[('contact',job['source'],120)]
            route += [('test_lift',job['source'][:2]+[job['source'][2]+20.],120)] + self.carry_route(job)+self.withdraw(job)
            self.preflight(previous,route,command);previous=route[-1][1]
        self.snapshot('onsite_preflight_complete',dict(jobs=self.jobs,supervised=True))
        # The user explicitly supported the currently held block and withdrew.
        self.check(command);self.query('M2231 V0');self.pumping=False;time.sleep(.7)
        self._move(*first_exit[0],command)
        return self.step('pickup',command)

    def carry_route(self, job):
        source,target,high=job['source'],job['target'],job['high']
        if job['layer']==1 and math.hypot(*source[:2])>340:
            return [('lift_clear',source[:2]+[20.],120),('relay_low',[280.,172.,20.],120),
                    ('relay_raise',[280.,172.,80.],300),('carry',target[:2]+[80.],300),('place',target,120)]
        return [('lift_clear',source[:2]+[high],300),('carry',target[:2]+[high],300),('place',target,120)]

    def approach_route(self,job):
        if job['layer']==1 and math.hypot(*job['source'][:2])>340:
            return [('relay_approach',[280.,172.,80.],300),('approach',job['source'][:2]+[20.],120)]
        return [('approach',job['source'][:2]+[80.],300)]

    def withdraw(self,job):
        high=job['high'];target=job['target']
        return [('exit_up',target[:2]+[high],300),('exit_side',[175.,150.,high],300),
                ('observe_lower',[175.,150.,80.],300)] if job['layer']>=0 else [('exit_up',target[:2]+[80.],300)]

    def _move(self, phase, target, feed, command):
        # The previously successful supervised tower script used a 3 mm
        # Euclidean arrival check for the entire take/place action (including
        # empty contact). Keep this explicit; it is not grasp verification.
        try:
            return self.move(phase,target,feed,command,supervised_arrival=True)
        except RuntimeError as exc:
            clear=phase in ('approach','supported_exit','exit_up','exit_side','observe_lower','relay_approach')
            if self.pumping or not clear or 'arrival outside' not in str(exc):raise
            actual=self.measured()
            if min(target[2],actual[2])<75 or math.dist(actual,target)>5:raise
            corrected=[2*t-a for t,a in zip(target,actual)]
            if corrected[2]<75:raise
            self.snapshot('clearance_feedback_correction',dict(target=target,measured=actual,command=corrected))
            return self.move('clearance_feedback_once',corrected,120,command,
                             measured_reference=target,supervised_arrival=True)

    def resume_empty(self,run,command,previous_run):
        """Resume only the first empty approach fault, after an empty-arm restart."""
        if self.run_id is not None:raise RuntimeError('Executor already initialized')
        if not all(re.fullmatch('[a-f0-9]{32}',v or '') for v in (run,command,previous_run)):
            raise ValueError('Invalid identity')
        folder=self.base/'private/fixed-runs'/previous_run
        events=[json.loads(v) for v in (folder/'events.jsonl').read_text().splitlines()]
        last=events[-1]
        if last.get('phase')!='stopped' or last.get('pumping') is not False or 'arrival outside' not in last.get('fault',''):
            raise RuntimeError('No empty arrival failure to resume')
        if any(e['phase'] in ('test_lift','contact') for e in events):
            raise RuntimeError('Cannot resume after pickup began')
        jobs=next(e['detail']['jobs'] for e in events if e['phase']=='onsite_preflight_complete')
        start=self.measured()
        if math.dist(start,jobs[0]['source'][:2]+[80.])>5:raise RuntimeError('Current empty pose changed')
        (folder/'empty-resume.started').open('x').close()
        self.jobs=jobs;self.job=0;self.run_id=run;self.sources=[j['source'] for j in jobs]
        self.directory=self.base/'private/fixed-runs'/run;self.directory.mkdir(parents=True,exist_ok=False)
        self.pumping=False;self.phase='ready'
        self.snapshot('resumed_empty_approach',dict(previous_run=previous_run,jobs=jobs))
        return self.step('pickup',command)

    def step(self, action, command, evidence=None):
        if not re.fullmatch('[a-f0-9]{32}',command):raise ValueError('Invalid command')
        self.check(command)
        expected={'ready':'pickup','awaiting_grasp':'carry','awaiting_support':'release'}
        if expected.get(self.phase)!=action:raise RuntimeError('Unexpected onsite stage; do not replay')
        (self.directory/(str(self.job)+'-'+action+'.started')).open('x').close()
        job=self.jobs[self.job];self.layer=max(0,job['layer'])
        try:
            if action=='pickup':
                for phase,target,feed in self.approach_route(job):self._move(phase,target,feed,command)
                self._move('contact',job['source'],120,command)
                self.contact=self.measured();self.before=self.capture(command)
                self.pumping=True;self.query('M2231 V1');time.sleep(1.5)
                self._move('test_lift',self.contact[:2]+[self.contact[2]+20.],120,command)
                after=self.capture(command)
                self.vision.save(self.directory,'onsite_grasp',[self.before,after],
                    dict(verified=False,observation_pending=True,job=self.job),self.run_id,command,self.layer,self.profile_hash)
                self.phase='awaiting_grasp'
            elif action=='carry':
                if evidence not in ('operator_confirmed','camera_observed'):raise ValueError('Grasp observation required')
                self.snapshot('grasp_observed',dict(source=evidence,job=self.job))
                for phase,target,feed in self.carry_route(job):self._move(phase,target,feed,command)
                self.support=self.measured();self.phase='awaiting_support'
                self.capture(command)
            else:
                if evidence not in ('operator_confirmed','camera_observed'):raise ValueError('Support observation required')
                if math.dist(self.measured(),self.support)>1:raise RuntimeError('Support pose changed')
                self.snapshot('support_observed',dict(source=evidence,job=self.job,measured=self.support))
                self.query('M2231 V0');self.pumping=False;time.sleep(.7)
                for phase,target,feed in self.withdraw(job):self._move(phase,target,feed,command)
                self.job+=1
                self.phase='complete' if self.job==3 else 'ready'
            self.snapshot('onsite_'+self.phase,dict(job=self.job,placed=self.job,supervised=True))
            return self.latest
        except Exception as exc:
            self.fault=str(exc);self.phase='stopped';self.snapshot('stopped');raise
        finally:self.send(b'M410\nM2000\n')  # Routine halt, not the independent stop latch.
