import numpy as np
import pytest
from spark_blocks_bridge.depth_height import measure_height


def inputs():
    depth = np.ones((120, 160), dtype=float)
    mask = np.zeros(depth.shape, dtype=bool)
    mask[45:75, 60:100] = True
    depth[mask] = .9
    k = [150, 0, 80, 0, 150, 60, 0, 0, 1]
    return depth, k, mask


def test_depth_measures_block_height_but_does_not_claim_grip():
    result = measure_height(*inputs())
    assert result['height_mm'] == pytest.approx(100, abs=.01)
    assert result['support_rmse_mm'] < .01
    assert result['grip_verified'] is False


def test_invalid_depth_cannot_be_used_as_zero_height():
    depth, k, mask = inputs()
    depth[mask] = np.nan
    with pytest.raises(ValueError, match='Insufficient'):
        measure_height(depth, k, mask)


def test_mask_mixing_floor_and_top_is_rejected():
    depth, k, mask = inputs()
    depth[45:60, 60:100] = 1
    with pytest.raises(ValueError, match='ambiguous'):
        measure_height(depth, k, mask)
