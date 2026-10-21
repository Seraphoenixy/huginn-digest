"""Worker单进程锁；进程退出后操作系统释放。"""
import os
from contextlib import contextmanager


def _open_lock_file(path):
    # 不使用追加模式：a+b 会忽略 seek，每次加锁都把锁文件写长1字节。
    try:
        return open(path, 'r+b')
    except FileNotFoundError:
        return open(path, 'w+b')


@contextmanager
def worker_lock(path):
    stream = _open_lock_file(path)
    try:
        if os.name == 'nt':
            import msvcrt
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()
