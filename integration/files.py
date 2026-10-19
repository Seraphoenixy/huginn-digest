"""正文读写与原子替换唯一出口。"""
import os
import tempfile
from pathlib import Path

def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return str(path)

def read_text(path):
    return Path(path).read_text(encoding='utf-8')
