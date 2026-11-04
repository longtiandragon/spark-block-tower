"""One-time height geometry and fail-closed measured suction evidence."""
import copy
import threading
import time

from .safety import finite, pose


def calibrated_heights(config, model):
    """Return a new site config; never overwrite independently measured poses.

    surface_z is the support surface in arm coordinates. tool_to_cup_z is
    TCP Z minus cup-mouth Z. Block height is physical height, all in mm.
    """
    if not isinstance(model, dict) or model.get('verified') is not True:
        raise ValueError('Height model requires a physically verified measurement')
    for name in ('block_height_mm', 'tool_to_cup_z_mm', 'compression_mm', 'clearance_mm'):
        if not finite(model.get(name)):
            raise ValueError('Finite height measurement required: ' + name)
    height, offset = model['block_height_mm'], model['tool_to_cup_z_mm']
    compression, clearance = model['compression_mm'], model['clearance_mm']
    if not 0 < height <= 300 or not -200 <= offset <= 200 or not 0 <= compression <= 2 or not 5 <= clearance <= 150:
        raise ValueError('Invalid block/tool/clearance geometry')
    result = copy.deepcopy(config)
    limits = result['limits']

    def levels(entry, layer):
        surface = entry.get('support_surface_z_mm')
        if not finite(surface):
            raise ValueError('Measured support_surface_z_mm required')
        down = surface + (layer + 1) * height + offset - compression
        above = down + clearance
        if not limits[2][0] <= down < above <= limits[2][1]:
            raise ValueError('Layer heights exceed arm workspace; do not clamp them')
        return down, above

    for source in result['sources']:
        down, above = levels(source, 0)
        if 'pickup_z' in source and abs(source['pickup_z'] - down) > .5:
            raise ValueError('Existing pickup_z conflicts with height model')
        if 'approach_z' in source and abs(source['approach_z'] - above) > .5:
            raise ValueError('Existing approach_z conflicts with height model')
        source.update(pickup_z=down, approach_z=above)
    for key, slot in result['slots'].items():
        layer = int(key.split('-')[2])
        if layer not in (0, 1, 2):
            raise ValueError('Invalid tower layer')
        down, above = levels(slot, layer)
        xy = slot.get('xy_mm')
        if not isinstance(xy, list) or len(xy) != 2:
            raise ValueError('Destination xy_mm required')
        for name, z in (('down', down), ('above', above)):
            target = pose(xy + [z], limits)
            if name in slot and any(abs(a-b) > .5 for a, b in zip(pose(slot[name], limits), target)):
                raise ValueError('Existing layer pose conflicts with height model')
            slot[name] = target
    result['height_model'] = copy.deepcopy(model)
    return result


class GraspMonitor:
    """Consumes measured switch/3D evidence; commands and current are not grip."""
    def __init__(self, max_age=.3, clock=time.monotonic):
        self.clock, self.max_age = clock, max_age
        self.lock = threading.RLock()
        self.last_seq, self.last_stamp = -1, float('-inf')
        self.received, self.streak_started = float('-inf'), float('-inf')
        self.count, self.holding = 0, None
        self.required_stamp = float('-inf')

    def begin(self, ros_now):
        with self.lock:
            self.required_stamp = ros_now
            self.count, self.holding = 0, None
            return self.clock()

    def update(self, data, ros_now):
        with self.lock:
            now = self.clock()
            if (not isinstance(data, dict) or type(data.get('seq')) is not int
                    or not finite(data.get('stamp')) or data['stamp'] <= 0 or not finite(ros_now)
                    or data.get('kind') not in ('vacuum_switch', 'vision_3d')
                    or type(data.get('holding')) is not bool
                    or not 0 <= ros_now-data['stamp'] <= self.max_age
                    or data['stamp'] < self.required_stamp
                    or data['seq'] <= self.last_seq or data['stamp'] <= self.last_stamp):
                self.count, self.holding = 0, None
                return False
            continuous = (now-self.received <= self.max_age and self.holding is True and data['holding'])
            self.last_seq, self.last_stamp, self.received = data['seq'], data['stamp'], now
            self.holding = data['holding']
            self.count = self.count+1 if continuous else int(data['holding'])
            if not continuous:
                self.streak_started = now
            return True

    def confirmed(self, after=float('-inf')):
        with self.lock:
            return (self.holding is True and self.count >= 3
                    and self.streak_started >= after
                    and .05 <= self.clock()-self.streak_started
                    and self.clock()-self.received <= self.max_age)

    def fault(self):
        with self.lock:
            if self.clock()-self.received > self.max_age:
                return 'Measured suction evidence stale; keep stopped and retain vacuum'
            if self.holding is not True:
                return 'Block holding lost or unknown; keep stopped and retain vacuum'
            return None
