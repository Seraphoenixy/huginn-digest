"""仅在Worker持锁且流水线空闲时清理可再生产物，不跟随链接。"""
import sqlite3
import time
from pathlib import Path


def cleanup(settings, tasks, now=None):
    now=time.time() if now is None else now
    root=settings.data_dir.resolve()
    candidates=[]
    def collect(folder, days):
        if not folder.exists() or folder.is_symlink():return
        for path in folder.rglob('*'):
            if not path.is_file() or path.is_symlink():continue
            if any(p.is_symlink() for p in path.parents):continue
            if not path.resolve().is_relative_to(root):continue
            stat=path.stat()
            candidates.append((stat.st_mtime,path,stat.st_size,days))
    collect(settings.cache_dir,settings.get('storage','cache_days',1))
    for task in tasks:
        if task['status']!='done':continue
        folder=settings.content_dir/str(task['id'])
        # 只有完整聚合文件存在才允许清理对应分片。
        groups=[]
        if (folder/'raw.txt').is_file():groups+=list(folder.glob('part_*.txt'))
        if (folder/'corrected.txt').is_file():groups+=list(folder.glob('correction_*.txt'))
        if (folder/'summary.json').is_file():groups+=list(folder.glob('summary_part_*.json'))
        for path in groups:
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):continue
            if not path.resolve().is_relative_to(root):continue
            stat=path.stat();candidates.append((stat.st_mtime,path,stat.st_size,settings.get('storage','intermediate_days',7)))
        for archive in [*folder.glob('previous_summary_*'),*folder.glob('previous_correction_*')]:
            if archive.is_dir():collect(archive,settings.get('storage','intermediate_days',7))
    total=sum(item[2] for item in candidates)
    limit=settings.get('storage','cache_max_mb',512)*1024*1024
    removed=freed=0
    for stamp,path,size,days in sorted(candidates,key=lambda item:item[0]):
        if now-stamp<days*86400 and total<=limit:continue
        try:path.unlink()
        except OSError:continue
        total-=size;freed+=size;removed+=1
    removed, freed = prune_versions(settings, root, now, removed, freed)
    return {'files':removed,'bytes':freed,'remaining_bytes':total}


def prune_versions(settings, root, now, removed=0, freed=0):
    """清理日报版本文件：孤儿快照立即删除，非活动旧版本过保留期后连同索引行删除。

    活动版本始终保留；被引用的非活动版本一旦删文件，同事务删除对应versions行，
    避免后续切换到已不存在的快照。
    """
    reports = settings.data_dir / 'reports'
    if not reports.exists() or reports.is_symlink():
        return removed, freed
    keep = settings.get('storage', 'intermediate_days', 7)
    database = sqlite3.connect(settings.db_path, timeout=30)
    database.row_factory = sqlite3.Row
    try:
        active = {row['path'] for row in database.execute('SELECT path FROM versions WHERE active=1')}
        referenced = {row['path']: row['id'] for row in database.execute('SELECT id,path FROM versions')}
        for path in reports.rglob('*.json'):
            if not path.is_file() or path.is_symlink():
                continue
            if any(parent.is_symlink() for parent in path.parents):
                continue
            if not path.resolve().is_relative_to(root):
                continue
            name = str(path)
            if name in active:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if name in referenced and now - stat.st_mtime < keep * 86400:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            if name in referenced:
                database.execute('DELETE FROM versions WHERE id=?', (referenced[name],))
            removed += 1
            freed += stat.st_size
        # 文件已被外部删除的非活动版本行也一并清理，避免切换到不存在的快照。
        for row in database.execute('SELECT id,path FROM versions WHERE active=0'):
            if not Path(row['path']).is_file():
                database.execute('DELETE FROM versions WHERE id=?', (row['id'],))
        database.commit()
    finally:
        database.close()
    return removed, freed
