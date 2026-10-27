#!/usr/bin/env python3
"""Prepare a new, reviewable version; never overwrite historical canonical files."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import json
import os
import shutil
import tempfile
import subprocess
import re
from pathlib import Path

from knowledge_quality import digest, object_digest
from knowledge_system_core import walk_chunk_items
from v2_core import load_profile, write_outputs

ROOT = Path(__file__).resolve().parents[1]


def reset_revision_review_metadata(generation):
    """Keep predecessor review history without claiming acceptance of new bytes."""
    keys = {'reviewer_task_id', 'reviewed_at', 'source_scope',
            'figure_selection_reason', 'substantive_figure_count'}
    historical = {key: copy.deepcopy(value) for key, value in generation.items()
                  if key in keys or key.startswith('visual_')}
    for key in historical:
        generation.pop(key, None)
    generation.update(reviewer_task_id='',
                      visual_review_status='pending_current_revision_acceptance',
                      visual_review_note='本修订的视觉验收另绑定当前模型哈希；历史视觉记录仅保留为前版本证据。',
                      human_reviewed=False, reviewer_confirmed=False,
                      human_check_status='NOT HUMAN CHECKED')
    return historical


def extract_source_region(pdf: Path, region: dict) -> tuple[str, dict]:
    """Re-extract a bounded original column; a producer's transcription is not proof.

    Coordinates are integer pixels at 72 dpi, from the top-left of the original
    MediaBox. Rotated or offset pages require a separately supported extractor.
    Literal presence still does not certify the meaning or the page attribution.
    """
    from pypdf import PdfReader
    routes = {'pdftotext_crop_v1': [], 'pdftotext_crop_layout_v1': ['-layout']}
    if not isinstance(region, dict) or region.get('extractor') not in routes:
        raise ValueError('unsupported source region extractor')
    page = region.get('pdf_page_index')
    bbox = region.get('bbox_72dpi')
    if (type(page) is not int or page < 1 or not isinstance(bbox, list) or len(bbox) != 4
            or any(type(n) is not int for n in bbox)):
        raise ValueError('source region requires a 1-based page and integer [x,y,width,height] at 72 dpi')
    reader = PdfReader(str(pdf), strict=False)
    if page > len(reader.pages):
        raise ValueError('source region page outside original PDF')
    original = reader.pages[page - 1]
    box = original.mediabox
    x, y, width, height = bbox
    if (original.rotation % 360 or float(box.left) != 0 or float(box.bottom) != 0
            or x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > float(box.width) or y + height > float(box.height)):
        raise ValueError('source region outside supported original page geometry')
    command = ['pdftotext', '-enc', 'UTF-8', *routes[region['extractor']], '-f', str(page), '-l', str(page), '-r', '72',
               '-x', str(x), '-y', str(y), '-W', str(width), '-H', str(height), str(pdf), '-']
    raw = subprocess.run(command, capture_output=True, check=True, timeout=60).stdout
    import hashlib
    actual = hashlib.sha256(raw).hexdigest()
    if not raw.strip() or region.get('text_sha256') != actual:
        raise ValueError('source region text differs from fresh bounded extraction')
    return raw.decode('utf-8'), dict(region, source_pdf_path=str(pdf.resolve()),
                                    source_pdf_sha256=digest(pdf), command=command)


def pointer_object(model, pointer):
    parts = pointer.strip('/').split('/')
    if parts[0] in {'reader_note', 'research_card'} and len(parts) >= 3:
        node, key = field_location(model, pointer)
        return node, key, node[key]
    if len(parts) != 2 or not parts[1].isdigit():
        raise ValueError('revision must address one existing atomic object')
    collection, index = parts[0], int(parts[1])
    if collection not in {'core_claims', 'methods', 'limitations', 'figures', 'self_test'}:
        raise ValueError('unsupported atomic revision collection')
    return model[collection], index, model[collection][index]


def field_location(model, pointer):
    parts = pointer.strip('/').split('/')
    if parts[0] in {'generation', 'doi', 'source_pdf_sha256', 'zotero_item_key', 'zotero_attachment_key'}:
        raise ValueError('propagation cannot change source identity or provenance')
    node = model
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part.replace('~1', '/').replace('~0', '~')]
    key = int(parts[-1]) if isinstance(node, list) else parts[-1].replace('~1', '/').replace('~0', '~')
    return node, key


def optional_reader_note_addition(original, model, pointer, edit):
    """Allow an absent-only core-work addition and exact existing short summary.

    IDs check referential integrity only; an independent reviewer must still
    verify that the authored relationships and prose are supported by the PDF.
    """
    if (pointer != '/reader_note' or edit.get('old_field_exists') is not False
            or 'old_value' not in edit or edit['old_value'] is not None):
        raise ValueError('add requires absent root /reader_note and explicit old_field_exists=false, old_value=null')
    if 'reader_note' in original or 'reader_note' in model:
        raise ValueError('optional reader_note addition is stale or conflicting: field already exists')
    value = edit.get('new_value')
    if (not isinstance(value, dict) or 'core_work' not in value
            or not set(value) <= {'core_work', 'one_sentence_summary'}):
        raise ValueError('optional reader_note addition permits core_work and an exact existing short summary only')
    if 'one_sentence_summary' in value:
        reading_map = original.get('reading_map')
        summary = reading_map.get('one_sentence_answer') if isinstance(reading_map, dict) else None
        if (not isinstance(summary, str) or not summary.strip() or len(summary) > 500
                or not isinstance(value['one_sentence_summary'], str)
                or value['one_sentence_summary'] != summary):
            raise ValueError('optional summary must exactly reuse original reading_map.one_sentence_answer within 500 characters')
    work = value['core_work']
    if not isinstance(work, list) or not 1 <= len(work) <= 3:
        raise ValueError('reader_note.core_work addition requires one to three explicit work items')
    known = {field: {item.get('id') for item in original.get(collection, [])
                     if isinstance(item, dict) and isinstance(item.get('id'), str)}
             for field, collection in (('claim_ids', 'core_claims'), ('method_ids', 'methods'))}
    prose = {'title', 'what_zh', 'why_zh', 'how_zh', 'result_zh'}
    for item in work:
        if (not isinstance(item, dict) or set(item) != prose | {'claim_ids', 'method_ids'}
                or any(not isinstance(item.get(key), str) or not item[key].strip() for key in prose)):
            raise ValueError('each added core work requires explicit prose and claim_ids/method_ids')
        for field in ('claim_ids', 'method_ids'):
            identifiers = item[field]
            if (not isinstance(identifiers, list) or (field == 'claim_ids' and not identifiers)
                    or any(not isinstance(identifier, str) or identifier not in known[field]
                           for identifier in identifiers)
                    or len(set(identifiers)) != len(identifiers)):
                raise ValueError('added core work contains invalid or unknown ' + field)
    return copy.deepcopy(value)


def validate_unique_chunk_inventory(model):
    """Apply the publisher's walker contract before any render/output writes."""
    locations = {}
    for identifier, path, _ in walk_chunk_items(model):
        pointer = ('/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1')
                                  for part in path)) if path else '<root>'
        locations.setdefault(identifier, []).append(pointer)
    duplicates = {identifier: paths for identifier, paths in locations.items()
                  if len(paths) > 1}
    if duplicates:
        raise ValueError('duplicate model chunk IDs and locations: '
                         + json.dumps(duplicates, ensure_ascii=False, sort_keys=True))


