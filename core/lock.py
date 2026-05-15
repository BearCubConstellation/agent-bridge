"""
Agent Bridge — 跨平台文件锁

提供两种锁接口：
  - lock_file(f) / unlock_file(f)  — 低级函数式接口（用于 send/poll）
  - file_lock(lock_path)           — 上下文管理器接口（用于 server API）

用法:
    from lock import lock_file, unlock_file
    with open("data.jsonl", "a") as f:
        lock_file(f)
        f.write(...)
        unlock_file(f)

    from lock import file_lock
    with file_lock("/tmp/data.lock"):
        ...
"""
import contextlib
import platform

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    import msvcrt

    def lock_file(f):
        """Acquire an exclusive lock on *f* (Windows)."""
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        f.seek(0, 2)  # seek to end so append position is correct after lock

    def unlock_file(f):
        """Release the lock on *f* (Windows)."""
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    def _flock_ex(fd):
        """Exclusive lock by path (Windows)."""
        # Use msvcrt locking on the file descriptor
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _flock_un(fd):
        """Unlock by path (Windows)."""
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    try:
        import fcntl as _fcntl

        def lock_file(f):
            """Acquire an exclusive lock on *f* (Unix)."""
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)

        def unlock_file(f):
            """Release the lock on *f* (Unix)."""
            try:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass

        def _flock_ex(fd):
            _fcntl.flock(fd, _fcntl.LOCK_EX)

        def _flock_un(fd):
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass

    except ImportError:
        # Extremely unlikely on Unix, but fall back gracefully
        def lock_file(f):
            pass

        def unlock_file(f):
            pass

        def _flock_ex(fd):
            pass

        def _flock_un(fd):
            pass


@contextlib.contextmanager
def file_lock(lock_path):
    """Acquire an exclusive file lock (context manager).

    Usage:
        with file_lock("/tmp/data.lock"):
            ...
    """
    fd = None
    try:
        fd = open(lock_path, "w")
        _flock_ex(fd.fileno())
        yield fd
    finally:
        if fd is not None:
            _flock_un(fd.fileno())
            fd.close()
