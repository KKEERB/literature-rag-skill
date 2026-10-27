#!/usr/bin/env python3
"""Check an explicit prospective release, or copy it to an isolated staging tree.

This tool never publishes. Automated scanning cannot replace a human review of
privacy, copyright, dependencies, platform behaviour or scientific claims.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = 'literature-release-manifest/1'
SKILL = 'skills/literature-rag-workflow/'
SENSITIVE_SUFFIXES = {'.pdf', '.db', '.sqlite', '.sqlite3', '.pyc', '.p12', '.pfx', '.pem', '.key'}
KEY_PATTERNS = {
    'home_path': re.compile(r'(?<![A-Za-z])/(?:Users|home)/[^/\s"\'<>]+/'),
    'windows_home_path': re.compile(r'(?i)\b[A-Z]:\\{1,2}Users\\{1,2}[^\\\s"\'<>]+\\{1,2}'),
    'chat_reference': re.compile(r'https?://chatgpt\.com/c/[A-Za-z0-9-]+'),
    'github_secret': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential_assignment': re.compile(r'(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*["\'][A-Za-z0-9_+/.=-]{20,}["\']'),
}
REVIEW_MARKERS = {
    'source_doi': re.compile(r'(?i)\b10\.\d{4,9}/[a-z0-9][a-z0-9._;()/:-]*'),
    'zotero_identity': re.compile(r'(?i)["\']?(?:collection_key|parent_key|attachment_key|zotero_item_key)["\']?\s*[:=]\s*["\'][A-Z0-9]{8}["\']'),
}
BASE_CORE_ATTESTATIONS = ('privacy_and_source_rights', 'third_party_licenses', 'dependency_closure',
                          'real_model_and_deep_reading')
PLATFORM_ATTESTATIONS = {'macos': 'macos_native', 'windows': 'windows_native'}
CORE_ATTESTATIONS = BASE_CORE_ATTESTATIONS + tuple(PLATFORM_ATTESTATIONS.values())
OPTIONAL_ATTESTATIONS = ('real_zotero_delivery',)
ATTESTATIONS = CORE_ATTESTATIONS + OPTIONAL_ATTESTATIONS
# Core-only candidates cannot ship the known Zotero integration entrypoints.
# This list is a conservative static check, not a complete dynamic security audit.
ZOTERO_WRITERS = frozenset(SKILL + 'runtime/scripts/' + name for name in (
    'portable_zotero_writer.py', 'portable_zotero_writer.js', 'writeback_zotero_sqlite.py',
    'writeback_zotero_api.py',
))
ZOTERO_INTEGRATION = ZOTERO_WRITERS | frozenset(SKILL + name for name in (
    'references/zotero-delivery.md',
    'runtime/scripts/portable_zotero.py', 'runtime/scripts/portable_zotero_delivery.py',
    'runtime/scripts/verify_revision_notes_native.js', 'runtime/scripts/promote_v21.py',
    'runtime/scripts/notes_delivery_plan.py', 'runtime/scripts/pipeline_v2.py',
    'runtime/scripts/prepare_revision_notes.py', 'runtime/scripts/native_foreground_window.py',
    'runtime/scripts/headless_writeback/__init__.py', 'runtime/scripts/headless_writeback/kb_adapter.py',
    'runtime/tests/test_portable_zotero.py', 'runtime/tests/test_portable_zotero_delivery.py',
    'runtime/tests/test_portable_zotero_writer.py', 'runtime/tests/test_portable_zotero_writer_harness.js',
))


def read_manifest(root: Path, manifest_name: str) -> dict:
    if (not manifest_name or '\\' in manifest_name or ':' in manifest_name
            or PurePosixPath(manifest_name).is_absolute()
            or any(part in ('', '.', '..') for part in manifest_name.split('/'))):
        raise ValueError('manifest path must be normalized and relative')
    path = root / manifest_name
    if path.is_symlink():
        raise ValueError('manifest cannot be a symlink')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema') != SCHEMA or not isinstance(value.get('files'), list):
        raise ValueError('invalid release manifest')
    if value.get('release_scope', 'full') not in ('full', 'core_only'):
        raise ValueError('invalid release_scope')
    platforms = value.get('release_platforms', ['macos', 'windows'])
    if (not isinstance(platforms, list) or not platforms or
            not all(isinstance(item, str) and item in PLATFORM_ATTESTATIONS for item in platforms) or
            len(platforms) != len(set(platforms))):
        raise ValueError('invalid release_platforms')
    names = value['files']
    if not names or not all(isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise ValueError('release files must be a nonempty unique list')
    for name in names:
        if not isinstance(name, str) or '\\' in name or not name or any(c in name for c in '\r\n\x00'):
            raise ValueError('invalid manifest path')
        part = PurePosixPath(name)
        if part.is_absolute() or any(bit in ('', '.', '..') for bit in name.split('/')) or ':' in name:
            raise ValueError('manifest path must be normalized and relative')
    if manifest_name not in names:
        raise ValueError('manifest must list itself')
    if names.count(SKILL + 'SKILL.md') != 1 or sum(name.endswith('/SKILL.md') for name in names) != 1:
        raise ValueError('release must contain exactly one skill')
    return value


def safe_file(root: Path, name: str) -> Path:
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink is not allowed')
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('missing, non-file or escaped release path')
    return resolved


def constant_string(node):
    """Read literal concatenations without executing source code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = constant_string(node.left), constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from json_strings(item)


