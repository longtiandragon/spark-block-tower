import copy
import pytest
from spark_blocks_bridge.auto_sources import stable_sources,detect_sources,source_plan,sources_match,core_center
import cv2
import numpy as np


def observations():
    return [[dict(xyz=[302.,181.,-43.04]),dict(xyz=[153.,235.,-43.04]),dict(xyz=[212.,134.,-43.04])] for _ in range(12)]


def test_stable_median_tolerates_isolated_contour_jitter():
    w=observations();w[2][0]['xyz'][0]+=3.1
    assert stable_sources(w)==[p['xyz'] for p in observations()[0]]


def test_real_movement_or_short_window_not_stable():
    w=observations()
    assert stable_sources(w[:11]) is None
    for row in w[6:]:row[0]['xyz'][0]+=4
    assert stable_sources(w) is None


def test_fresh_lock_matches_reordered_jitter_but_rejects_moved_cube():
    anchor=[p['xyz'] for p in observations()[0]]
    found=copy.deepcopy(observations()[0][::-1])
    found[0]['xyz'][0]+=1.2
    assert sources_match(anchor,found)
    found[0]['xyz'][0]+=4
    assert not sources_match(anchor,found)


def test_thin_color_spur_does_not_move_pickup_center_to_bounding_box_center():
    mask=np.zeros((200,230),np.uint8)
    cv2.rectangle(mask,(40,50),(140,150),255,-1)
    cv2.rectangle(mask,(140,90),(200,110),255,-1)
    contour=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[-2][0]
    center=core_center(contour)
    assert abs(center[0]-90)<2 and abs(center[1]-100)<1
    assert cv2.minAreaRect(contour)[0][0]-center[0]>20


def test_detector_requires_three_complete_top_faces():
    hsv=np.zeros((480,640,3),np.uint8)
    for x,y in [(140,280),(280,280),(420,280)]:cv2.rectangle(hsv,(x-45,y-45),(x+45,y+45),(104,160,170),-1)
    f=cv2.cvtColor(hsv,cv2.COLOR_HSV2BGR)
    coeff=[-1.,480.,-.5,250.]
    found=detect_sources(f,coeff)
    assert len(found)==3
    cv2.rectangle(f,(375,235),(465,325),(0,0,0),-1)
    with pytest.raises(ValueError,match='three complete'):detect_sources(f,coeff)


def test_staging_is_not_available_without_a_commissioned_fixed_layout():
    sources=[[325.,14.,-43.04],[231.,58.,-43.04],[328.,112.,-43.04]]
    with pytest.raises(ValueError):source_plan(sources)
