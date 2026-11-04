"""Site-specific top-face observations, never vacuum/contact evidence."""
import cv2
import numpy as np
from itertools import permutations


def sources_match(anchor, found, tolerance=3.):
    """Match physical observations independently of contour sorting jitter."""
    current=[p['xyz'] for p in found]
    return len(anchor)==len(current)==3 and any(
        all(np.linalg.norm(np.array(a[:2])-np.array(b[:2]))<=tolerance
            for a,b in zip(anchor,ordered)) for ordered in permutations(current))


def core_center(contour):
    """Weight the face interior; thin side-face/color spurs carry little weight."""
    x,y,w,h=cv2.boundingRect(contour)
    local=np.zeros((h+2,w+2),np.uint8)
    cv2.drawContours(local,[contour-[x-1,y-1]],-1,255,-1)
    distance=cv2.distanceTransform(local,cv2.DIST_L2,5)
    weight=distance**4
    yy,xx=np.indices(weight.shape)
    return (float((weight*xx).sum()/weight.sum()+x-1),
            float((weight*yy).sum()/weight.sum()+y-1))


def source_plan(sources, config=None):
    try:
        from .fixed_layout import match_slots
    except ImportError:
        from fixed_layout import match_slots
    return match_slots(sources, config or {})


def stable_sources(window):
    if len(window)!=12:return None
    values=np.array([[p['xyz'] for p in row] for row in window],dtype=float)
    if values.shape!=(12,3,3) or not np.isfinite(values).all():return None
    if np.max(np.std(values,axis=0))>1.5:return None
    if np.max(np.abs(np.mean(values[:6],axis=0)-np.mean(values[6:],axis=0)))>1.5:return None
    return np.median(values,axis=0).tolist()


def detect_sources(frame,coeff):
    candidates=[]
    for upper in (110,109,108,107,106,105):
        try:
            found=_detect_top_faces(frame,coeff,upper)
            candidates.append(found)
        except ValueError:
            continue
    if not candidates:
        raise ValueError('Need three complete separate top faces; adjacent colors are ambiguous')
    # Largest accepted top-face area minimizes clipping from a narrow hue band.
    return max(candidates,key=lambda row:sum(p['area'] for p in row))


def _detect_top_faces(frame,coeff,upper):
    if frame.shape[:2]!=(480,640) or len(coeff)!=4:
        raise ValueError('Camera calibration dimensions differ')
    mask=cv2.inRange(cv2.cvtColor(frame,cv2.COLOR_BGR2HSV),np.array([95,90,80]),np.array([upper,255,255]))
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((11,11),np.uint8))
    contours=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[-2]
    found=[]
    for c in contours:
        area=cv2.contourArea(c)
        if area<4000:continue
        (u,v),(w,h),angle=cv2.minAreaRect(c)
        if not (7000<area<22000 and min(w,h)>70 and max(w,h)/min(w,h)<1.5):
            raise ValueError('Ambiguous blue top faces; no automatic pickup')
        if c[:,:,0].min()<4 or c[:,:,0].max()>635 or c[:,:,1].min()<4 or c[:,:,1].max()>475:
            raise ValueError('Block top clipped by camera border')
        u,v=core_center(c)
        x,y=coeff[0]*v+coeff[1]+5,coeff[2]*u+coeff[3]
        if not (110<=x<=330 and -100<=y<=260):
            raise ValueError('Detected source outside reviewed scan envelope')
        found.append(dict(pixel=[u,v],xyz=[x,y,-43.04],area=area))
    if len(found)!=3:raise ValueError('Need exactly three separate visible blue top faces')
    # Farther radial sources first; last source must support the high tower lift.
    # Clear the tower footprint before placing the first base. Then consume
    # radially distant sources before the high third-layer lift.
    def priority(p):
        x,y,_=p['xyz']
        blocks_destination=abs(x-240)<112 and abs(y+29.23)<112
        return (not blocks_destination,-(x*x+y*y))
    return sorted(found,key=priority)
