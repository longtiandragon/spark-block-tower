#!/usr/bin/env python3
"""Derive a reviewable site candidate from measured one-time height geometry."""
import argparse
import json
from pathlib import Path
import yaml
from spark_blocks_bridge.manipulation import calibrated_heights


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    config = yaml.safe_load(args.input.read_text(encoding='utf-8'))
    derived = calibrated_heights(config, config['height_model'])
    # Deriving geometry cannot establish driver/stop/transport commissioning.
    derived['commissioned'] = False
    with args.output.open('x', encoding='utf-8') as output:
        yaml.safe_dump(derived, output, allow_unicode=True, sort_keys=False)
    print(json.dumps({'output': str(args.output), 'commissioned': False,
                      'sources': len(derived['sources']), 'slots': list(derived['slots'])}))


if __name__ == '__main__':
    main()
