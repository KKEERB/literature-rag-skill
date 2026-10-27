"""Release candidate scanning tests with invented data only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/release_guard.py'
SPEC = importlib.util.spec_from_file_location('release_guard', SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.skill = self.root / guard.SKILL / 'SKILL.md'
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text('name: synthetic\n', encoding='utf-8')
        self.marker = Path(self.temp.name) / 'private-markers.txt'
        self.marker.write_text('PRIVATE-EXAMPLE-MARKER\n', encoding='utf-8')
        self.manifest = {'schema': guard.SCHEMA, 'files': ['release-manifest.json',
                          guard.SKILL + 'SKILL.md'], 'attestations': {}, 'reviewed_content': []}
        self.save()

    def save(self):
        (self.root / 'release-manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_missing_attestations_and_markers_block_publication(self):
        result = guard.scan(self.root, 'release-manifest.json')
        self.assertEqual(result['technical_status'], 'PASS')
        self.assertEqual(result['publication_status'], 'BLOCKED')
        self.assertIn('private_marker_scan_not_supplied', result['unmet_attestations'])
        self.assertIn('windows_native', result['unmet_attestations'])

    def test_attestations_expire_when_candidate_changes(self):
        first = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.manifest['attestations'] = {
            name: {'passed': True, 'reviewer': 'fixture', 'evidence': 'synthetic',
                   'candidate_sha256': first['candidate_sha256']}
            for name in guard.ATTESTATIONS}
        self.save()
        self.assertEqual(guard.scan(self.root, 'release-manifest.json', self.marker)['publication_status'], 'READY_FOR_REVIEW')
        self.skill.write_text('name: altered\n', encoding='utf-8')
        changed = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(changed['publication_status'], 'BLOCKED')
        self.assertEqual(set(changed['unmet_attestations']), set(guard.ATTESTATIONS))

    def test_core_only_excludes_zotero_attestation_without_waiving_core_gates(self):
        self.manifest['release_scope'] = 'core_only'
        self.save()
        initial = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(initial['zotero_native_delivery_status'], 'NOT_INCLUDED')
        self.assertNotIn('real_zotero_delivery', initial['unmet_attestations'])
        self.assertEqual(initial['publication_status'], 'BLOCKED')
        self.assertIn('privacy_and_source_rights', initial['unmet_attestations'])
        self.assertIn('third_party_licenses', initial['unmet_attestations'])
        self.assertIn('windows_native', initial['unmet_attestations'])
        self.manifest['attestations'] = {
            name: {'passed': True, 'reviewer': 'fixture', 'evidence': 'synthetic',
                   'candidate_sha256': initial['candidate_sha256']}
            for name in guard.CORE_ATTESTATIONS}
        self.save()
        ready = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(ready['publication_status'], 'READY_FOR_REVIEW')
        self.assertEqual(ready['zotero_native_delivery_status'], 'NOT_INCLUDED')

    def test_core_only_rejects_included_zotero_writer_even_if_attested(self):
        self.manifest['release_scope'] = 'core_only'
        relative = guard.SKILL + 'runtime/scripts/portable_zotero_writer.js'
        writer = self.root / relative
        writer.parent.mkdir(parents=True)
        writer.write_text('// synthetic writer\n', encoding='utf-8')
        self.manifest['files'].append(relative)
        self.save()
        initial = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.manifest['attestations'] = {
            name: {'passed': True, 'reviewer': 'fixture', 'evidence': 'synthetic',
                   'candidate_sha256': initial['candidate_sha256']}
            for name in guard.ATTESTATIONS}
        self.save()
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(result['technical_status'], 'FAIL')
        self.assertEqual(result['publication_status'], 'BLOCKED')
        self.assertIn('zotero_integration_in_core_only_candidate', [x['rule'] for x in result['findings']])

    def test_core_only_also_rejects_zotero_read_only_entrypoint(self):
        self.manifest['release_scope'] = 'core_only'
        relative = guard.SKILL + 'runtime/scripts/portable_zotero.py'
        entrypoint = self.root / relative
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text('# synthetic read-only connector\n', encoding='utf-8')
        self.manifest['files'].append(relative)
        self.save()
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(result['technical_status'], 'FAIL')
        self.assertEqual(result['zotero_integration_status'], 'INCLUDED_UNASSESSED')
        self.assertIn('zotero_integration_in_core_only_candidate', [x['rule'] for x in result['findings']])

    def test_macos_only_does_not_invent_windows_acceptance(self):
        self.manifest['release_scope'] = 'core_only'
        self.manifest['release_platforms'] = ['macos']
        self.save()
        initial = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertIn('macos_native', initial['core_unmet_attestations'])
        self.assertNotIn('windows_native', initial['core_unmet_attestations'])
        self.assertEqual(initial['platform_verification']['windows'], 'NOT_INCLUDED')
        self.manifest['attestations'] = {
            name: {'passed': True, 'reviewer': 'fixture', 'evidence': 'synthetic',
                   'candidate_sha256': initial['candidate_sha256']}
            for name in guard.BASE_CORE_ATTESTATIONS + ('macos_native',)}
        self.save()
        report = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(report['publication_status'], 'READY_FOR_REVIEW')
        self.assertEqual(report['platform_verification'], {'macos': 'VERIFIED', 'windows': 'NOT_INCLUDED'})
        self.assertNotIn('windows_native', self.manifest['attestations'])
        self.manifest['release_platforms'].append('windows')
        self.save()
        widened = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(widened['publication_status'], 'BLOCKED')
        self.assertEqual(widened['platform_verification']['windows'], 'UNVERIFIED')
        self.assertIn('windows_native', widened['unmet_attestations'])

    def test_invalid_platform_scope_is_rejected(self):
        for bad in ([], ['linux'], ['macos', 'macos'], 'macos'):
            with self.subTest(platforms=bad):
                self.manifest['release_platforms'] = bad
                self.save()
                with self.assertRaisesRegex(ValueError, 'release_platforms'):
                    guard.scan(self.root, 'release-manifest.json', self.marker)

    def test_full_candidate_reports_unverified_optional_writer_separately(self):
        relative = guard.SKILL + 'runtime/scripts/portable_zotero_writer.py'
        writer = self.root / relative
        writer.parent.mkdir(parents=True)
        writer.write_text('# synthetic writer\n', encoding='utf-8')
        self.manifest['files'].append(relative)
        self.save()
        initial = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.manifest['attestations'] = {
            name: {'passed': True, 'reviewer': 'fixture', 'evidence': 'synthetic',
                   'candidate_sha256': initial['candidate_sha256']}
            for name in guard.CORE_ATTESTATIONS}
        self.save()
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(result['core_publication_status'], 'READY_FOR_REVIEW')
        self.assertEqual(result['zotero_native_delivery_status'], 'UNVERIFIED')
        self.assertEqual(result['publication_status'], 'BLOCKED')
        self.assertEqual(result['unmet_attestations'], ['real_zotero_delivery'])

    def test_empty_marker_file_and_escaping_manifest_are_rejected(self):
        self.marker.write_text('short\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'at least one marker'):
            guard.scan(self.root, 'release-manifest.json', self.marker)
        with self.assertRaisesRegex(ValueError, 'manifest path'):
            guard.scan(self.root, '../release-manifest.json')

    def test_private_markers_are_not_printed_and_staging_refuses(self):
        self.skill.write_text('do not share PRIVATE-EXAMPLE-MARKER\n', encoding='utf-8')
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(result['technical_status'], 'FAIL')
        self.assertEqual(result['findings'][0]['rule'], 'private_marker')
        self.assertNotIn('PRIVATE-EXAMPLE-MARKER', json.dumps(result['findings']))
        destination = Path(self.temp.name) / 'staged'
        with self.assertRaisesRegex(ValueError, 'technical audit failed'):
            guard.stage(self.root, 'release-manifest.json', destination, self.marker)
        self.assertFalse(destination.exists())

    def test_bad_paths_symlink_and_forbidden_data_fail(self):
        self.manifest['files'].append('../escape')
        self.save()
        with self.assertRaises(ValueError):
            guard.scan(self.root, 'release-manifest.json', self.marker)
        self.manifest['files'].pop()
        self.manifest['files'].append('private.sqlite3')
        (self.root / 'private.sqlite3').write_bytes(b'fixture')
        self.save()
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertIn('forbidden_file_type_or_directory', [r['rule'] for r in result['findings']])
        self.manifest['files'][-1] = 'linked.txt'
        try:
            (self.root / 'linked.txt').symlink_to(self.marker)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.save()
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertIn('unreadable_or_invalid', [r['rule'] for r in result['findings']])

    def test_windows_home_path_and_split_literal_are_detected(self):
        scripts = self.root / guard.SKILL / 'runtime/scripts'
        scripts.mkdir(parents=True)
        source = scripts / 'sample.py'
        source.write_text('value = "PRIVATE-EXAMPLE-" + "MARKER"\n', encoding='utf-8')
        self.manifest['files'].append(source.relative_to(self.root).as_posix())
        sample = self.root / 'notes.txt'
        sample.write_text('C:' + '\\' + 'Users' + '\\' + 'someone' + '\\file', encoding='utf-8')
        self.manifest['files'].append('notes.txt')
        self.save()
        report = guard.scan(self.root, 'release-manifest.json', self.marker)
        rules = {r['rule'] for r in report['findings']}
        self.assertIn('private_marker_literal', rules)
        self.assertIn('windows_home_path', rules)

    def test_json_escaped_private_marker_is_detected(self):
        marker = '示例秘密标识'
        self.marker.write_text(marker, encoding='utf-8')
        value = self.root / 'value.json'
        value.write_text(json.dumps({'hidden': marker}, ensure_ascii=True), encoding='utf-8')
        self.manifest['files'].append('value.json')
        self.save()
        report = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertIn('private_marker_json_value', [r['rule'] for r in report['findings']])

    def test_review_of_source_reference_is_hash_bound(self):
        self.skill.write_text('An example DOI: 10.1234/' + 'sample\n', encoding='utf-8')
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertEqual(result['manual_content_review'][0]['rule'], 'source_doi')
        self.manifest['reviewed_content'] = [{'file': guard.SKILL + 'SKILL.md',
            'sha256': hashlib.sha256(self.skill.read_bytes()).hexdigest(),
            'rules': ['source_doi'], 'reviewer': 'test', 'reason': 'synthetic'}]
        self.save()
        self.assertEqual(guard.scan(self.root, 'release-manifest.json', self.marker)['manual_content_review'], [])
        self.skill.write_text('Another example DOI: 10.1234/' + 'sample2\n', encoding='utf-8')
        self.assertEqual(len(guard.scan(self.root, 'release-manifest.json', self.marker)['manual_content_review']), 1)

    def test_missing_local_python_dependency_is_detected(self):
        scripts = self.root / guard.SKILL / 'runtime/scripts'
        scripts.mkdir(parents=True)
        (scripts / 'main.py').write_text('import helper\n', encoding='utf-8')
        (scripts / 'helper.py').write_text('value = 1\n', encoding='utf-8')
        self.manifest['files'].append((scripts / 'main.py').relative_to(self.root).as_posix())
        self.save()
        report = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertIn('unlisted_local_dependency', [r['rule'] for r in report['findings']])

    def test_excluded_referenced_resource_is_detected(self):
        reference = self.root / guard.SKILL / 'references/internal.md'
        reference.parent.mkdir(parents=True)
        reference.write_text('not selected', encoding='utf-8')
        self.skill.write_text('[Resource](references/internal.md)', encoding='utf-8')
        result = guard.scan(self.root, 'release-manifest.json', self.marker)
        self.assertIn('unlisted_markdown_reference', [r['rule'] for r in result['findings']])

    def test_stage_copies_only_scanned_files_and_detects_extras(self):
        destination = Path(self.temp.name) / 'staged'
        report = guard.stage(self.root, 'release-manifest.json', destination, self.marker)
        self.assertEqual(report['publication_status'], 'BLOCKED')
        self.assertEqual(report['technical_status'], 'PASS')
        (destination / 'extra.txt').write_text('not approved', encoding='utf-8')
        second = guard.scan(destination, 'release-manifest.json', self.marker, exact_tree=True)
        self.assertIn('unlisted_file_in_release_tree', [r['rule'] for r in second['findings']])
        with self.assertRaises(ValueError):
            guard.stage(self.root, 'release-manifest.json', destination, self.marker)


if __name__ == '__main__':
    unittest.main()
