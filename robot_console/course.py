"""Explicit reuse of the installed warehouse course reference, in arm mm."""
import copy
import re
from pathlib import Path


def apply_course_reference(config):
    if config.get('course_profile') != 'warehouse_10cm':
        return config
    result = copy.deepcopy(config)
    if 'height_model' in result:
        raise ValueError('Choose course fixed heights or a measured height model, not both')
    if result.get('block_size_mm', [100, 100, 100]) != [100, 100, 100]:
        raise ValueError('warehouse_10cm requires the confirmed 100 mm blue blocks')
    result['block_size_mm'] = [100, 100, 100]
    result.setdefault('verification_mode', 'operator')
    result.setdefault('color_hsv_file', '~/color_block_HSV.txt')
    result.setdefault('rest', [120, 0, 35])
    result.setdefault('carry', [250, 0, 150])
    for source in result.get('sources', []):
        # Explicit profile selection opts into the actual installed course's
        # approach/pick heights. Individually specified site values take priority.
        source.setdefault('approach_z', 20.)
        source.setdefault('pickup_z', -50.)
    return result


def color_bounds(filename=None, color='blue'):
    if filename is None:
        return [95, 90, 80], [130, 255, 255]
    content = Path(filename).expanduser().read_text(encoding='utf-8-sig')
    found = []
    for line in content.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        name, sep, values = line.partition(':')
        if name.strip().lower() != color:
            continue
        if not sep or not re.fullmatch(r'[\d,\s]+', values):
            raise ValueError('Invalid HSV row for ' + color)
        values = [int(v) for v in re.split(r'[,\s]+', values.strip()) if v]
        if len(values) != 6:
            raise ValueError('HSV row requires exactly six integers')
        low, high = values[:3], values[3:]
        if any(not 0 <= a <= b <= maximum for a, b, maximum in zip(low, high, [179, 255, 255])):
            raise ValueError('Invalid OpenCV HSV bounds')
        found.append((low, high))
    if len(found) != 1:
        raise ValueError('HSV file must contain one unambiguous ' + color + ' row')
    return found[0]
