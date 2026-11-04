"""Validate a local site JSON without importing ROS or touching actuators.

Use YAML on the robot via rosparam; this helper accepts an exported JSON copy
so the PC needs no extra YAML dependency. It validates structure, not physics.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ros/spark_blocks_bridge/src'))
from spark_blocks_bridge.safety import validate_config


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site_json', type=Path)
    args = parser.parse_args()
    try:
        config = validate_config(json.loads(args.site_json.read_text(encoding='utf-8-sig')))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f'Invalid site configuration: {error}\n')
    print(f"Structure valid: {len(config['sources'])} source stations, {len(config['slots'])} placement slots. Physical calibration remains required.")
