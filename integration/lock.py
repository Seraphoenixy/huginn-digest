"""Worker单进程锁；进程退出后操作系统释放。"""
import os
from contextlib import contextmanager

@contextmanager
def worker_lock(path):
    stream = open(path,'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        stream.close()
