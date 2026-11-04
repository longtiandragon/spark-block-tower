"""Camera observations for a browser-only presentation. No robot commands."""
import base64
import cv2
import numpy as np


def scene(body, source):
    frame=cv2.imdecode(np.frombuffer(body,np.uint8),cv2.IMREAD_COLOR)
    if frame is None or frame.shape[:2]!=(480,640):
        raise ValueError('需要 640×480 现场画面')
    hsv=cv2.cvtColor(frame,cv2.COLOR_BGR2HSV)
    candidates=[]
    for hue in (106,107,108,109,110):
        mask=cv2.inRange(hsv,np.array([95,90,80]),np.array([hue,255,255]))
        mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((11,11),np.uint8))
        found=[]
        for contour in cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[-2]:
            area=cv2.contourArea(contour)
            if area<4000:continue
            (_, _),(w,h),_=cv2.minAreaRect(contour)
            if not (7000<area<22000 and min(w,h)>70 and max(w,h)/min(w,h)<1.5):continue
            x,y,bw,bh=cv2.boundingRect(contour)
            m=cv2.moments(contour)
            found.append(dict(x=x,y=y,width=bw,height=bh,u=m['m10']/m['m00'],v=m['m01']/m['m00'],area=area))
        if len(found)==3:candidates.append(found)
    if not candidates:raise ValueError('当前画面未识别到三个完整顶面')
    found=max(candidates,key=lambda row:sum(p['area'] for p in row))
    found.sort(key=lambda p:p['u'])
    return dict(blocks=[dict(p,id=i+1) for i,p in enumerate(found)],source=source,
                image='data:image/jpeg;base64,'+base64.b64encode(body).decode(),
                simulation=True,hardware_motion=False)
