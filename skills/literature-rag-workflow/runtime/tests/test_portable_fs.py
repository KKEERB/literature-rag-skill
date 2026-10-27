"""Filesystem contract tests; native Windows execution remains separately required."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from portable_fs import ensure_material_alias, file_lock
import knowledge_recovery as recovery


class PortableFsTest(unittest.TestCase):
    def test_independent_process_cannot_acquire_held_lock(self):
        with tempfile.TemporaryDirectory(prefix='lock-中文 space-') as directory:
            lock = Path(directory) / 'workflow.lock'
            source = (
                'import sys; from pathlib import Path; '
                'sys.path.insert(0, sys.argv[1]); from portable_fs import file_lock; '
                '\ntry:\n with file_lock(Path(sys.argv[2])): print("unexpected")'
                '\nexcept BlockingIOError: print("locked")\n'
            )
            with file_lock(lock):
                result = subprocess.run([sys.executable, '-c', source, str(SCRIPTS), str(lock)],
                                        capture_output=True, text=True, timeout=15, check=True)
                self.assertEqual(result.stdout.strip(), 'locked')
            # Closing the original handle releases the lock.
            with file_lock(lock):
                pass

    def test_process_exit_releases_lock(self):
        with tempfile.TemporaryDirectory(prefix='lock-release-') as directory:
            lock = Path(directory) / 'workflow.lock'
            source = (
                'import sys,time; from pathlib import Path; '
                'sys.path.insert(0, sys.argv[1]); from portable_fs import file_lock; '
                '\nwith file_lock(Path(sys.argv[2])):\n print("ready", flush=True)\n time.sleep(60)\n'
            )
            child = subprocess.Popen([sys.executable, '-c', source, str(SCRIPTS), str(lock)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(BlockingIOError):
                    with file_lock(lock):
                        pass
            finally:
                child.terminate()
                child.communicate(timeout=10)
            with file_lock(lock):
                pass

    def test_symlink_denied_creates_verified_copy_and_detects_drift(self):
        with tempfile.TemporaryDirectory(prefix='alias-中文 space-') as directory:
            root = Path(directory)
            source, alias = root / '原始.pdf', root / 'alias.pdf'
            source.write_bytes(b'first source content')
            with patch.object(Path, 'symlink_to', side_effect=PermissionError(errno.EPERM, 'denied')):
                ensure_material_alias(alias, source)
                self.assertFalse(alias.is_symlink())
                self.assertEqual(alias.read_bytes(), source.read_bytes())
                ensure_material_alias(alias, source)
                source.write_bytes(b'changed source content')
                with self.assertRaisesRegex(ValueError, 'alias changed'):
                    ensure_material_alias(alias, source)
                self.assertEqual(alias.read_bytes(), b'first source content')

    def test_existing_different_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, alias = root / 'source.pdf', root / 'alias.pdf'
            source.write_bytes(b'new')
            alias.write_bytes(b'existing')
            with self.assertRaisesRegex(ValueError, 'alias changed'):
                ensure_material_alias(alias, source)
            self.assertEqual(alias.read_bytes(), b'existing')

    def test_unexpected_symlink_error_is_not_silently_bypassed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, alias = root / 'source.pdf', root / 'alias.pdf'
            source.write_bytes(b'content')
            with patch.object(Path, 'symlink_to', side_effect=OSError(errno.EIO, 'disk error')):
                with self.assertRaises(OSError):
                    ensure_material_alias(alias, source)
            self.assertFalse(alias.exists())

    def test_failed_directory_flush_remains_blocked_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, alias = root / 'source.pdf', root / 'alias.pdf'
            source.write_bytes(b'fixture')
            with patch.object(Path, 'symlink_to', side_effect=PermissionError(errno.EPERM, 'denied')):
                with patch('portable_fs.fsync_directory', side_effect=OSError('flush failed')):
                    for _ in range(2):
                        with self.assertRaisesRegex(OSError, 'flush failed'):
                            ensure_material_alias(alias, source)
            self.assertTrue(alias.exists())
            ensure_material_alias(alias, source)

    def test_recovery_object_retry_rechecks_directory_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            (store / 'objects' / recovery._sha(b'fixture')[:2]).mkdir(parents=True)
            with patch.object(recovery, '_fsync_directory', side_effect=OSError('flush failed')):
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, 'flush failed'):
                        recovery._write_object(store, b'fixture')
            record, created = recovery._write_object(store, b'fixture')
            self.assertFalse(created)
            self.assertEqual(recovery._read_object(store, record), b'fixture')


if __name__ == '__main__':
    unittest.main()