def scan(root: Path, manifest_name: str, marker_file: Path | None = None, *, exact_tree: bool = False) -> dict:
    root = root.resolve(strict=True)
    manifest = read_manifest(root, manifest_name)
    files = manifest['files']
    findings, review, inventory, content = [], [], {}, {}
    release_scope = manifest.get('release_scope', 'full')
    writers_present = bool(set(files) & ZOTERO_WRITERS)
    integration_present = bool(set(files) & ZOTERO_INTEGRATION)
    if release_scope == 'core_only' and integration_present:
        findings.append({'file': 'release-manifest.json', 'rule': 'zotero_integration_in_core_only_candidate'})
    markers = []
    if marker_file is not None:
        if marker_file.is_symlink() or not marker_file.is_file():
            raise ValueError('private marker file must be an existing regular file')
        if marker_file.resolve().is_relative_to(root):
            raise ValueError('private marker file must be outside the release tree')
        markers = [s for s in marker_file.read_text(encoding='utf-8').splitlines() if len(s) >= 6]
        if not markers:
            raise ValueError('private marker file must contain at least one marker of six or more characters')
    for name in files:
        pure = PurePosixPath(name)
        if any(marker in name for marker in markers):
            findings.append({'file': '<redacted>', 'rule': 'private_marker_in_filename'})
        if (pure.suffix.lower() in SENSITIVE_SUFFIXES or any(p in {'.git', '__pycache__', 'work', 'reports', 'outputs'} for p in pure.parts)
                or pure.name == '.env' or pure.name.startswith('.env.')):
            findings.append({'file': name, 'rule': 'forbidden_file_type_or_directory'})
            continue
        try:
            path = safe_file(root, name)
            raw = path.read_bytes()
            if len(raw) > 5_000_000:
                raise ValueError('file exceeds 5 MB scan limit')
            text = raw.decode('utf-8')
            inventory[name] = hashlib.sha256(raw).hexdigest()
            content[name] = raw
            if name.endswith('.py'):
                tree = ast.parse(text, filename=name)
                for node in ast.walk(tree):
                    if isinstance(node, ast.BinOp):
                        joined = constant_string(node)
                        if joined is not None and any(marker in joined for marker in markers):
                            findings.append({'file': name, 'line': node.lineno, 'rule': 'private_marker_literal'})
            elif name.endswith('.json'):
                value = json.loads(text)
                if any(marker in item for item in json_strings(value) for marker in markers):
                    findings.append({'file': name, 'rule': 'private_marker_json_value'})
            for line, value in enumerate(text.splitlines(), 1):
                for rule, pattern in KEY_PATTERNS.items():
                    if pattern.search(value):
                        findings.append({'file': name, 'line': line, 'rule': rule})
                if any(marker in value for marker in markers):
                    findings.append({'file': name, 'line': line, 'rule': 'private_marker'})
                for rule, pattern in REVIEW_MARKERS.items():
                    if pattern.search(value):
                        review.append({'file': name, 'line': line, 'rule': rule})
        except (OSError, ValueError, SyntaxError, UnicodeError, json.JSONDecodeError) as exc:
            findings.append({'file': name, 'rule': 'unreadable_or_invalid', 'detail': type(exc).__name__})
    # Check static local imports against the source tree, not only the staged
    # tree, so an omitted module cannot hide simply by being absent there.
    runtime = root / SKILL / 'runtime'
    known_scripts = {p.stem: p for p in (runtime / 'scripts').glob('*.py')}
    known_tests = {p.stem: p for p in (runtime / 'tests').glob('*.py')}
    chosen = set(files)
    for name, raw in content.items():
        if not name.startswith(SKILL + 'runtime/') or not name.endswith('.py'):
            continue
        tree = ast.parse(raw.decode('utf-8'), filename=name)
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                ref = node.value
                if re.fullmatch(r'(?:scripts|docs|schemas|configs|profiles|templates|tests)/[A-Za-z0-9_./-]+', ref):
                    reference = runtime / ref
                    if reference.is_file():
                        relative = reference.relative_to(root).as_posix()
                        if relative not in chosen:
                            findings.append({'file': name, 'rule': 'unlisted_local_resource', 'dependency': relative})
        for module in modules:
            parts = module.split('.')
            candidate = known_scripts.get(parts[0]) or known_tests.get(parts[0])
            if candidate is None and len(parts) > 1:
                candidate = runtime / 'scripts' / Path(*parts).with_suffix('.py')
                if not candidate.is_file():
                    candidate = None
            if candidate is not None:
                relative = candidate.relative_to(root).as_posix()
                if relative not in chosen:
                    findings.append({'file': name, 'rule': 'unlisted_local_dependency', 'dependency': relative})
    for name, raw in content.items():
        if not name.startswith(SKILL) or not name.endswith('.md'):
            continue
        for link in re.findall(r'\]\(([^)]+)\)', raw.decode('utf-8')):
            target = link.strip().split('#', 1)[0]
            if not target or '://' in target or target.startswith('#'):
                continue
            candidate = (root / name).parent / target
            if candidate.exists():
                try:
                    relative = candidate.resolve().relative_to(root).as_posix()
                except ValueError:
                    findings.append({'file': name, 'rule': 'markdown_reference_outside_release'})
                    continue
                if relative not in chosen:
                    findings.append({'file': name, 'rule': 'unlisted_markdown_reference', 'dependency': relative})
            else:
                findings.append({'file': name, 'rule': 'missing_markdown_reference'})
    if exact_tree:
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink()}
        for name in sorted(actual - set(files)):
            findings.append({'file': name, 'rule': 'unlisted_file_in_release_tree'})
        for name in sorted(set(files) - actual):
            findings.append({'file': name, 'rule': 'missing_file_in_release_tree'})
    approved = {}
    for item in manifest.get('reviewed_content', []):
        if (not isinstance(item, dict) or not isinstance(item.get('file'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', '')))
                or not isinstance(item.get('rules'), list) or not item.get('reviewer')
                or not item.get('reason')):
            raise ValueError('invalid reviewed_content entry')
        if inventory.get(item['file']) == item['sha256']:
            approved[item['file']] = set(item['rules'])
    unresolved_review = [entry for entry in review if entry['rule'] not in approved.get(entry['file'], set())]
    # Bind approvals to the actual candidate bytes. The manifest is excluded
    # to avoid a circular hash when the approvals are recorded in it.
    candidate_sha256 = hashlib.sha256(json.dumps(
        {name: digest for name, digest in sorted(inventory.items()) if name != manifest_name},
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    attestations = manifest.get('attestations', {})
    def has_attestation(name):
        value = attestations.get(name)
        return (isinstance(value, dict) and value.get('passed') is True
                and isinstance(value.get('reviewer'), str) and bool(value['reviewer'].strip())
                and isinstance(value.get('evidence'), str) and bool(value['evidence'].strip())
                and value.get('candidate_sha256') == candidate_sha256)
    release_platforms = manifest.get('release_platforms', ['macos', 'windows'])
    required_core = BASE_CORE_ATTESTATIONS + tuple(PLATFORM_ATTESTATIONS[p] for p in release_platforms)
    core_unmet = [name for name in required_core if not has_attestation(name)]
    if marker_file is None:
        core_unmet.append('private_marker_scan_not_supplied')
    optional_unmet = [name for name in OPTIONAL_ATTESTATIONS if not has_attestation(name)]
    unmet = core_unmet + (optional_unmet if release_scope == 'full' else [])
    core_status = 'BLOCKED' if findings or unresolved_review or core_unmet else 'READY_FOR_REVIEW'
    optional_status = ('NOT_INCLUDED' if not writers_present else
                       'VERIFIED' if not optional_unmet else 'UNVERIFIED')
    return {'schema': 'literature-release-audit/1', 'technical_status': 'FAIL' if findings else 'PASS',
            'publication_status': 'BLOCKED' if findings or unresolved_review or unmet else 'READY_FOR_REVIEW',
            'release_scope': release_scope, 'core_publication_status': core_status,
            'core_unmet_attestations': core_unmet,
            'release_platforms': release_platforms,
            'platform_verification': {
                platform: ('NOT_INCLUDED' if platform not in release_platforms else
                           'VERIFIED' if has_attestation(attestation) else 'UNVERIFIED')
                for platform, attestation in PLATFORM_ATTESTATIONS.items()},
            'zotero_integration_status': 'INCLUDED_UNASSESSED' if integration_present else 'NOT_INCLUDED',
            'zotero_native_delivery_status': optional_status,
            'zotero_native_delivery_unmet_attestations': optional_unmet if writers_present else [],
            'files_checked': len(inventory), 'findings': findings, 'manual_content_review': unresolved_review,
            'unmet_attestations': unmet, 'candidate_sha256': candidate_sha256,
            'sha256': inventory, '_content': content}


def stage(root: Path, manifest_name: str, destination: Path, marker_file: Path | None) -> dict:
    root = root.resolve(strict=True)
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(root):
        raise ValueError('staging destination must be new and outside source tree')
    report = scan(root, manifest_name, marker_file)
    if report['technical_status'] != 'PASS':
        raise ValueError('technical audit failed; no staging files created')
    # Reserve the destination exclusively. Copy only the exact bytes scanned.
    # A failed local staging remains visibly incomplete rather than deleting a
    # directory that another process may have modified after creation.
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        for name, raw in report['_content'].items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(raw)
        check = scan(destination, manifest_name, marker_file, exact_tree=True)
        if check['technical_status'] != 'PASS' or check['sha256'] != report['sha256']:
            raise ValueError('staged tree did not match the scanned source bytes')
    except BaseException:
        # Never remove a directory that could now include another user's data.
        raise
    return {k: v for k, v in check.items() if k != '_content'} | {'staging_path': str(destination)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--manifest', default='release-manifest.json')
    parser.add_argument('--private-markers', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('scan')
    check.add_argument('--exact-tree', action='store_true')
    output = commands.add_parser('stage')
    output.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            result = scan(args.root, args.manifest, args.private_markers, exact_tree=args.exact_tree)
            result.pop('_content')
        else:
            result = stage(args.root, args.manifest, args.destination, args.private_markers)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['publication_status'] == 'READY_FOR_REVIEW' else 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
