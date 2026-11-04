#!/usr/bin/env python3
"""Fixed tower web adapter. No serial ownership; uses one private arm console."""
import json
import os
import re
import hashlib
import socket
import threading
import time
import uuid
from pathlib import Path
import requests
import rospy
import cv2
import numpy as np
from cv_bridge import CvBridge
from sensor_msgs.msg import Image


class Agent:
    def __init__(self):
        self.base=Path(os.environ['SPARK_FIXED_BASE']).resolve()
        self.url=os.environ['SPARK_FIXED_BACKEND'].rstrip('/')+'/api/fixed-tower'
        self.token=os.environ['SPARK_BRIDGE_TOKEN']
        if len(self.token)<16:
            raise RuntimeError('Bridge token missing')
        self.id=uuid.uuid4().hex
        self.lock=threading.RLock()
        self.busy,self.armed,self.fault=False,False,None
        if (self.base/'private/fixed-hardware.lock').exists():
            self.fault='实物动作已锁定：等待现场人员就绪后检查解锁。'
        self.last_ack=time.monotonic()
        self.backend_run=None
        self.result=None
        self.seen={}
        self.frame,self.frame_at=b'',float('-inf')
        self.status,self.status_at=None,float('-inf')
        self.console_protocol=False
        self.progress='等待网页任务'
        self.sources=[];self.sources_at=float('-inf');self.source_note='等待稳定的三个顶面'
        self.staging=None
        self.source_window=[]
        self.source_window_at=[]
        self.source_anchor=[]
        self.site=json.loads((self.base/'private/fixed-site.json').read_text())
        self.structures=self.site.get('verified_structures',['single','tower2','tower3'])
        self.slot_version=self.site.get('fixed_slots',{}).get('version')
        self.slots_ready=False
        self.site_hash=hashlib.sha256((self.base/'private/fixed-site.json').read_bytes()).hexdigest()
        self.calibration_sha256=None
        self.profile_sha256=None
        self.done=threading.Event()
        self.headers={'X-Bridge-Token':self.token}

    def console(self,action,run=None,command=None,levels=3,structure='tower3',sources=None,staging=None,**identity):
        payload={'action':action}
        if run is not None:
            payload.update(run=run,command=command,levels=levels,structure=structure,sources=sources or [],staging=staging)
            payload.update(identity)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(360 if action.startswith('fixed_') else 1)
            s.connect(str(self.base/'private/arm-console.sock'))
            s.sendall((json.dumps(payload)+'\n').encode())
            data=b''
            while not data.endswith(b'\n'):
                b=s.recv(65536)
                if not b or len(data)>1_000_000:
                    raise RuntimeError('Console disconnected')
                data+=b
        result=json.loads(data)
        if not result.get('ok'):
            raise RuntimeError(result.get('error','Console failed'))
        return result

    def stop(self,reason):
        with self.lock:
            if self.fault:
                return
            self.fault=str(reason)[:300]
        try:
            self.send_stop()
        except Exception as exc:
            rospy.logerr('Physical stop needs attention: '+str(exc))

    def send_stop(self):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(.3)
            s.connect(str(self.base/'private/arm-stop.sock'))
            s.sendall(b'{"action":"stop"}\n')
            response=json.loads(s.recv(2048))
            if response.get('stop_sent') is not True:
                raise RuntimeError('Stop write unconfirmed')

    def accept_poll(self,data):
        """Validate before renewing an existing lease or dispatching a worker."""
        if (not isinstance(data,dict) or type(data.get('armed')) is not bool
                or type(data.get('stop')) is not bool or not isinstance(data.get('run'),str)
                or not re.fullmatch('[a-f0-9]{32}',data['run'])):
            raise RuntimeError('Malformed backend lease')
        with self.lock:
            now=time.monotonic()
            if self.armed and now-self.last_ack>1:
                raise RuntimeError('Previous backend lease expired before renewal')
            if self.armed and self.backend_run is not None and data['run']!=self.backend_run:
                raise RuntimeError('Backend task session changed while armed')
            if self.busy and not data['armed']:
                raise RuntimeError('Motion lease revoked during action')
            if data['armed'] and now-self.frame_at>.6:
                raise RuntimeError('Camera expired before lease renewal')
            if data['armed'] and (self.base/'private/fixed-hardware.lock').exists():
                raise RuntimeError('Hardware lock active')
            cmd=data.get('command')
            if cmd is not None:
                if (not isinstance(cmd,dict) or cmd.get('run')!=data['run']
                        or not isinstance(cmd.get('id'),str) or not re.fullmatch('[a-f0-9]{32}',cmd['id'])
                        or cmd.get('action') not in ('prepare','build','release')
                        or type(cmd.get('levels')) is not int or cmd['levels'] not in (1,2,3)
                        or type(cmd.get('layer')) is not int or not 0<=cmd['layer']<cmd['levels']
                        or cmd.get('structure') not in self.structures
                        or {'single':1,'tower2':2,'tower3':3,'pyramid3':3}.get(cmd['structure'])!=cmd['levels']):
                    raise RuntimeError('Invalid fixed command')
                identity={k:cmd[k] for k in ('id','run','action','layer','levels','structure')}
                identity['sources']=cmd.get('sources',[])
                identity['staging']=cmd.get('staging')
                if cmd.get('sources'):
                    identity.update(profile_sha256=cmd.get('profile_sha256'),slot_version=cmd.get('slot_version'))
                    if (cmd.get('staging') is not None or cmd['structure']!='tower3' or
                            cmd.get('profile_sha256')!=self.site_hash or cmd.get('slot_version')!=self.slot_version):
                        raise RuntimeError('Fixed slot command/profile mismatch')
                if cmd['id'] in self.seen and self.seen[cmd['id']]!=identity:
                    raise RuntimeError('Command identity changed after receipt')
                if self.busy and cmd['id'] not in self.seen:
                    raise RuntimeError('Cannot queue fixed commands')
                if not data['armed']:
                    raise RuntimeError('Command supplied without active lease')
                if not self.console_protocol:
                    raise RuntimeError('Web console protocol is not ready')
            if data['stop']:
                self.stop('网页请求停止')
                return
            self.last_ack,self.armed,self.backend_run=now,data['armed'],data['run']
            if cmd and data['armed'] and not self.fault:
                lease=self.base/'private/fixed-lease.json'
                temporary=lease.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(run=cmd['run'],command=cmd['id'],at=now)))
                temporary.replace(lease)
                if cmd['id'] not in self.seen:
                    self.seen[cmd['id']]=identity
                    self.busy=True
                    threading.Thread(target=self.worker,args=(dict(cmd),),daemon=True).start()

    def image(self,msg):
        now=time.monotonic()
        if now-self.frame_at<.15:
            return
        if not 0 <= (rospy.Time.now()-msg.header.stamp).to_sec() <= .3:
            return
        frame=CvBridge().imgmsg_to_cv2(msg,'bgr8')
        # Persist fresh camera observations even while the motion worker is busy.
        try:
            path=self.base/'private/fixed-frame.npz'
            with path.with_suffix('.tmp').open('wb') as f:
                np.savez(f,frame=frame,at=now,sequence=uuid.uuid4().hex)
            path.with_suffix('.tmp').replace(path)
        except OSError as exc:
            self.stop('视觉核验画面保存失败: '+str(exc))
            return
        ok,data=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,75])
        if not self.armed and not self.busy:
            try:
                from auto_sources import detect_sources,stable_sources,source_plan,sources_match
                coeff=list(map(float,Path('/home/spark/thefile.txt').read_text().split()))
                found=detect_sources(CvBridge().imgmsg_to_cv2(msg,'bgr8'),coeff)
                if self.source_anchor and sources_match(self.source_anchor,found):
                    # Keep the accepted coordinates fixed; fresh images confirm
                    # them without requiring a new 12-frame lock on every tick.
                    self.sources_at=now
                    stable=self.source_anchor
                else:
                    if self.source_anchor:
                        self.sources=[];self.staging=None;self.source_anchor=[]
                        self.source_window=[];self.source_window_at=[]
                    self.source_window=(self.source_window+[found])[-12:]
                    self.source_window_at=(self.source_window_at+[now])[-12:]
                    stable=stable_sources(self.source_window) if now-self.source_window_at[0]<=4 else None
                if stable is not None:
                    self.sources,self.staging=source_plan(stable,self.site);self.sources_at=now;self.source_anchor=stable
                    self.slots_ready=True
                    self.source_note='三块与固定料位匹配；塔基空置，无临时搬运'
                else:self.source_note='正在积累稳定识别帧'
            except Exception as exc:
                # A single failed segmentation does not erase a fresh lock.
                # No new lease is granted: backend still requires age <= .5s.
                if now-self.sources_at>.5:
                    self.sources=[];self.staging=None;self.source_anchor=[]
                    self.source_window=[];self.source_window_at=[]
                    self.source_note=str(exc)[:200]
                    self.slots_ready=False
        if ok:
            with self.lock:
                self.frame,self.frame_at=data.tobytes(),now

    def worker(self,cmd):
        try:
            action='fixed_'+cmd['action']+str(cmd['layer'])
            identity={k:cmd[k] for k in ('profile_sha256','slot_version') if k in cmd} if cmd.get('sources') else {}
            r=self.console(action,cmd['run'],cmd['id'],cmd['levels'],cmd['structure'],cmd.get('sources',[]),cmd.get('staging'),**identity)
            with self.lock:
                if self.fault:
                    raise RuntimeError('Action finished after stop latch: '+self.fault)
            result=dict(id=self.id,command_id=cmd['id'],ok=True,details=r.get('details',{}))
        except Exception as exc:
            self.stop(exc)
            result=dict(id=self.id,command_id=cmd['id'],ok=False,details={'error':str(exc)})
        with self.lock:
            self.busy=False
            self.result=result

    def watchdog(self):
        while not self.done.wait(.05):
            with self.lock:
                expired=self.armed and time.monotonic()-self.last_ack>1
                camera_expired=self.armed and time.monotonic()-self.frame_at>.6
                hardware_locked=self.armed and (self.base/'private/fixed-hardware.lock').exists()
            if expired or camera_expired or hardware_locked:
                self.stop('网页/网络租约或相机反馈过期')

    def observe(self):
        while not self.done.wait(.15):
            with self.lock:
                busy=self.busy
            if busy:
                try:
                    p=json.loads((self.base/'private/fixed-progress.json').read_text())
                    if p.get('run')==self.backend_run:
                        phase=str(p.get('phase','执行中'))
                        self.progress={'probe_lift':'正在试提','grasp_verifying':'正在核验抓取',
                                       'grasp_verified':'抓取已核验','placement_verifying':'正在核验放置',
                                       'placement_verified':'放置已核验'}.get(phase,phase)
                    else:self.progress='正在准备当前任务'
                except (OSError,ValueError):
                    pass
                continue
            try:
                r=self.console('status')
                calibration_sha256=hashlib.sha256(Path('/home/spark/thefile.txt').read_bytes()).hexdigest()
                profile_sha256=hashlib.sha256((self.base/'private/fixed-site.json').read_bytes()).hexdigest()
                if profile_sha256!=self.site_hash:
                    self.stop('料位配置已变化；需检查并重新加载执行端')
                with self.lock:
                    self.status,self.status_at=r['status'],time.monotonic()
                    self.calibration_sha256=calibration_sha256
                    self.profile_sha256=profile_sha256
                    self.console_protocol=r.get('fixed_protocol')==2
                    if not self.console_protocol:
                        self.progress='只读控制台；等待现场切换网页执行版'
            except Exception:
                pass

    def frames(self):
        with requests.Session() as http:
            while not self.done.wait(.25):
                with self.lock:
                    frame,fresh=self.frame,time.monotonic()-self.frame_at<.5
                if frame and fresh:
                    try:
                        http.post(self.url+'/robot/frame',data=frame,
                            headers={**self.headers,'X-Bridge-Id':self.id,'Content-Type':'image/jpeg'},timeout=.4)
                    except requests.RequestException:
                        pass

    def run(self):
        rospy.Subscriber('/camera/rgb/image_raw',Image,self.image,queue_size=1)
        for f in (self.watchdog,self.observe,self.frames):
            threading.Thread(target=f,daemon=True).start()
        with requests.Session() as http:
            while not rospy.is_shutdown() and not self.done.wait(.1):
                with self.lock:
                    stopped=bool(not self.busy and self.status and self.status['moving']=='V0' and time.monotonic()-self.status_at<1)
                    ready=bool(stopped and self.status['power']=='V1' and self.status['attached']==['V1']*3
                               and self.console_protocol and not self.fault and time.monotonic()-self.frame_at<.5)
                    payload=dict(id=self.id,ready=ready,stopped=stopped,fault=self.fault,progress=self.progress,
                                 structures=self.structures,calibration_sha256=self.calibration_sha256,profile_sha256=self.profile_sha256)
                    payload.update(sources=self.sources,source_age=min(1e9,max(0,time.monotonic()-self.sources_at)),source_note=self.source_note)
                    payload['staging']=self.staging
                    payload.update(slot_version=self.slot_version,slots_ready=self.slots_ready)
                    result=self.result
                try:
                    response=http.post(self.url+'/robot/tick',json=payload,headers=self.headers,timeout=.4)
                    response.raise_for_status()
                    self.accept_poll(response.json())
                    if result:
                        r=http.post(self.url+'/robot/result',json=result,headers=self.headers,timeout=.4)
                        r.raise_for_status()
                        with self.lock:
                            if self.result is result:
                                self.result=None
                except requests.RequestException:
                    pass  # Independent watchdog/console lease handles loss, never replays.
                except Exception as exc:
                    self.stop(exc)
        self.stop('固定塔执行端退出')
        self.done.set()


if __name__=='__main__':
    rospy.init_node('fixed_tower_agent')
    Agent().run()
