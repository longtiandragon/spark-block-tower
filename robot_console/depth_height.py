"""Measure a visible block above the dominant support plane, in metres.

Caller MUST establish RGB/depth registration, fresh synchronized frames and
correct intrinsics. This measures object height, never cup seal or grasp.
"""
import numpy as np


def measure_height(depth_m, intrinsics, object_mask):
    depth = np.asarray(depth_m)
    mask = np.asarray(object_mask, dtype=bool)
    k = np.asarray(intrinsics, dtype=float).reshape(3, 3)
    if depth.ndim != 2 or depth.shape != mask.shape or not np.isfinite(k).all() or k[0, 0] <= 0 or k[1, 1] <= 0:
        raise ValueError('Aligned depth/mask and valid camera intrinsics required')
    v, u = np.indices(depth.shape)
    valid = np.isfinite(depth) & (depth > .1) & (depth < 3)
    points = np.stack(((u-k[0, 2])*depth/k[0, 0], (v-k[1, 2])*depth/k[1, 1], depth), axis=-1)
    floor = points[valid & ~mask]
    block = points[valid & mask]
    if len(floor) < 1000 or len(block) < 100:
        raise ValueError('Insufficient valid depth: support=%d, block=%d, colored_pixels=%d'
                         % (len(floor), len(block), int(mask.sum())))
    rng = np.random.RandomState(0)
    floor = floor[rng.choice(len(floor), min(3000, len(floor)), replace=False)]
    best = None
    for _ in range(120):
        a, b, c = floor[rng.choice(len(floor), 3, replace=False)]
        n = np.cross(b-a, c-a)
        length = np.linalg.norm(n)
        if length < 1e-8:
            continue
        n /= length
        d = -float(np.dot(n, a))
        inliers = np.abs(floor @ n+d) < .006
        if best is None or inliers.sum() > best.sum():
            best = inliers
    if best is None or best.mean() < .6:
        raise ValueError('No dominant stable support plane')
    support = floor[best]
    center = support.mean(axis=0)
    _, singular, axes = np.linalg.svd(support-center, full_matrices=False)
    if singular[1] < .1:
        raise ValueError('Support plane has insufficient spatial coverage')
    normal = axes[-1]
    if normal[2] > 0:
        normal = -normal
    intercept = -float(np.dot(normal, center))
    residual = support @ normal+intercept
    heights = block @ normal+intercept
    # A color mask containing floor, side faces or another object is not a
    # reliable top-plane measurement. Do not select a convenient percentile.
    median = float(np.median(heights))
    spread = float(np.percentile(heights, 90)-np.percentile(heights, 10))
    if not .005 <= median <= .3 or spread > .015:
        raise ValueError('Block top depth ambiguous or outside height range')
    return {'height_mm': median*1000, 'top_spread_mm': spread*1000,
            'support_rmse_mm': float(np.sqrt(np.mean(residual**2))*1000),
            'support_inlier_fraction': float(best.mean()), 'top_valid_points': len(block),
            'plane_normal': normal.tolist(), 'plane_intercept_m': intercept,
            'grip_verified': False}
