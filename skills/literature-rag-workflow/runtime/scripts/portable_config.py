#!/usr/bin/env python3
"""Create isolated runtime configuration; never import another user's records."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def create(home: Path) -> dict:
    root = home.expanduser().resolve()
    config_dir = root / 'config'
    config_dir.mkdir(parents=True, exist_ok=True)
    database = root / 'knowledge.sqlite3'
    fast_root = root / 'fast_pdf'
    if not (config_dir / 'workspace.json').exists():
        if database.exists() or database.is_symlink() or fast_root.exists() or fast_root.is_symlink():
            raise FileExistsError('workspace has pre-existing database or storage; choose a new directory')
    knowledge = {
        'schema_version': '1.0', 'database_path': str(database),
        'review_dir': str(root / 'review'), 'stale_after_seconds': 3600,
        'verify_pdf_hashes': True,
        'paper_model_globs': [],
        'human_review_policy': {'enabled': False},
        'translation_review_sidecars': [], 'additional_sources': [],
        'baseline': {'validation_mode': 'dynamic'},
        'fast_pdf_model_globs': [str(fast_root / 'published' / '*' / '*' / '02_machine_evidence_pack.json')],
    }
    manifest = {'schema': 'literature-rag-workspace/1', 'workspace': str(root),
                'database': str(database), 'fast_pdf_root': str(fast_root),
                'knowledge_config': str(config_dir / 'knowledge_system.json'),
                'model_settings': str(config_dir / 'model_settings.json'),
                'user_review_inherited': False}
    files = {config_dir / 'knowledge_system.json': knowledge,
             config_dir / 'workspace.json': manifest}
    for path, value in files.items():
        if path.exists():
            previous = json.loads(path.read_text(encoding='utf-8'))
            if previous != value:
                raise FileExistsError(f'configuration already exists and differs: {path}')
    for path, value in files.items():
        if not path.exists():
            with path.open('x', encoding='utf-8', newline='\n') as out:
                json.dump(value, out, ensure_ascii=False, indent=2)
                out.write('\n')
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.workspace), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
