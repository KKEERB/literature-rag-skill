#!/usr/bin/env python3
"""Manage user-selected two-round settings; this does not execute model calls.

The host must check account/model availability, supported effort, and whether
the selected settings can actually be passed to independent tasks. Saving a
setting or reading model-authored metadata is not an execution attestation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMA = 'literature-model-settings/1'
ROUNDS = ('analysis', 'review')
EFFORT_NAME = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')


def validate(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or set(value) != {'schema', *ROUNDS}:
        raise ValueError('invalid model settings schema; both rounds required')
    for stage in ROUNDS:
        setting = value[stage]
        if not isinstance(setting, dict) or set(setting) != {'model', 'reasoning_effort'}:
            raise ValueError(f'{stage} requires model and reasoning_effort')
        model = setting['model']
        if not isinstance(model, str) or not model.strip() or model != model.strip() or len(model) > 128:
            raise ValueError(f'{stage} model must be a nonempty identifier')
        effort = setting['reasoning_effort']
        if not isinstance(effort, str) or not EFFORT_NAME.fullmatch(effort):
            raise ValueError(f'{stage} reasoning effort is not a valid identifier')
    return value


def load(path):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))


def save(path, changes):
    path = Path(path).expanduser()
    value = load(path) if path.exists() else {'schema': SCHEMA, 'analysis': {}, 'review': {}}
    for stage in ROUNDS:
        for key, item in changes.get(stage, {}).items():
            if item is not None:
                value[stage][key] = item
    validate(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.model-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    show = commands.add_parser('show')
    show.add_argument('--path', type=Path, required=True)
    edit = commands.add_parser('set')
    edit.add_argument('--path', type=Path, required=True)
    for stage in ROUNDS:
        edit.add_argument(f'--{stage}-model')
        edit.add_argument(f'--{stage}-effort')
    args = parser.parse_args(argv)
    try:
        if args.command == 'show':
            value = load(args.path)
        else:
            changes = {stage: {'model': getattr(args, stage + '_model'),
                               'reasoning_effort': getattr(args, stage + '_effort')} for stage in ROUNDS}
            if all(item is None for change in changes.values() for item in change.values()):
                raise ValueError('provide at least one model or effort change')
            value = save(args.path, changes)
        print(json.dumps({'settings': value, 'availability': 'NOT_VERIFIED',
                          'actual_execution': 'NOT_VERIFIED'}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
