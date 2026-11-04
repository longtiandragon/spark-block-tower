"""HSV observations only: blue pixels are not proof of a stable stack."""
import cv2
import numpy as np


def detect_blue(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([95, 90, 80]), np.array([130, 255, 255]))
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
    boxes = []
    preview = frame.copy()
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:20]:
        area = cv2.contourArea(c)
        if area < max(100, frame.shape[0]*frame.shape[1]*0.001):
            continue
        x, y, w, h = cv2.boundingRect(c)
        boxes.append(dict(x=x, y=y, width=w, height=h, area=round(area)))
        cv2.rectangle(preview, (x, y), (x+w, y+h), (255, 180, 60), 2)
        cv2.putText(preview, "BLUE", (x, max(18, y-5)), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 180, 60), 2)
    return preview, boxes
