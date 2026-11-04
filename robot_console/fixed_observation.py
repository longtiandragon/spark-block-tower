"""Conservative image checks; no vacuum/contact sensor claims.

Thresholds require site validation. A missing/ambiguous image fails closed.
The camera producer stays independent of the serial motion owner.
"""
import json
import time
import uuid
from pathlib import Path

import cv2
import numpy as np


def blue_mask(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array([95, 90, 65]), np.array([112, 255, 255]))


def face(frame, center):
    mask = blue_mask(frame)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
    selected = [c for c in contours if cv2.contourArea(c) >= 3000
                and cv2.pointPolygonTest(c, tuple(map(float, center)), True) >= -25]
    if len(selected) != 1:
        raise ValueError('顶面遮挡、粘连或无法唯一识别，不能确认抓取')
    c = selected[0]
    if c[:,:,0].min()<4 or c[:,:,0].max()>635 or c[:,:,1].min()<4 or c[:,:,1].max()>475:
        raise ValueError('顶面被相机边缘截断')
    m = cv2.moments(c)
    return np.array([m['m10']/m['m00'], m['m01']/m['m00']]), cv2.contourArea(c), c


def verify_lift(before, after, center):
    if before.shape != (480, 640, 3) or after.shape != before.shape:
        raise ValueError('相机尺寸变化')
    p, area, contour = face(before, center)
    q, raised_area, _ = face(after, p)
    displacement = float(np.linalg.norm(q-p))
    ratio = raised_area/area
    # Independently track blue face features; arm movement alone is insufficient.
    mask = np.zeros(before.shape[:2], np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, -1)
    gray0 = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    gray1 = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    points = cv2.goodFeaturesToTrack(gray0, 100, .01, 5, mask=mask)
    if points is None or len(points) < 6:
        raise ValueError('顶面特征不足，不能确认随动')
    moved, status, _ = cv2.calcOpticalFlowPyrLK(gray0, gray1, points, None)
    back, back_status, _ = cv2.calcOpticalFlowPyrLK(gray1, gray0, moved, None)
    valid = (status.ravel()==1) & (back_status.ravel()==1) & (np.linalg.norm(back-points,axis=2).ravel()<1.5)
    vectors = (moved-points).reshape(-1, 2)[valid]
    if len(vectors)<6 or len(vectors)/len(points)<.7:
        raise ValueError('顶面跟踪不可靠')
    median = np.median(vectors, axis=0)
    coherent = float(np.mean(np.linalg.norm(vectors-median,axis=1)<3))
    # Static textured background must stay still; rejects camera shifts.
    excluded = cv2.dilate(blue_mask(before)|blue_mask(after), np.ones((31,31),np.uint8))
    bg = cv2.goodFeaturesToTrack(gray0, 150, .02, 10, mask=255-excluded)
    if bg is None or len(bg)<12:
        raise ValueError('背景特征不足，无法排除相机移动')
    bg1, bs, _ = cv2.calcOpticalFlowPyrLK(gray0, gray1, bg, None)
    bvec = (bg1-bg).reshape(-1,2)[bs.ravel()==1]
    if len(bvec)<12 or np.mean(np.linalg.norm(bvec,axis=1)<2)<.7:
        raise ValueError('相机或场景发生移动')
    if not (4<=displacement<=45 and .85<=ratio<=1.5 and
            4<=float(np.linalg.norm(median))<=45 and coherent>=.75 and np.linalg.norm(median-(q-p))<=5):
        raise ValueError('未确认积木随试提移动；可能空吸，停止横移')
    return dict(verified=True, displacement_px=displacement, area_ratio=ratio,
                coherent_fraction=coherent, method='blue_face_motion', contact_sensor=False)


def verify_placement(before, frames, roi, source_center):
    x0,y0,x1,y1 = roi
    masks = [blue_mask(f) for f in frames]
    patches = [m[y0:y1,x0:x1]>0 for m in masks]
    original = blue_mask(before)[y0:y1,x0:x1]>0
    if any(float(p.mean())<.6 for p in patches):
        raise ValueError('目标层顶面不完整或被遮挡')
    if float(np.mean(patches[0] != original))<.12:
        raise ValueError('目标区域没有足够变化，不能确认新增一层')
    if any(float(np.mean(p != patches[0]))>.05 for p in patches[1:]):
        raise ValueError('放置后积木未保持稳定')
    u,v = map(int, source_center)
    if not (30<=u<610 and 30<=v<450):
        raise ValueError('取料位置不可见')
    if any(float((m[v-25:v+25,u-25:u+25]>0).mean())>.15 for m in masks):
        raise ValueError('源料位仍有蓝色物体或遮挡，不能确认搬运')
    return dict(verified=True, method='target_change_source_empty_stable', contact_sensor=False)


class FileVision:
    def __init__(self, base):
        self.path = Path(base)/'private/fixed-frame.npz'

    def capture(self, check, after):
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            check()
            try:
                with np.load(self.path, allow_pickle=False) as data:
                    at=float(data['at']); frame=data['frame'].copy(); sequence=str(data['sequence'])
                if after < at <= time.monotonic() and time.monotonic()-at<=.3 and frame.shape==(480,640,3):
                    return dict(frame=frame, at=at, sequence=sequence)
            except (OSError, ValueError, KeyError, EOFError):
                pass
            time.sleep(.03)
        raise RuntimeError('等待新的视觉核验画面超时；禁止继续搬运')

    @staticmethod
    def save(directory, kind, captures, result, run, command, layer, profile):
        evidence_id=uuid.uuid4().hex
        folder=Path(directory)/'evidence'/evidence_id
        folder.mkdir(parents=True, exist_ok=False)
        for i,c in enumerate(captures):
            if not cv2.imwrite(str(folder/('%02d.jpg'%i)),c['frame']):
                raise RuntimeError('视觉证据保存失败')
        record=dict(result, id=evidence_id, kind=kind, run=run, command=command, layer=layer,
                    profile_sha256=profile, frames=[dict(at=c['at'],sequence=c['sequence']) for c in captures])
        (folder/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
        return record
