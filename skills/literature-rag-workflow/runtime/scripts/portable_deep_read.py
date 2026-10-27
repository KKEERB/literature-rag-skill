#!/usr/bin/env python3
"""Create an independently source-bound deep-reading work packet.

This prepares files only: no model execution, human review, Zotero write, or
claim of reading is performed. Full-text translation is not part of the packet.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

import fast_pdf_intake as fast
import reading_sessions as sessions
from portable_identity import source_identity, validate_portable_identity
from v21_quality import text_checksum
from v2_core import load_profile, new_model_stub

ROOT = Path(__file__).resolve().parents[1]


def write_new(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def prepare(session_dir, workspace):
    workspace = Path(workspace).expanduser().resolve(strict=True)
    session_dir = Path(session_dir).expanduser().resolve(strict=True)
    if not session_dir.is_relative_to(workspace):
        raise ValueError('reading session must be inside the selected workspace')
    session, _events, _head = sessions.session_state(session_dir)
    sessions.check_current(session)
    source = session['paper']['source']
    parent_key, attachment_key = source_identity(source['source_id'], source.get('zotero_item_key'),
                                                 source.get('zotero_attachment_key'))
    provenance = json.loads(source.get('provenance_json') or '{}')
    if provenance.get('ingestion_mode') != 'fast_pdf_two_round':
        raise ValueError('this entry supports only an independently verified fast-intake source')
    manifest_path = Path(provenance.get('extraction_manifest_path') or '')
    expected_manifest_sha = (provenance.get('lineage') or {}).get('extraction_manifest_sha256')
    if not manifest_path.is_file() or not expected_manifest_sha or fast.sha(manifest_path) != expected_manifest_sha:
        raise ValueError('source extraction manifest identity changed')
    extraction = fast.verify_extraction(manifest_path, source['source_pdf_sha256'])
    source_pdf = Path(source['source_pdf_path']).resolve(strict=True)
    if fast.sha(source_pdf) != source['source_pdf_sha256']:
        raise ValueError('source PDF changed')
    profile_name = str(source.get('project_profile') or '')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', profile_name):
        raise ValueError('explicit safe project profile required')
    profile_path = ROOT / 'profiles' / (profile_name + '.json')
    if not profile_path.is_file():
        raise ValueError('project profile is unavailable')
    profile = load_profile(profile_path)
    quality = sessions.read(ROOT / 'profiles/content_quality_v21.json')
    target = session_dir / 'deep_reading_stage'
    if target.exists() or target.is_symlink():
        raise FileExistsError('deep-reading stage already exists; inspect or create a new session')
    target.mkdir()
    try:
        pages = []
        for record in extraction['pages']:
            number = record['pdf_page_index']
            routes = {}
            if record.get('low_text'):
                raise ValueError('a page needs manual OCR/inspection before deep reading')
            for route in ('normal', 'layout'):
                data = record['routes'][route]
                src = manifest_path.parent / data['path']
                raw = src.read_bytes()
                if fast.sha(src) != data['sha256']:
                    raise ValueError('source text cache changed while preparing stage')
                text = raw.decode('utf-8')
                rel = Path('extraction/pages') / f'page-{number:03d}.{route}.txt'
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open('xb') as stream:
                    stream.write(raw)
                routes[route] = {'path': rel.as_posix(), 'normalized_sha256': text_checksum(text),
                                 'sha256': data['sha256'], 'returncode': 0, 'reliable': True}
            pages.append({'pdf_page_index': number, 'routes': routes, 'selected_route': 'normal',
                          'selected_path': routes['normal']['path'], 'status': 'pass'})
        if fast.sha(source_pdf) != source['source_pdf_sha256'] or fast.sha(manifest_path) != expected_manifest_sha:
            raise ValueError('source changed during deep-reading preparation')
        paper_id = 'paper-' + source['source_pdf_sha256'][:16]
        manifest = {'schema': 'portable-deep-reading-source/1', 'paper_id': paper_id,
                    'source_id': source['source_id'], 'pdf_path': str(source_pdf),
                    'pdf_sha256': source['source_pdf_sha256'], 'page_count': len(pages),
                    'zotero_item_key': parent_key, 'zotero_attachment_key': attachment_key,
                    'project_profile': profile_name, 'document_mode': 'standard'}
        extraction_v21 = {'paper_id': paper_id, 'pdf_path': str(source_pdf),
                          'pdf_sha256': source['source_pdf_sha256'], 'page_count': len(pages),
                          'status': 'pass', 'unreliable_pages': [], 'pages': pages,
                          'source_fast_manifest_sha256': expected_manifest_sha}
        metadata = {'paper_id': paper_id, 'title': source.get('title') or paper_id,
                    'authors': source.get('authors') or '', 'year': source.get('year') or '',
                    'doi': source.get('doi') or '', 'pdf_path': str(source_pdf),
                    'zotero_item_key': parent_key, 'zotero_attachment_key': attachment_key}
        scaffold = new_model_stub(target, metadata, {'zotero_item_key': metadata['zotero_item_key']}, profile)
        scaffold['zotero_item_key'] = metadata['zotero_item_key']
        scaffold['zotero_attachment_key'] = metadata['zotero_attachment_key']
        validate_portable_identity(manifest, scaffold)
        write_new(target / 'paper_manifest.json', manifest)
        write_new(target / 'extraction/extraction_manifest.json', extraction_v21)
        write_new(target / 'project_profile.json', profile)
        write_new(target / 'quality_profile.json', quality)
        write_new(target / 'model.scaffold.json', scaffold)
        common = {'schema': 'portable-deep-reading-task/1', 'source_id': source['source_id'],
                  'source_pdf_path': str(source_pdf), 'source_pdf_sha256': source['source_pdf_sha256'],
                  'stage': str(target), 'paper_manifest': str(target / 'paper_manifest.json'),
                  'project_profile': str(target / 'project_profile.json'),
                  'quality_profile': str(target / 'quality_profile.json'),
                  'model_schema': str(ROOT / 'schemas/machine_evidence_pack.schema.json'),
                  'prompt': str(ROOT / 'docs/portable_deep_reading.md'),
                  'translation_required': False}
        write_new(target / 'producer-task.json', {**common, 'stage_role': 'producer',
                  'scaffold': str(target / 'model.scaffold.json'),
                  'output': str(target / '02_machine_evidence_pack.json')})
        write_new(target / 'reviewer-task.json', {**common, 'stage_role': 'independent_reviewer',
                  'model_input': str(target / '02_machine_evidence_pack.json'),
                  'output': str(target / 'review/review_report.json'),
                  'independent_source_first': True})
    except Exception:
        # Retain the incomplete isolated stage for diagnosis; never claim success.
        raise
    return {'status': 'prepared', 'stage': str(target), 'producer_task': str(target / 'producer-task.json'),
            'reviewer_task': str(target / 'reviewer-task.json'), 'model_executed': False,
            'human_reviewed': False, 'translation_required': False}


def finalize(stage, workspace):
    """Render reviewed notes in place; never alter model, review, source or existing notes."""
    import datetime as dt
    import local_stage_validation as plan
    from v2_core import markdown_to_zotero_html, render_reader_note, render_research_card

    workspace = Path(workspace).expanduser().resolve(strict=True)
    stage = Path(stage).expanduser().resolve(strict=True)
    if not stage.is_relative_to(workspace) or stage == workspace:
        raise ValueError('stage must be inside the selected workspace')
    names = {name for pair in plan.NOTE_FILES.values() for name in pair}
    # Exclude mutable validation caches; bind every other existing stage input.
    before = plan.stage_hashes(stage)
    model, source, _ = plan.validate_stage(stage, require_notes=False)
    profile = load_profile(stage / 'project_profile.json')
    date = dt.datetime.fromisoformat(model['generation']['generated_at']).date().isoformat()
    outputs = {}
    for kind, (markdown, html) in plan.NOTE_FILES.items():
        text = (render_reader_note(model, profile, today=date) if kind == 'reader_note'
                else render_research_card(model, profile, today=date))
        outputs[markdown] = text
        outputs[html] = markdown_to_zotero_html(text)
    for name, content in outputs.items():
        path = stage / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text(encoding='utf-8') != content)):
            raise ValueError('existing note conflicts; do not overwrite: ' + name)
    if plan.stage_hashes(stage) != before or plan.digest(source) != model['source_pdf_sha256']:
        raise ValueError('reviewed stage or source changed during rendering')
    for name, content in outputs.items():
        path = stage / name
        if not path.exists():
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    after = plan.stage_hashes(stage)
    if {k: v for k, v in before.items() if k not in names} != {k: v for k, v in after.items() if k not in names}:
        raise ValueError('reviewed stage changed during finalization; inspect without promotion')
    plan.validate_stage(stage)
    return {'status': 'finalized_draft_notes', 'stage': str(stage),
            'notes': {kind: {'markdown': str(stage / pair[0]), 'html': str(stage / pair[1])}
                      for kind, pair in plan.NOTE_FILES.items()},
            'model_sha256': plan.digest(stage / '02_machine_evidence_pack.json'),
            'review_sha256': plan.digest(stage / 'review/review_report.json'),
            'source_pdf_sha256': model['source_pdf_sha256'],
            'human_reviewed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--session', required=True); p.add_argument('--workspace', required=True)
    p = sub.add_parser('finalize'); p.add_argument('--stage', required=True); p.add_argument('--workspace', required=True)
    argv = sys.argv[1:]
    if argv and argv[0].startswith('--') and argv[0] not in ('--help',):
        argv = ['prepare', *argv]
    args = parser.parse_args(argv)
    result = prepare(args.session, args.workspace) if args.command == 'prepare' else finalize(args.stage, args.workspace)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
