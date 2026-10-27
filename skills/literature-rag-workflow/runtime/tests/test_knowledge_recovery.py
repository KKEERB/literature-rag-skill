"""Real temporary SQLite/WAL snapshots; never open the production knowledge DB."""
import hashlib
import json
import random
import sqlite3
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import knowledge_recovery as recovery
import knowledge_quality
import test_quality_transactions as quality_fixtures
from apply_knowledge_reviews import import_reviews


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class KnowledgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'knowledge.sqlite3'
        self.writer = sqlite3.connect(self.db)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.execute('CREATE TABLE docs(id INTEGER PRIMARY KEY, title TEXT NOT NULL)')
        self.writer.execute('CREATE TABLE payloads(id INTEGER PRIMARY KEY, value BLOB NOT NULL)')
        self.writer.execute('CREATE VIRTUAL TABLE docs_fts USING fts5(title)')
        self.add_doc(1, 'aurora cusp reconnection')
        self.add_payloads(1, 65536)
        self.writer.commit()
        self.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.store = self.root / 'objects_store'
        self.counter = 0

    def add_doc(self, identifier, text):
        self.writer.execute('INSERT INTO docs VALUES (?, ?)', (identifier, text))
        self.writer.execute('INSERT INTO docs_fts(rowid,title) VALUES (?, ?)', (identifier, text))

    def add_payloads(self, count, size=131072):
        start = self.writer.execute('SELECT coalesce(max(id),0) FROM payloads').fetchone()[0]
        for number in range(start + 1, start + count + 1):
            # Incompressible, deterministic fixture: compression cannot fake deduplication.
            value = random.Random(42000 + number).randbytes(size)
            self.writer.execute('INSERT INTO payloads VALUES (?, ?)', (number, value))

    def signature(self, path, *, standalone=False):
        # An online source must read its committed WAL. A standalone captured
        # image has no sidecars; immutable reads that complete image directly.
        options = '?mode=ro&immutable=1' if standalone else '?mode=ro'
        with sqlite3.connect(Path(path).resolve().as_uri() + options, uri=True) as con:
            return {
                'integrity': con.execute('PRAGMA integrity_check').fetchone()[0],
                'schema': con.execute('SELECT name,type,sql FROM sqlite_master ORDER BY name').fetchall(),
                'docs': con.execute('SELECT * FROM docs ORDER BY id').fetchall(),
                'payloads': [(n, len(value), hashlib.sha256(value).hexdigest())
                             for n, value in con.execute('SELECT * FROM payloads ORDER BY id')],
                'fts': con.execute("SELECT rowid,title FROM docs_fts WHERE docs_fts MATCH 'reconnection' ORDER BY rowid").fetchall(),
            }

    def snapshot(self, *, name=None, store=True):
        self.counter += 1
        manifest_path = self.root / 'manifests' / (name or ('snapshot-%d.recovery.json' % self.counter))
        kwargs = {'store_path': self.store} if store else {}
        result = recovery.create_snapshot(self.db, manifest_path, **kwargs)
        self.assertIsInstance(result, dict)
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['schema'], 'sqlite-recovery-snapshot-v1')
        self.assertEqual(manifest['block_size'], 1024 * 1024)
        self.assertEqual(manifest['source_path'], str(self.db.resolve()))
        self.assertEqual(sum(x['raw_bytes'] for x in manifest['objects']), manifest['database_bytes'])
        return manifest_path, manifest

    def object_path(self, manifest_path, manifest, index=0):
        obj = manifest['objects'][index]
        store = (manifest_path.parent / manifest['store']).resolve()
        return store / 'objects' / obj['sha256'][:2] / (obj['sha256'] + '.zlib')

    def store_bytes(self):
        return sum(p.stat().st_size for p in self.store.rglob('*.zlib'))

    def malformed(self, original_path, change):
        manifest = json.loads(original_path.read_text())
        change(manifest)
        path = original_path.parent / ('bad-' + str(self.counter) + '.recovery.json')
        self.counter += 1
        path.write_text(json.dumps(manifest))
        return path

    def assert_rejected_without_destination(self, manifest_path):
        destination = self.root / 'restore-attempt' / 'restored.sqlite3'
        destination.parent.mkdir(exist_ok=True)
        before = {p.name for p in destination.parent.iterdir()}
        with self.assertRaises((ValueError, OSError)):
            recovery.verify_snapshot(manifest_path)
        with self.assertRaises((ValueError, OSError)):
            recovery.restore_snapshot(manifest_path, destination)
        self.assertFalse(destination.exists())
        self.assertEqual({p.name for p in destination.parent.iterdir()}, before)

    def test_uncheckpointed_wal_commits_fts_and_full_bytes_are_recovered(self):
        self.add_doc(2, 'new committed reconnection observation')
        self.add_payloads(2)
        self.writer.commit()
        self.assertGreater(Path(str(self.db) + '-wal').stat().st_size, 0)
        # The base file deliberately does not yet contain the committed WAL row.
        with sqlite3.connect(self.db.as_uri() + '?immutable=1', uri=True) as old:
            self.assertEqual(old.execute('SELECT id FROM docs ORDER BY id').fetchall(), [(1,)])
        expected = self.signature(self.db)
        oracle = self.root / 'independent-sqlite-backup.sqlite3'
        with sqlite3.connect(oracle) as destination:
            self.writer.backup(destination)
        manifest_path, manifest = self.snapshot()
        self.assertEqual(manifest['database_sha256'], sha(oracle))
        self.assertEqual(manifest['database_bytes'], oracle.stat().st_size)
        self.assertIsInstance(recovery.verify_snapshot(manifest_path), dict)
        restored = self.root / 'restored.sqlite3'
        self.assertIsInstance(recovery.restore_snapshot(manifest_path, restored), dict)
        self.assertEqual(sha(restored), sha(oracle))
        self.assertEqual(self.signature(restored, standalone=True), expected)
        self.assertEqual(expected['fts'], [(1, 'aurora cusp reconnection'),
                                           (2, 'new committed reconnection observation')])

    def test_two_states_share_objects_and_use_far_less_than_two_full_backups(self):
        self.add_payloads(64)
        self.writer.commit()
        first_path, first = self.snapshot()
        first_bytes = self.store_bytes()
        self.assertGreater(len(first['objects']), 7)
        self.add_doc(2, 'changed state reconnection')
        self.writer.commit()
        second_path, second = self.snapshot()
        second_bytes = self.store_bytes() - first_bytes
        first_ids = {x['sha256'] for x in first['objects']}
        second_ids = {x['sha256'] for x in second['objects']}
        self.assertNotEqual(first['database_sha256'], second['database_sha256'])
        self.assertGreater(len(first_ids & second_ids), 4)
        self.assertGreater(second['stats']['reused_objects'], 4)
        self.assertLess(second['stats']['new_objects'], len(second['objects']))
        self.assertLess(second_bytes, first_bytes // 2)
        self.assertLess(self.store_bytes(), (first['database_bytes'] + second['database_bytes']) * 0.70)
        self.assertIsInstance(recovery.verify_snapshot(first_path), dict)
        self.assertIsInstance(recovery.verify_snapshot(second_path), dict)

    def test_identical_state_creates_zero_new_objects(self):
        first_path, first = self.snapshot()
        before = self.store_bytes()
        second_path, second = self.snapshot()
        self.assertEqual(first['database_sha256'], second['database_sha256'])
        self.assertEqual(first['objects'], second['objects'])
        self.assertEqual(second['stats']['new_objects'], 0)
        self.assertEqual(second['stats']['new_stored_bytes'], 0)
        self.assertEqual(second['stats']['reused_objects'], len(second['objects']))
        self.assertEqual(self.store_bytes(), before)
        self.assertTrue(first_path.exists() and second_path.exists())

    def test_latest_manifest_recovers_without_earlier_manifest_or_original_database(self):
        self.add_payloads(20)
        self.writer.commit()
        first_path, _ = self.snapshot()
        self.add_doc(2, 'standalone reconnection recovery')
        self.writer.commit()
        latest_path, latest = self.snapshot()
        expected = self.signature(self.db)
        first_path.unlink()
        self.writer.close()
        self.db.unlink()
        restored = self.root / 'recovered-without-source.sqlite3'
        recovery.restore_snapshot(latest_path, restored)
        self.assertEqual(sha(restored), latest['database_sha256'])
        self.assertEqual(self.signature(restored, standalone=True), expected)

    def test_default_store_is_partitioned_by_resolved_database_path(self):
        manifest_path, manifest = self.snapshot(store=False)
        namespace = hashlib.sha256(str(self.db.resolve()).encode()).hexdigest()[:16]
        expected = self.db.parent / 'recovery_snapshots' / namespace
        self.assertEqual((manifest_path.parent / manifest['store']).resolve(), expected.resolve())
        self.assertTrue(self.object_path(manifest_path, manifest).is_file())

    def test_manifest_parent_alias_and_resolved_store_have_one_relative_path_basis(self):
        actual = self.root / 'actual-manifests'
        actual.mkdir()
        alias = self.root / 'manifest-alias'
        alias.symlink_to(actual, target_is_directory=True)
        path = alias / 'aliased.recovery.json'
        manifest = recovery.create_snapshot(self.db, path, store_path=self.store.resolve())
        self.assertEqual((path.parent / manifest['store']).resolve(), self.store.resolve())
        recovery.verify_snapshot(path)
        target = self.root / 'alias-restored.sqlite3'
        recovery.restore_snapshot(path, target)
        self.assertEqual(sha(target), manifest['database_sha256'])

    def test_missing_object_refuses_verification_and_restore_without_partial_target(self):
        path, manifest = self.snapshot()
        self.object_path(path, manifest).unlink()
        self.assert_rejected_without_destination(path)

    def test_corrupted_compressed_object_refuses_verification_and_restore(self):
        path, manifest = self.snapshot()
        obj = self.object_path(path, manifest)
        data = bytearray(obj.read_bytes())
        data[len(data) // 2] ^= 0x40
        obj.write_bytes(data)
        self.assert_rejected_without_destination(path)

    def test_decompressed_object_cannot_exceed_declared_bounded_block(self):
        path, manifest = self.snapshot()
        bomb = b'X' * (2 * 1024 * 1024)
        payload = zlib.compress(bomb)
        raw_sha = hashlib.sha256(bomb).hexdigest()
        replacement = json.loads(path.read_text())
        replacement['objects'][0].update(sha256=raw_sha, stored_sha256=hashlib.sha256(payload).hexdigest(),
                                         stored_bytes=len(payload))
        obj = self.object_path(path, replacement)
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(payload)
        # Keep the original allowed raw_bytes, forcing bounded decompression rejection.
        bad_path = path.parent / 'bounded-decompression.recovery.json'
        bad_path.write_text(json.dumps(replacement))
        self.assert_rejected_without_destination(bad_path)

    def test_tampered_manifest_full_hash_size_order_and_object_metadata_are_rejected(self):
        self.add_payloads(20)
        self.writer.commit()
        path, manifest = self.snapshot()
        self.assertGreaterEqual(len(manifest['objects']), 3)
        mutations = {
            'database_sha': lambda value: value.update(database_sha256='0' * 64),
            'database_bytes': lambda value: value.update(database_bytes=value['database_bytes'] + 1),
            'ordered_blocks': lambda value: value['objects'].__setitem__(slice(1, 3), list(reversed(value['objects'][1:3]))),
            'raw_object_sha': lambda value: value['objects'][0].update(sha256='0' * 64),
            'stored_object_sha': lambda value: value['objects'][0].update(stored_sha256='0' * 64),
            'stored_object_bytes': lambda value: value['objects'][0].update(stored_bytes=value['objects'][0]['stored_bytes'] + 1),
            'empty_objects': lambda value: value.update(objects=[]),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.assert_rejected_without_destination(self.malformed(path, mutation))

    def test_existing_restore_target_is_not_overwritten(self):
        path, _ = self.snapshot()
        target = self.root / 'existing.sqlite3'
        target.write_bytes(b'preserve existing destination')
        with self.assertRaises((ValueError, FileExistsError)):
            recovery.restore_snapshot(path, target)
        self.assertEqual(target.read_bytes(), b'preserve existing destination')

    def test_restore_commit_failure_cleans_temporary_image_and_never_creates_target(self):
        path, _ = self.snapshot()
        directory = self.root / 'atomic-restore'
        directory.mkdir()
        target = directory / 'restored.sqlite3'
        observed = []

        def fail(temporary, destination):
            self.assertEqual(Path(destination), target.resolve())
            self.assertTrue(Path(temporary).is_file())
            observed.append(Path(temporary))
            raise OSError('fixture restore commit interruption')

        with patch.object(recovery, '_commit_new', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'restore commit interruption'):
                recovery.restore_snapshot(path, target)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertFalse(target.exists())
        self.assertEqual(list(directory.iterdir()), [])

    def test_restore_destination_appearing_at_commit_is_preserved(self):
        path, _ = self.snapshot()
        directory = self.root / 'raced-restore'
        directory.mkdir()
        target = directory / 'restored.sqlite3'
        commit = recovery._commit_new

        def concurrent_destination(temporary, destination):
            Path(destination).write_bytes(b'concurrent owner data')
            return commit(temporary, destination)

        with patch.object(recovery, '_commit_new', side_effect=concurrent_destination):
            with self.assertRaises(ValueError):
                recovery.restore_snapshot(path, target)
        self.assertEqual(target.read_bytes(), b'concurrent owner data')
        self.assertEqual(list(directory.iterdir()), [target])

    def test_existing_manifest_is_not_overwritten(self):
        path, _ = self.snapshot()
        before = path.read_bytes()
        with self.assertRaises((ValueError, FileExistsError)):
            recovery.create_snapshot(self.db, path, store_path=self.store)
        self.assertEqual(path.read_bytes(), before)

    def test_capture_interruption_removes_only_its_temporary_database(self):
        path = self.root / 'failed-capture.recovery.json'
        captured = []
        original = recovery._capture_database

        def fail(source, temporary):
            captured.append(Path(temporary))
            original(source, temporary)
            raise RuntimeError('fixture interruption after consistent capture')

        before = self.signature(self.db)
        with patch.object(recovery, '_capture_database', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'after consistent capture'):
                recovery.create_snapshot(self.db, path, store_path=self.store)
        self.assertFalse(path.exists())
        self.assertEqual(len(captured), 1)
        for suffix in ['', '-wal', '-shm']:
            self.assertFalse(Path(str(captured[0]) + suffix).exists())
        self.assertEqual(self.signature(self.db), before)

    def test_object_or_manifest_commit_interruption_cleans_capture_and_keeps_previous_snapshot(self):
        first_path, first = self.snapshot()
        expected = self.signature(self.db)
        self.add_doc(2, 'interrupted next reconnection state')
        self.writer.commit()
        original_capture = recovery._capture_database
        for point in ['_write_object', '_commit_manifest']:
            with self.subTest(point=point):
                captured = []

                def record_capture(source, temporary):
                    captured.append(Path(temporary))
                    return original_capture(source, temporary)

                failed = self.root / (point + '.recovery.json')
                with patch.object(recovery, '_capture_database', side_effect=record_capture), \
                        patch.object(recovery, point, side_effect=RuntimeError('fixture mid-snapshot failure')):
                    with self.assertRaisesRegex(RuntimeError, 'mid-snapshot'):
                        recovery.create_snapshot(self.db, failed, store_path=self.store)
                self.assertFalse(failed.exists())
                self.assertEqual(len(captured), 1)
                self.assertFalse(captured[0].exists())
                target = self.root / ('restore-after-' + point + '.sqlite3')
                recovery.restore_snapshot(first_path, target)
                self.assertEqual(sha(target), first['database_sha256'])
                self.assertEqual(self.signature(target, standalone=True), expected)

    def test_non_sqlite_source_is_rejected_without_manifest(self):
        source = self.root / 'not-sqlite.bin'
        source.write_bytes(b'This is not a SQLite database.')
        manifest = self.root / 'invalid-source.recovery.json'
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            recovery.create_snapshot(source, manifest, store_path=self.store)
        self.assertFalse(manifest.exists())

    def test_consistent_backup_keeps_the_existing_raw_sqlite_contract(self):
        self.add_doc(2, 'raw sqlite reconnection contract')
        self.writer.commit()
        destination = self.root / 'baseline.sqlite3'
        result = knowledge_quality.consistent_backup(self.db, destination)
        self.assertIsNone(result)
        self.assertTrue(destination.read_bytes().startswith(b'SQLite format 3\x00'))
        self.assertEqual(self.signature(destination, standalone=True), self.signature(self.db))
        with self.assertRaises(ValueError):
            knowledge_quality.consistent_backup(self.db, destination)
        self.assertFalse(list(self.root.rglob('*.recovery.json')))

    def test_new_full_prewrite_rollback_copies_are_refused_without_creating_files(self):
        for name in ['pre_review_20000101.sqlite3', 'pre_publish.sqlite3', 'pre_relation_update.sqlite3']:
            with self.subTest(name=name):
                target = self.root / name
                with self.assertRaises(ValueError):
                    knowledge_quality.consistent_backup(self.db, target)
                self.assertFalse(target.exists())

    def test_recovery_backup_wrapper_returns_manifest_and_does_not_materialize_requested_full_copy(self):
        requested = self.root / 'backups' / 'pre-change.sqlite3'
        manifest_path = knowledge_quality.recovery_backup(self.db, requested)
        self.assertIsInstance(manifest_path, Path)
        self.assertTrue(manifest_path.name.endswith('.recovery.json'))
        self.assertFalse(requested.exists())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['schema'], 'sqlite-recovery-snapshot-v1')
        restored = self.root / 'wrapper-restored.sqlite3'
        recovery.restore_snapshot(manifest_path, restored)
        self.assertEqual(sha(restored), manifest['database_sha256'])
        self.assertEqual(self.signature(restored, standalone=True), self.signature(self.db))

    def test_restored_image_is_usable_with_a_normal_writable_sqlite_connection(self):
        path, manifest = self.snapshot()
        restored = self.root / 'normal-writable-restored.sqlite3'
        recovery.restore_snapshot(path, restored)
        self.assertEqual(sha(restored), manifest['database_sha256'])
        with sqlite3.connect(restored) as con:
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(con.execute('SELECT * FROM docs').fetchall(), [(1, 'aurora cusp reconnection')])
            self.assertEqual(con.execute("SELECT rowid FROM docs_fts WHERE docs_fts MATCH 'reconnection'").fetchall(), [(1,)])
            con.execute("INSERT INTO docs VALUES (2, 'restored database accepts new writes')")
        with sqlite3.connect(restored) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM docs').fetchone()[0], 2)

    def test_confirmed_semantic_import_uses_real_recoverable_prewrite_manifest(self):
        # Reuse the existing isolated business fixture, not an imitation importer.
        fixture = quality_fixtures.QualityTransactionTests(methodName='test_fabricated_or_old_passage_cannot_be_imported')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch('apply_knowledge_reviews.subprocess.run') as extract:
            extract.return_value.stdout = fixture.fresh
            result = import_reviews(fixture.db, fixture.decisions, fixture.root / 'real-import',
                                    confirmed=True, lock_path=fixture.lock)
        self.assertEqual(result['status'], 'pass')
        backup = Path(result['backup'])
        self.assertTrue(backup.name.endswith('.recovery.json'))
        self.assertTrue(result['backup_format'])
        self.assertFalse(backup.read_bytes().startswith(b'SQLite format 3\x00'))
        recovery.verify_snapshot(backup)
        restored = fixture.root / 'pre-review-restored.sqlite3'
        recovery.restore_snapshot(backup, restored)
        self.assertEqual(sha(restored), json.loads(backup.read_text())['database_sha256'])
        with sqlite3.connect(restored) as con:
            old_quality = con.execute("SELECT name FROM sqlite_master WHERE name='chunk_quality'").fetchone()
            self.assertIsNone(old_quality)
            self.assertEqual(con.execute('SELECT count(*) FROM chunks').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM chunks_fts').fetchone()[0], 1)
        with sqlite3.connect(fixture.db) as con:
            record = json.loads(con.execute('SELECT record_json FROM chunk_quality').fetchone()[0])
            self.assertEqual(record['state'], 'verified')

    def test_corrupt_reused_backup_object_blocks_real_semantic_import_before_any_write(self):
        fixture = quality_fixtures.QualityTransactionTests(methodName='test_fabricated_or_old_passage_cannot_be_imported')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        existing_path = fixture.root / 'existing.recovery.json'
        recovery.create_snapshot(fixture.db, existing_path)
        manifest = json.loads(existing_path.read_text())
        obj = self.object_path(existing_path, manifest)
        damaged = bytearray(obj.read_bytes())
        damaged[len(damaged) // 2] ^= 0x40
        obj.write_bytes(damaged)
        before = sha(fixture.db)
        with sqlite3.connect(fixture.db) as con:
            old_sources = con.execute('SELECT * FROM sources').fetchall()
            old_chunks = con.execute('SELECT * FROM chunks').fetchall()
            old_fts = con.execute('SELECT * FROM chunks_fts').fetchall()
        output = fixture.root / 'must-not-write-review'
        with patch('apply_knowledge_reviews.subprocess.run') as extract:
            extract.return_value.stdout = fixture.fresh
            with self.assertRaises((ValueError, OSError)):
                import_reviews(fixture.db, fixture.decisions, output,
                               confirmed=True, lock_path=fixture.lock)
        self.assertEqual(sha(fixture.db), before)
        with sqlite3.connect(fixture.db) as con:
            self.assertEqual(con.execute('SELECT * FROM sources').fetchall(), old_sources)
            self.assertEqual(con.execute('SELECT * FROM chunks').fetchall(), old_chunks)
            self.assertEqual(con.execute('SELECT * FROM chunks_fts').fetchall(), old_fts)
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='chunk_quality'").fetchone())
        self.assertFalse(list(output.glob('review_receipt_*.json')))


if __name__ == '__main__':
    unittest.main()