def prepare(proposal_paths, output: Path, *, producer_id='/root', renderer=write_outputs):
    proposals = [json.loads(Path(path).read_text(encoding='utf-8')) for path in proposal_paths]
    if not proposals or len({p['canonical_model_path'] for p in proposals}) != 1:
        raise ValueError('one source version per revision transaction')
    if output.exists():
        raise ValueError('use a new version directory; historical files are immutable')
    model_path = Path(proposals[0]['canonical_model_path'])
    original_dir = model_path.parent
    before = {p.name: digest(p) for p in original_dir.iterdir() if p.is_file()}
    original = json.loads(model_path.read_text(encoding='utf-8'))
    model = copy.deepcopy(original)
    fresh_literal_text = None
    region_text = []
    region_checks = []
    def validate_literal_change(pointer, old, new):
        nonlocal fresh_literal_text
        if old == new:
            return
        source_literal = pointer.endswith('/quote') or ('/key_sentences/' in pointer and pointer.endswith('/en'))
        if source_literal:
            if not isinstance(new, str) or not new.strip():
                raise ValueError('original wording must remain a nonempty string at ' + pointer)
            if fresh_literal_text is None:
                pdf_path = proposals[0]['source_verification']['source_pdf_path']
                fresh_literal_text = [subprocess.run(['pdftotext', '-enc', 'UTF-8'] + ([] if route == 'normal' else ['-' + route]) + [pdf_path, '-'],
                                                    capture_output=True, check=True, timeout=60).stdout.decode('utf-8')
                                      for route in ('normal', 'layout', 'raw')]
            literal = re.sub(r'\s+', '', new)
            if not any(literal in re.sub(r'\s+', '', text) for text in fresh_literal_text + region_text):
                raise ValueError('revision would replace original wording with non-source text at ' + pointer)
            return
        if isinstance(new, dict):
            previous = old if isinstance(old, dict) else {}
            for key, value in new.items():
                escaped = key.replace('~', '~0').replace('/', '~1')
                validate_literal_change(pointer + '/' + escaped, previous.get(key), value)
            return
        if isinstance(new, list):
            previous = old if isinstance(old, list) else []
            for index, value in enumerate(new):
                validate_literal_change(pointer + '/' + str(index),
                                        previous[index] if index < len(previous) else None, value)
            return
    changed = []
    analyst_notes = []
    pointers = set()
    # Validate each distinct region against the same bound PDF before any edit.
    # Multi-column extraction may interleave correct text, so a fresh crop is an
    # additional original-source route, never an analyst-supplied substitute.
    seen_regions = set()
    for proposal in proposals:
        pdf = Path(proposal['source_verification']['source_pdf_path'])
        if digest(pdf) != proposal['inputs']['source_pdf_sha256']:
            raise ValueError('current source inputs differ from reviewed proposal')
        regions = proposal['source_verification'].get('source_regions', [])
        if not isinstance(regions, list):
            raise ValueError('source_regions must be a list')
        for region in regions:
            key = object_digest({'pdf': str(pdf.resolve()), 'region': region})
            if key not in seen_regions:
                text, check = extract_source_region(pdf, region)
                region_text.append(text)
                region_checks.append(check)
                seen_regions.add(key)
    for proposal_index, proposal in enumerate(proposals):
        bindings = proposal['inputs']
        pdf = Path(proposal['source_verification']['source_pdf_path'])
        if (digest(pdf) != bindings['source_pdf_sha256'] or
                digest(model_path) != bindings['model_sha256'] or
                digest(original_dir / '02_rag_knowledge.md') != bindings['rag_sha256']):
            raise ValueError('current source inputs differ from reviewed proposal')
        edits = ([{'pointer': proposal['canonical_json_pointer'],
                   'old_value': proposal['old_value'], 'new_value': proposal['new_value'],
                   'old_value_sha256': bindings['assertion_sha256'], 'reason_zh': proposal['reason_zh']}]
                 if 'canonical_json_pointer' in proposal else
                 [{'pointer': e['json_pointer'], 'old_value': e['old_value'], 'new_value': e['new_value'],
                   'old_value_sha256': e['old_value_sha256'], 'reason_zh': e.get('classification', 'independently reviewed propagated correction')}
                  for e in proposal.get('remaining_propagation_edits', [])])
        if not edits:
            raise ValueError('revision proposal has no bound change')
        for edit in edits:
            pointer = edit['pointer']
            if pointer in pointers:
                raise ValueError('overlapping revision proposals')
            pointers.add(pointer)
            collection, index, old = pointer_object(model, pointer)
            if old != edit['old_value'] or object_digest(old) != edit['old_value_sha256']:
                raise ValueError('user edit or stale proposal at ' + pointer)
            new = edit['new_value']
            if new == old or (isinstance(old, dict) and 'id' in old and
                              (not isinstance(new, dict) or new.get('id') != old['id'])):
                raise ValueError('revision must retain identity and change content')
            validate_literal_change(pointer, old, new)
            collection[index] = copy.deepcopy(new)
            changed.append({'proposal_id': proposal['proposal_id'],
                            'proposal_path': str(Path(proposal_paths[proposal_index]).resolve()),
                            'proposal_sha256': digest(Path(proposal_paths[proposal_index])),
                            'pointer': pointer,
                            'old_sha256': object_digest(old), 'new_sha256': object_digest(new),
                            'reason_zh': edit['reason_zh']})
        if proposal.get('new_analyst_note_proposal'):
            note = proposal['new_analyst_note_proposal']
            used = {item['id'] for item in model.get('limitations', [])}
            index = 1
            while 'L%03d' % index in used:
                index += 1
            identifier = 'L%03d' % index
            inference = {
                'id': identifier, 'chunk_id': str(model['paper_id']) + ':' + identifier,
                'source_kind': 'model', 'epistemic_status': 'codex_inference',
                'support_level': 'indirect', 'verification_status': 'needs_human_check',
                'text': 'Analyst inference: using unprojected three-dimensional angles may change boundary classifications; the paper does not report this comparison.',
                'text_zh': note['text_zh'],
                'evidence': copy.deepcopy(new.get('evidence', {})),
                'retrieval_queries_zh': ['投影角与三维角分类的敏感性比较；分析者推论，尚未实测'],
                'retrieval_queries_en': ['projected versus three-dimensional angles sensitivity analyst inference'],
                'related_chunk_ids': [new['chunk_id']],
                'usable_for': ['待验证的方法敏感性问题'],
                'cannot_support': note['cannot_support']}
            model.setdefault('limitations', []).append(inference)
            analyst_notes.append({'id': identifier, 'object_sha256': object_digest(inference),
                                  'derived_from_proposal_id': proposal['proposal_id'],
                                  'scientific_claim_support': False})
    propagation_edits = []
    for proposal in proposals:
        for edit in proposal.get('propagation_edits', []):
            pointer = edit.get('pointer') or edit.get('json_pointer')
            operation = edit.get('operation')
            if operation == 'add':
                value = optional_reader_note_addition(original, model, pointer, edit)
                validate_literal_change(pointer, None, value)
                model['reader_note'] = value
                propagation_edits.append(dict(edit, pointer=pointer, new_field_exists=True,
                                              new_value_sha256=object_digest(value)))
                continue
            if operation not in (None, 'replace'):
                raise ValueError('unsupported propagation operation')
            if pointer == '/reader_note' and 'reader_note' not in original:
                raise ValueError('absent reader_note requires an explicit add operation')
            validate_literal_change(pointer, edit['old_value'], edit['new_value'])
            old_node, old_key = field_location(original, pointer)
            if old_node[old_key] != edit['old_value']:
                raise ValueError('stale dependent content at ' + pointer)
            node, key = field_location(model, pointer)
            if node[key] not in (edit['old_value'], edit['new_value']):
                raise ValueError('conflicting dependent revisions at ' + pointer)
            node[key] = copy.deepcopy(edit['new_value'])
            propagation_edits.append(dict(edit, pointer=pointer))
    for change in changed:
        change['intermediate_object_sha256'] = change['new_sha256']
        change['new_sha256'] = object_digest(pointer_object(model, change['pointer'])[2])
        change['final_object_sha256'] = change['new_sha256']
    # Historical human declarations remain in the old source and in this
    # manifest. A newly generated version stays draft until separately accepted.
    model['status'] = 'draft'
    model.setdefault('generation', {})['producer_task_id'] = producer_id
    model['generation']['generated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    historical_review_metadata = reset_revision_review_metadata(model['generation'])
    model['generation']['revision'] = {
        'parent_model_sha256': digest(model_path),
        'parent_source_id': proposals[0]['source_id'],
        'changes': changed, 'propagation_edits': propagation_edits,
        'historical_review_metadata': historical_review_metadata,
        'analyst_notes_preserved': analyst_notes, 'source_region_checks': region_checks,
        'scientific_promotion': False}
    validate_unique_chunk_inventory(model)
    profile = load_profile(ROOT / 'profiles' / (model['project_profile'] + '.json'))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=output.name + '.building-', dir=output.parent))
    try:
        # Metadata and inventory accompany the new version. Rendered scientific
        # content is always rebuilt from the changed model, never patched in HTML.
        for filename in ('00_metadata.yaml', 'paper_manifest.json'):
            if (original_dir / filename).is_file():
                shutil.copy2(original_dir / filename, temporary / filename)
        renderer(temporary, model, profile)
        rendered = {p.name: digest(p) for p in temporary.iterdir() if p.is_file()}
        current = {p.name: digest(p) for p in original_dir.iterdir() if p.is_file()}
        if current != before:
            raise ValueError('source artifacts changed during revision preparation')
        if any(digest(Path(p['source_verification']['source_pdf_path'])) != p['inputs']['source_pdf_sha256']
               for p in proposals):
            raise ValueError('original PDF changed during revision preparation')
        manifest = {
            'schema': 'knowledge-versioned-revision-v1',
            'stage': 'awaiting_independent_revision_review',
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'producer_task_id': producer_id,
            'source_id': proposals[0]['source_id'],
            'source_pdf_path': proposals[0]['source_verification']['source_pdf_path'],
            'source_pdf_sha256': proposals[0]['inputs']['source_pdf_sha256'],
            'historical_canonical': str(original_dir),
            'historical_artifact_hashes': before,
            'historical_model_status': original['status'],
            'new_artifact_hashes': rendered,
            'changes': changed,
            'propagation_edits': propagation_edits,
            'analyst_notes_preserved': analyst_notes,
            'source_region_checks': region_checks,
            'canonical_unchanged': True, 'zotero_modified': False,
            'knowledge_index_modified': False, 'scientific_promotion': False,
            'release_requirements': ['independent review bound to new model and artifacts',
                                    'live source identity and explicit project route',
                                    'current affected native-note key/version/raw-hash snapshot',
                                    'shared writeback lock and concurrent-writer checkpoint',
                                    'versioned per-source publication and exact cross-system readback']}
        (temporary / 'revision_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.rename(temporary, output)
    except BaseException:
        # This temporary directory was created by this transaction only.
        shutil.rmtree(temporary)
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proposal', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.proposal, args.output)
    print(json.dumps({'stage': result['stage'], 'changes': len(result['changes']),
                      'output': str(args.output), 'canonical_unchanged': True}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
