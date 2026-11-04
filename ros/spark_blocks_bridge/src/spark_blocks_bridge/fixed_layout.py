"""Fixed three-slot contract. Candidate coordinates are NOT commissioning evidence."""
import math
from itertools import permutations

CANDIDATE_XY = [[310., 100.], [190., 235.], [195., 110.]]


def layout_profile(config):
    profile = config.get('fixed_slots', {})
    if not isinstance(profile.get('version'), str) or not profile['version']:
        raise ValueError('缺少固定料位配置版本；候选料位尚未验收')
    slots = profile.get('slots', [])
    if len(slots) != 3:
        raise ValueError('需要三个固定料位')
    for slot in slots:
        for key in ('command_xyz', 'measured_xyz'):
            p = slot.get(key)
            if (not isinstance(p, list) or len(p) != 3 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in p)):
                raise ValueError('料位缺少独立的指令/实测坐标')
        x, y, z = slot['command_xyz']
        if not (110 <= x <= 330 and -100 <= y <= 260 and -50 <= z <= -35):
            raise ValueError('固定料位超出工作区')
        if math.dist(slot['command_xyz'], slot['measured_xyz']) > 3:
            raise ValueError('料位实测与指令差异过大')
        if abs(x-240) < 112 and abs(y+29.23) < 112:
            raise ValueError('固定料位占用塔基；禁止临时整理')
    for i, a in enumerate(slots):
        for b in slots[i+1:]:
            if all(abs(a['command_xyz'][j]-b['command_xyz'][j]) < 112 for j in (0, 1)):
                raise ValueError('固定料位间隙不足')
    checks = ('camera_verified', 'ik_verified', 'collision_verified', 'vision_verified')
    if any(profile.get(k) is not True for k in checks):
        raise ValueError('固定料位的相机、路径、避碰或视觉核验尚未实测验收')
    if any(s.get('pickup_verified') is not True or not s.get('evidence_id') for s in slots):
        raise ValueError('三个料位尚未分别通过实际抓取验收')
    rois = profile.get('placement_rois', [])
    if len(rois) != 3:
        raise ValueError('缺少三层放置视觉区域')
    for roi in rois:
        if (not isinstance(roi, list) or len(roi) != 4 or
                any(type(v) is not int for v in roi) or
                not (0 <= roi[0] < roi[2] <= 640 and 0 <= roi[1] < roi[3] <= 480) or
                (roi[2]-roi[0])*(roi[3]-roi[1]) < 2500):
            raise ValueError('放置视觉区域无效')
    return profile


def match_slots(found, config):
    """Observe placement; never silently replace it with the candidate coordinates."""
    profile = layout_profile(config)
    targets = [s['command_xyz'] for s in profile['slots']]
    if len(found) != 3:
        raise ValueError('请将三块一次摆在三个固定料位')
    for ordered in permutations(found):
        if all(math.dist(p[:2], t[:2]) <= 3 for p, t in zip(ordered, targets)):
            return [list(p) for p in targets], None
    raise ValueError('积木偏离固定料位超过 3 mm；请按标记摆放，塔基保持空置')
