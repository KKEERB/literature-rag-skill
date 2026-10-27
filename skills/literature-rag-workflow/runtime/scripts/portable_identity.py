"""Source identity rules for portable PDF reading and explicit Zotero bindings."""
from __future__ import annotations

import re

KEY = re.compile(r'^[A-Z0-9]{8}$')
SHA = re.compile(r'^[0-9a-f]{64}$')


def identity_pair(parent, attachment):
    """Accept only two absent fields or two syntactically valid Zotero keys."""
    parent = '' if parent is None else parent
    attachment = '' if attachment is None else attachment
    if not isinstance(parent, str) or not isinstance(attachment, str):
        raise ValueError('Zotero identity fields must be strings')
    if (parent, attachment) == ('', ''):
        return parent, attachment
    if not KEY.fullmatch(parent) or not KEY.fullmatch(attachment):
        raise ValueError('Zotero parent and attachment must both be valid keys or both empty')
    return parent, attachment


def source_identity(source_id, parent, attachment):
    pair = identity_pair(parent, attachment)
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('source identity is required')
    if source_id.startswith('zotero:') and (not pair[0] or source_id != 'zotero:' + pair[0]):
        raise ValueError('Zotero source requires matching parent and attachment keys')
    return pair


def validate_portable_identity(manifest, model, review=None):
    """Check immutable source identity against model and optional review."""
    source_id = manifest.get('source_id')
    if 'zotero_item_key' not in manifest or 'zotero_attachment_key' not in manifest:
        raise ValueError('portable source manifest must bind both identity fields')
    pair = source_identity(source_id, manifest['zotero_item_key'], manifest['zotero_attachment_key'])
    if 'zotero_item_key' not in model or 'zotero_attachment_key' not in model:
        raise ValueError('model must contain both identity fields')
    if not all(isinstance(model[field], str) for field in ('zotero_item_key', 'zotero_attachment_key')):
        raise ValueError('model identity fields must be strings')
    if identity_pair(model['zotero_item_key'], model['zotero_attachment_key']) != pair:
        raise ValueError('model Zotero identity differs from portable source')
    digest = manifest.get('pdf_sha256')
    if not isinstance(digest, str) or not SHA.fullmatch(digest) or model.get('source_pdf_sha256') != digest:
        raise ValueError('model PDF identity differs from portable source')
    if source_id.startswith('pdf:') and source_id != 'pdf:' + digest:
        raise ValueError('PDF source ID differs from the bound source hash')
    if model.get('paper_id') != manifest.get('paper_id'):
        raise ValueError('model paper identity differs from portable source')
    if review is not None:
        if (review.get('paper_id') != manifest.get('paper_id') or
                review.get('attachment_key') != pair[1] or
                review.get('source_pdf_sha256') != digest):
            raise ValueError('review identity differs from portable source')
    return pair
