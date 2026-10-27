"""Platform-specific filesystem primitives for the isolated literature workflow.

No unlocked fallback is provided. Unsupported durability or hardlink operations
raise an error, so publication can stop before changing the knowledge database.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time


@contextlib.contextmanager
def file_lock(path: Path, *, shared: bool = False, blocking: bool = False):
    """Hold a process-level lock until context exit; Windows shared is exclusive."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('lock path cannot be a symlink')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            # Byte-range locking requires a lockable byte. Never truncate a lock.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    # Only contention is retried; all other errors fail closed.
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise BlockingIOError(exc.errno, 'lock already held', str(path)) from exc
                    time.sleep(0.05)
        else:
            import fcntl
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(fd, mode | (0 if blocking else fcntl.LOCK_NB))
            locked = True
        yield
    finally:
        if locked:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def fsync_directory(path: Path) -> None:
    """Flush directory metadata or raise; never claim durability on failure."""
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    # Windows requires a directory handle opened with BACKUP_SEMANTICS. A file
    # system/ACL may reject a metadata flush; propagate the error in that case.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    flush = kernel.FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(Path(path)), 0x40000000, 0x1 | 0x2 | 0x4,
                    None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def ensure_material_alias(alias: Path, source: Path) -> None:
    """Link source when permitted; otherwise make a checked, exclusive copy."""
    alias, source = Path(alias), Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError('material source must be a regular file')
    if alias.is_symlink():
        if alias.resolve() != source:
            raise ValueError('material alias changed')
        fsync_directory(alias.parent)
        return
    if alias.exists():
        if not alias.is_file() or _digest(alias) != _digest(source):
            raise ValueError('material alias changed')
        fsync_directory(alias.parent)
        return
    try:
        alias.symlink_to(source)
    except OSError as exc:
        allowed = (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EINVAL,
                   getattr(errno, 'ENOTSUP', errno.EPERM))
        if exc.errno not in allowed and getattr(exc, 'winerror', None) != 1314:
            raise
    else:
        fsync_directory(alias.parent)
        return
    before = _digest(source)
    fd, name = tempfile.mkstemp(prefix='.' + alias.name + '-', dir=alias.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as dst, source.open('rb') as src:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if _digest(temporary) != before or _digest(source) != before:
            raise ValueError('material source changed during copying')
        # Hard link gives a no-replace commit. An unsupported filesystem fails.
        try:
            os.link(temporary, alias)
        except FileExistsError as exc:
            raise ValueError('material alias appeared during copy') from exc
        fsync_directory(alias.parent)
    finally:
        temporary.unlink(missing_ok=True)
