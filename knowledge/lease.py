"""Prevent two API workers from concurrently replacing the same snapshots."""
import os
from contextlib import contextmanager


@contextmanager
def single_worker(database):
    file = open(database + ".lock", "a+b")
    locked = False
    try:
        file.seek(0, 2)
        if not file.tell():
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise RuntimeError("Another connector worker owns this database; use --workers 1") from None
        yield
    finally:
        if locked:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()
