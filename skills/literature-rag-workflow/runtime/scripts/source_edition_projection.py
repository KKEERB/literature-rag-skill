"""Preserve edition boundaries without changing bibliographic work identity.

The value is copied from the bound model, not inferred from a DOI or filename.
Edition projection does not grant source support or human acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path


def _same_json(left, right):
    # Python equality alone treats False == 0 and True == 1. An edition field
    # must keep the exact JSON type as well as its value.
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return encode(left) == encode(right)


def edition_projection(model, source_pdf_sha256, model_sha256):
    value = model.get('source_edition')
    if value is None or value == '':
        return None
    # Historical models also use a descriptive string. Keep its exact type and
    # content instead of converting it into an invented structured edition.
    if not ((isinstance(value, dict) and value)
            or (isinstance(value, str) and value.strip())):
        raise ValueError('source edition must be a nonempty object or string')
    if (model.get('source_pdf_sha256') != source_pdf_sha256
            or any(not isinstance(v, str) or re.fullmatch(r'[0-9a-f]{64}', v) is None
                   for v in (source_pdf_sha256, model_sha256))):
        raise ValueError('source edition PDF/model binding differs')
    # JSON serialization also rejects non-finite values; never silently coerce
    # an unrepresentable edition into another form.
    json.dumps(value, allow_nan=False)
    return {'schema': 'source-edition-projection-v1',
            'source_edition': copy.deepcopy(value),
            'inputs': {'source_pdf_sha256': source_pdf_sha256,
                       'model_sha256': model_sha256}}


def edition_provenance(projection):
    if projection is None:
        return {}
    return {'source_edition': copy.deepcopy(projection['source_edition']),
            'source_edition_binding': copy.deepcopy(projection['inputs'])}


def validate_edition_projection(model, source, declared):
    """New edition-bearing plans cannot use legacy-plan compatibility to omit it."""
    expected = edition_projection(model, source['source_pdf_sha256'], source['model_sha256'])
    provenance = json.loads(source['provenance_json'])
    if not isinstance(provenance, dict):
        raise ValueError('source edition provenance is not an object')
    actual = {key: provenance[key] for key in ('source_edition', 'source_edition_binding')
              if key in provenance}
    if not _same_json(declared, expected) or not _same_json(actual, edition_provenance(expected)):
        raise ValueError('publication source edition differs from reviewed model projection')


def resolve_edition_projection(row, source_files_available, cache=None):
    """Distinguish undeclared legacy editions from missing required metadata.

    The caller already checks the live PDF/model/RAG hashes. Within one retrieval
    request, read a source model at most once even when many chunks use it.
    """
    if not source_files_available:
        return {'state': 'source_unavailable', 'projection': None}
    row = dict(row)
    cache = cache if cache is not None else {}
    try:
        provenance = json.loads(row.get('provenance_json') or '{}')
        if not isinstance(provenance, dict):
            raise ValueError('source edition provenance is not an object')
        inputs = {key: row.get(key, '') for key in ('source_pdf_sha256', 'model_sha256')}
        model_path = Path(row['source_path']) / '02_machine_evidence_pack.json'
        key = ('source_edition_model', str(model_path), inputs['model_sha256'])
        if key not in cache:
            raw = model_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != inputs['model_sha256']:
                raise ValueError('source model changed during retrieval')
            try:
                cache[key] = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                # Legacy non-JSON sources have no edition contract. Existing
                # source-support policy remains responsible for those sources.
                cache[key] = None
        model = cache[key]
        expected = edition_projection(model, **inputs) if isinstance(model, dict) else None
        actual = {field: provenance[field] for field in ('source_edition', 'source_edition_binding')
                  if field in provenance}
        if expected is None and not actual:
            return {'state': 'not_declared', 'projection': None}
        if expected is not None and not actual:
            # Older canonical rows predate the stored projection. The live
            # hash-bound model is authoritative here; never guess an edition
            # from the bibliographic parent or silently discard its boundary.
            return {'state': 'reconstructed_from_hash_bound_model', 'projection': expected}
        if expected is None or not _same_json(edition_provenance(expected), actual):
            raise ValueError('required source edition is missing or differs from bound model')
        return {'state': 'valid', 'projection': expected}
    except (OSError, ValueError, TypeError, KeyError):
        return {'state': 'projection_mismatch', 'projection': None}


def bound_edition_projection(row, source_files_available, cache=None):
    """Compatibility accessor for callers needing only the qualified value."""
    return resolve_edition_projection(row, source_files_available, cache)['projection']
