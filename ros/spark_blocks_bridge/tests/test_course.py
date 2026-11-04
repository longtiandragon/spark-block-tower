import copy
import pytest
from spark_blocks_bridge.course import apply_course_reference, color_bounds
from spark_blocks_bridge.safety import validate_config


def test_course_profile_has_100mm_blocks_fixed_heights_and_no_pressure_requirement():
    original = dict(course_profile='warehouse_10cm', sources=[{'map_pose': [1, 0, 0]}])
    saved = copy.deepcopy(original)
    result = apply_course_reference(original)
    assert original == saved
    assert result['block_size_mm'] == [100, 100, 100]
    assert result['sources'][0]['pickup_z'] == -50
    assert result['sources'][0]['approach_z'] == 20
    assert result['rest'] == [120, 0, 35]
    assert result['carry'] == [250, 0, 150]
    assert result['verification_mode'] == 'operator'
    assert 'grasp_monitor' not in result
    assert 'commissioned' not in result


def test_course_profile_preserves_specific_height_and_rejects_mixed_geometry():
    config = dict(course_profile='warehouse_10cm', sources=[{'pickup_z': -48}])
    assert apply_course_reference(config)['sources'][0]['pickup_z'] == -48
    config['height_model'] = {}
    with pytest.raises(ValueError, match='not both'):
        apply_course_reference(config)


def test_hsv_text_controls_next_detection_and_supports_other_color_rows(tmp_path):
    path = tmp_path/'color_block_HSV.txt'
    path.write_text('blue:95,90,80 130,255,255\nred:0,90,80 10,255,255\n')
    assert color_bounds(str(path)) == ([95, 90, 80], [130, 255, 255])
    assert color_bounds(str(path), 'red') == ([0, 90, 80], [10, 255, 255])
    path.write_text('blue:96,100,90 129,250,245\n')
    assert color_bounds(str(path)) == ([96, 100, 90], [129, 250, 245])


@pytest.mark.parametrize('content', ['red:0,90,80 10,255,255', 'blue:95,90,80 180,255,255',
    'blue:130,90,80 95,255,255', 'blue:95,90,80 130,255',
    'blue:95,90,80 130,255,255\nblue:95,90,80 130,255,255', 'blue:nan,90,80 130,255,255'])
def test_invalid_or_ambiguous_hsv_never_silently_falls_back(tmp_path, content):
    path = tmp_path/'hsv.txt'
    path.write_text(content)
    with pytest.raises(ValueError):
        color_bounds(str(path))


def test_missing_hsv_file_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        color_bounds(str(tmp_path/'absent.txt'))
