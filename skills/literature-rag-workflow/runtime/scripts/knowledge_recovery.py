"""Lossless SQLite recovery points using shared, independent compressed blocks.

The manifest is a complete ordered image, never a delta from another snapshot.
Only temporary consistent SQLite images are materialized. Existing recovery
points, objects and restore destinations are never overwritten.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import zlib

from portable_fs import file_lock, fsync_directory

SCHEMA = 'sqlite-recovery-snapshot-v1'
BLOCK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_COMPRESSED_BYTES = BLOCK_SIZE + 65536
CAPTURE_TIMEOUT_SECONDS = 120
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_directory(path: Path) -> None:
    fsync_directory(path)


def _require_new(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError('destination must be new: ' + str(path))


def _canonical_parent(path: Path) -> Path:
    # Resolve directory aliases (/var vs /private/var on macOS), while not
    # following a final-component symlink that must instead be rejected.
    path = Path(path).absolute()
    return path.parent.resolve() / path.name


def _mkdir_durable(path: Path) -> None:
    missing = []
    parent = path
    while not parent.exists():
        if parent.is_symlink():
            raise ValueError('directory cannot be a dangling symlink')
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise ValueError('directory ancestor is not a directory')
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError('directory changed during creation')
        _fsync_directory(directory.parent)


@contextlib.contextmanager
def _store_lock(store: Path, *, shared: bool = False):
    _mkdir_durable(store)
    lock = store / '.store.lock'
    with file_lock(lock, shared=shared, blocking=True):
        yield


def _integrity_check(database: Path) -> None:
    # Every image is standalone. immutable avoids creating WAL/SHM sidecars
    # while verifying a captured or reconstructed image.
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
        result = con.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise ValueError('SQLite integrity_check failed')


def _capture_database(source: Path, temporary: Path) -> None:
    """Capture committed SQLite state, including committed uncheckpointed WAL."""
    deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite online backup timed out')

    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as src:
        with contextlib.closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst, pages=1024, progress=progress, sleep=0.05)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('captured SQLite integrity_check failed')
            dst.commit()
    with temporary.open('rb') as handle:
        os.fsync(handle.fileno())


def _object_path(store: Path, raw_sha: str) -> Path:
    if not isinstance(raw_sha, str) or not _SHA.fullmatch(raw_sha):
        raise ValueError('invalid object SHA256')
    objects = store / 'objects'
    prefix = objects / raw_sha[:2]
    if objects.is_symlink() or prefix.is_symlink():
        raise ValueError('object directory cannot be a symlink')
    path = prefix / (raw_sha + '.zlib')
    if path.is_symlink():
        raise ValueError('object cannot be a symlink')
    return path


def _read_object(store: Path, entry: dict) -> bytes:
    path = _object_path(store, entry['sha256'])
    if not path.is_file() or path.stat().st_size != entry['stored_bytes']:
        raise ValueError('missing or truncated recovery object: ' + entry['sha256'])
    with path.open('rb') as handle:
        payload = handle.read(MAX_COMPRESSED_BYTES + 1)
    if len(payload) > MAX_COMPRESSED_BYTES or _sha(payload) != entry['stored_sha256']:
        raise ValueError('compressed recovery object hash differs')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(payload, entry['raw_bytes'] + 1)
    except zlib.error as exc:
        raise ValueError('invalid compressed recovery object') from exc
    if (len(raw) != entry['raw_bytes'] or not decoder.eof or decoder.unconsumed_tail
            or decoder.unused_data or _sha(raw) != entry['sha256']):
        raise ValueError('raw recovery object differs or exceeds declared size')
    return raw


def _commit_new(temporary: Path, destination: Path) -> None:
    """Atomic, no-replace commit on the same filesystem."""
    _require_new(destination)
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise ValueError('destination appeared during commit') from exc
    _fsync_directory(destination.parent)


def _write_object(store: Path, raw: bytes) -> tuple[dict, bool]:
    raw_sha = _sha(raw)
    path = _object_path(store, raw_sha)
    if path.exists():
        size = path.stat().st_size
        if size > MAX_COMPRESSED_BYTES:
            raise ValueError('oversized existing recovery object')
        with path.open('rb') as handle:
            payload = handle.read(MAX_COMPRESSED_BYTES + 1)
        entry = {'sha256': raw_sha, 'raw_bytes': len(raw),
                 'stored_sha256': _sha(payload), 'stored_bytes': len(payload)}
        if _read_object(store, entry) != raw:
            raise ValueError('existing content-addressed object differs')
        _fsync_directory(path.parent)
        return entry, False
    payload = zlib.compress(raw)
    entry = {'sha256': raw_sha, 'raw_bytes': len(raw),
             'stored_sha256': _sha(payload), 'stored_bytes': len(payload)}
    _mkdir_durable(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix='.' + raw_sha + '-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _commit_new(temporary, path)
        if _read_object(store, entry) != raw:
            raise ValueError('new recovery object readback differs')
    finally:
        temporary.unlink(missing_ok=True)
    return entry, True


def _commit_manifest(path: Path, manifest: dict) -> None:
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError('recovery manifest exceeds supported size')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _commit_new(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_manifest(path: Path) -> tuple[dict, Path, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('recovery manifest must be a regular file')
    with path.open('rb') as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('oversized recovery manifest')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError('invalid recovery manifest JSON') from exc
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('unsupported recovery manifest schema')
    if (type(value.get('block_size')) is not int or value['block_size'] != BLOCK_SIZE
            or type(value.get('database_bytes')) is not int or value['database_bytes'] < 100
            or not isinstance(value.get('database_sha256'), str)
            or not _SHA.fullmatch(value['database_sha256'])
            or not isinstance(value.get('source_path'), str) or '\x00' in value['source_path']
            or not Path(value['source_path']).is_absolute()
            or not isinstance(value.get('created_at'), str) or not value['created_at']):
        raise ValueError('invalid recovery manifest database metadata')
    relative = value.get('store')
    if not isinstance(relative, str) or not relative or '\x00' in relative or Path(relative).is_absolute():
        raise ValueError('recovery object store must be relative to manifest')
    objects = value.get('objects')
    expected = (value['database_bytes'] + BLOCK_SIZE - 1) // BLOCK_SIZE
    if not isinstance(objects, list) or len(objects) != expected:
        raise ValueError('incomplete ordered recovery object inventory')
    for i, entry in enumerate(objects):
        if not isinstance(entry, dict) or set(entry) != {'sha256', 'raw_bytes', 'stored_sha256', 'stored_bytes'}:
            raise ValueError('invalid recovery object metadata')
        size = min(BLOCK_SIZE, value['database_bytes'] - i * BLOCK_SIZE)
        if (type(entry['raw_bytes']) is not int or entry['raw_bytes'] != size
                or type(entry['stored_bytes']) is not int or not 0 < entry['stored_bytes'] <= MAX_COMPRESSED_BYTES
                or any(not isinstance(entry[k], str) or not _SHA.fullmatch(entry[k])
                       for k in ('sha256', 'stored_sha256'))):
            raise ValueError('invalid recovery object length or hash')
    stats = value.get('stats')
    if (not isinstance(stats, dict) or set(stats) != {'new_objects', 'reused_objects', 'new_stored_bytes'}
            or any(type(n) is not int or n < 0 for n in stats.values())
            or stats['new_objects'] + stats['reused_objects'] != len(objects)):
        raise ValueError('invalid recovery storage statistics')
    return value, (path.parent / relative).resolve(), _sha(raw)


def _materialize(manifest: dict, store: Path, destination: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    with destination.open('xb') as handle:
        for entry in manifest['objects']:
            raw = _read_object(store, entry)
            handle.write(raw)
            digest.update(raw)
            size += len(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if size != manifest['database_bytes'] or digest.hexdigest() != manifest['database_sha256']:
        raise ValueError('reconstructed database hash or size differs')
    _integrity_check(destination)


def _manifest_unchanged(path: Path, expected: str) -> None:
    _, _, actual = _read_manifest(path)
    if actual != expected:
        raise ValueError('recovery manifest changed during verification')


def create_snapshot(database: Path, manifest_path: Path, *, store_path: Path | None = None) -> dict:
    database, manifest_path = Path(database).resolve(), _canonical_parent(manifest_path)
    if not database.is_file() or not manifest_path.name.endswith('.recovery.json'):
        raise ValueError('existing SQLite source and *.recovery.json destination required')
    _require_new(manifest_path)
    store = (Path(store_path).resolve() if store_path is not None else database.parent / 'recovery_snapshots' /
             hashlib.sha256(str(database).encode()).hexdigest()[:16])
    _mkdir_durable(manifest_path.parent)
    with _store_lock(store):
        _require_new(manifest_path)
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=store) as directory:
            image = Path(directory) / 'consistent.sqlite3'
            _capture_database(database, image)
            entries, digest, size = [], hashlib.sha256(), 0
            stats = {'new_objects': 0, 'reused_objects': 0, 'new_stored_bytes': 0}
            with image.open('rb') as handle:
                while raw := handle.read(BLOCK_SIZE):
                    entry, created = _write_object(store, raw)
                    entries.append(entry)
                    digest.update(raw)
                    size += len(raw)
                    stats['new_objects' if created else 'reused_objects'] += 1
                    if created:
                        stats['new_stored_bytes'] += entry['stored_bytes']
            manifest = {'schema': SCHEMA, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                        'source_path': str(database), 'database_sha256': digest.hexdigest(),
                        'database_bytes': size, 'block_size': BLOCK_SIZE,
                        'store': os.path.relpath(store, manifest_path.parent), 'objects': entries, 'stats': stats}
            _commit_manifest(manifest_path, manifest)
    return manifest


def _stat_identity(value: os.stat_result) -> dict:
    if not stat.S_ISREG(value.st_mode) or value.st_size < 100:
        raise ValueError('static archive source must be a regular SQLite file')
    return {'device': value.st_dev, 'inode': value.st_ino,
            'mtime_ns': value.st_mtime_ns, 'ctime_ns': value.st_ctime_ns,
            'size': value.st_size}


def _source_stat(source: Path) -> dict:
    # Even empty sidecars mean that this is not an established standalone
    # historical file. Never checkpoint or change a source to make it eligible.
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError('static archive source has a SQLite sidecar: ' + str(sidecar))
    return _stat_identity(source.lstat())


def _hash_file(handle) -> tuple[str, int]:
    handle.seek(0)
    digest, size = hashlib.sha256(), 0
    while raw := handle.read(BLOCK_SIZE):
        digest.update(raw)
        size += len(raw)
    return digest.hexdigest(), size


def _assert_static_source(source: Path, expected: dict, descriptor: int) -> None:
    if _source_stat(source) != expected or _stat_identity(os.fstat(descriptor)) != expected:
        raise ValueError('static archive source changed during archival')


def archive_snapshot(database: Path, manifest_path: Path, *, store_path: Path | None = None) -> dict:
    """Archive an inactive standalone SQLite file without changing its bytes.

    Unlike create_snapshot, this never runs online backup or materializes a
    different SQLite image. The caller must keep all writers inactive, and must
    revalidate the source before any separately authorized deletion. This
    function never deletes, renames or writes the source.
    """
    database, manifest_path = _canonical_parent(database), _canonical_parent(manifest_path)
    if database == manifest_path or not manifest_path.name.endswith('.recovery.json'):
        raise ValueError('distinct *.recovery.json destination required')
    initial = _source_stat(database)
    _require_new(manifest_path)
    store = (Path(store_path).resolve() if store_path is not None else database.parent / 'recovery_snapshots' /
             hashlib.sha256(str(database).encode()).hexdigest()[:16])
    _mkdir_durable(manifest_path.parent)
    with _store_lock(store):
        _require_new(manifest_path)
        if _source_stat(database) != initial:
            raise ValueError('static archive source changed while waiting for store lock')
        fd = os.open(database, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            _assert_static_source(database, initial, source.fileno())
            _integrity_check(database)
            _assert_static_source(database, initial, source.fileno())
            entries, digest, size = [], hashlib.sha256(), 0
            stats = {'new_objects': 0, 'reused_objects': 0, 'new_stored_bytes': 0}
            while raw := source.read(BLOCK_SIZE):
                entry, created = _write_object(store, raw)
                entries.append(entry)
                digest.update(raw)
                size += len(raw)
                stats['new_objects' if created else 'reused_objects'] += 1
                if created:
                    stats['new_stored_bytes'] += entry['stored_bytes']
            _assert_static_source(database, initial, source.fileno())
            database_sha = digest.hexdigest()
            second_sha, second_size = _hash_file(source)
            _assert_static_source(database, initial, source.fileno())
            if size != initial['size'] or second_size != size or second_sha != database_sha:
                raise ValueError('static archive source bytes changed during archival')
            manifest = {'schema': SCHEMA, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                        'source_path': str(database), 'database_sha256': database_sha,
                        'database_bytes': size, 'block_size': BLOCK_SIZE,
                        'store': os.path.relpath(store, manifest_path.parent), 'objects': entries, 'stats': stats,
                        'capture_mode': 'static-byte-archive', 'source_stat': initial}
            _assert_static_source(database, initial, source.fileno())
            _commit_manifest(manifest_path, manifest)
    return manifest


def verify_snapshot(manifest_path: Path) -> dict:
    path = _canonical_parent(manifest_path)
    manifest, store, manifest_sha = _read_manifest(path)
    with _store_lock(store, shared=True):
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=store) as directory:
            _materialize(manifest, store, Path(directory) / 'verified.sqlite3')
        _manifest_unchanged(path, manifest_sha)
    return {'status': 'pass', 'schema': SCHEMA, 'manifest_sha256': manifest_sha,
            'database_sha256': manifest['database_sha256'], 'database_bytes': manifest['database_bytes'],
            'objects': len(manifest['objects']), 'sqlite_integrity_check': 'ok'}


def restore_snapshot(manifest_path: Path, destination: Path) -> dict:
    path, destination = _canonical_parent(manifest_path), _canonical_parent(destination)
    manifest, store, manifest_sha = _read_manifest(path)
    _require_new(destination)
    resolved = destination.resolve()
    if resolved == Path(manifest['source_path']).resolve() or resolved == path.resolve() or resolved.is_relative_to(store):
        raise ValueError('restore cannot target original database, manifest or object store')
    _mkdir_durable(destination.parent)
    with _store_lock(store, shared=True):
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=destination.parent) as directory:
            image = Path(directory) / 'restored.sqlite3'
            _materialize(manifest, store, image)
            _manifest_unchanged(path, manifest_sha)
            _commit_new(image, destination)
    return {'status': 'pass', 'schema': SCHEMA, 'manifest_sha256': manifest_sha,
            'destination': str(destination), 'database_sha256': manifest['database_sha256'],
            'database_bytes': manifest['database_bytes'], 'sqlite_integrity_check': 'ok'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('manifest', type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('manifest', type=Path)
    restore.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = verify_snapshot(args.manifest) if args.command == 'verify' else restore_snapshot(args.manifest, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
