"""仅在Worker持锁且流水线空闲时清理可再生产物，不跟随链接。"""
import sqlite3
import time
from pathlib import Path

# 转写类中间产物：体积最大、只在重建时需要，且日报展示完全不依赖它们。
TRANSCRIPT_NAMES=('raw.txt','corrected.txt','feed.txt','video_description.txt','correction_manifest.json')
TRANSCRIPT_GLOBS=('part_*.txt','correction_*.txt','previous_correction_*')


def disk_usage(settings):
    """统计数据目录各类文件占用；只读遍历，不跟随符号链接。

    分类口径与清理逻辑一致，便于判断哪一类最该调整保留期。
    """
    root=settings.data_dir
    buckets={'transcripts':0,'summaries':0,'reports':0,'cache':0,'database':0,'other':0}
    counts={'transcripts':0,'summaries':0,'reports':0,'cache':0}
    if not root.exists() or root.is_symlink():
        return {'buckets':buckets,'counts':counts,'total':0}
    reports_dir=root/'reports'
    cache_dir=settings.cache_dir
    content_dir=settings.content_dir
    # WAL模式下行数据可能在 -wal/-shm，只匹配主文件会把数据库算成 0。
    db_files={str(settings.db_path),str(settings.db_path)+'-wal',str(settings.db_path)+'-shm'}
    for path in root.rglob('*'):
        try:
            if path.is_symlink() or not path.is_file():continue
            if any(parent.is_symlink() for parent in path.parents):continue
            size=path.stat().st_size
        except OSError:
            continue
        if str(path) in db_files:
            buckets['database']+=size
        elif _is_relative(path,reports_dir):
            buckets['reports']+=size;counts['reports']+=1
        elif _is_relative(path,cache_dir):
            buckets['cache']+=size;counts['cache']+=1
        elif _is_relative(path,content_dir):
            if path.name in TRANSCRIPT_NAMES or any(path.match(p) for p in TRANSCRIPT_GLOBS):
                buckets['transcripts']+=size;counts['transcripts']+=1
            elif path.suffix=='.json':
                buckets['summaries']+=size;counts['summaries']+=1
            else:
                buckets['other']+=size
        else:
            buckets['other']+=size
    return {'buckets':buckets,'counts':counts,'total':sum(buckets.values())}


def _is_relative(path,parent):
    try:return path.resolve().is_relative_to(parent.resolve())
    except (OSError,ValueError):return False


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
    removed, freed = prune_transcripts(settings, root, tasks, now, removed, freed)
    removed, freed = prune_versions(settings, root, now, removed, freed)
    return {'files':removed,'bytes':freed,'remaining_bytes':total}


def prune_transcripts(settings, root, tasks, now, removed=0, freed=0):
    """按transcript_days删除转写原文与纠错稿；摘要与日报快照不受影响。

    这些文件体积最大（单条约90KB）且只在重建时用于避免重复下载/ASR，日报展示
    只依赖summary.json与版本快照，删除后首页与历史日报照常可读。删除只按年龄，
    不受cache_max_mb容量压力影响：重新生成要再付出下载与ASR成本，提前删除更亏。
    transcript_days=0表示永久保留，与升级前行为一致。
    """
    days=settings.get('storage','transcript_days',30)
    if not days:return removed, freed
    cutoff=now-days*86400
    for task in tasks:
        if task['status']!='done':continue
        folder=settings.content_dir/str(task['id'])
        if not folder.is_dir() or folder.is_symlink():continue
        targets=[folder/name for name in TRANSCRIPT_NAMES]
        for pattern in TRANSCRIPT_GLOBS:
            targets.extend(folder.glob(pattern))
        for path in targets:
            if not path.is_file() or path.is_symlink():continue
            if any(p.is_symlink() for p in path.parents):continue
            try:
                if not path.resolve().is_relative_to(root):continue
                stat=path.stat()
            except OSError:
                continue
            if stat.st_mtime>=cutoff:continue
            try: path.unlink()
            except OSError: continue
            removed+=1;freed+=stat.st_size
    return removed, freed


def prune_versions(settings, root, now, removed=0, freed=0):
    """清理日报版本文件：孤儿快照立即删除，非活动旧版本过保留期后连同索引行删除。

    活动版本始终保留；被引用的非活动版本一旦删文件，同事务删除对应versions行，
    避免后续切换到已不存在的快照。
    活动行对应的文件若已丢失，先降级为非活动：页面不再出现指向空内容的死链，
    下一次publish会用新快照重新激活该日，而不是永久留一条坏指针。
    """
    reports = settings.data_dir / 'reports'
    if not reports.exists() or reports.is_symlink():
        return removed, freed
    keep = settings.get('storage', 'intermediate_days', 7)
    database = sqlite3.connect(settings.db_path, timeout=30)
    database.row_factory = sqlite3.Row
    try:
        expired=expired_days(settings, database, now)
        for day in expired:
            removed, freed = _drop_day(settings, database, root, day, removed, freed)
        for row in database.execute('SELECT id,path FROM versions WHERE active=1'):
            if not Path(row['path']).is_file():
                database.execute('UPDATE versions SET active=0 WHERE id=?', (row['id'],))
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


def expired_days(settings, database, now):
    """按report_days列出应整日删除的日期；0 表示永久保留。

    以日期本身而非文件时间判断：日报的“年龄”是它代表的新闻日期，
    重新发布过旧内容不应让旧日报重新获得保留期。日期按北京时间计算，
    与归档口径一致（UTC 会差一天）。
    """
    days=settings.get('storage','report_days',1095)
    if not days:return []
    from datetime import datetime, timedelta, timezone
    tz=timezone(timedelta(hours=8))
    cutoff=(datetime.fromtimestamp(now,tz)-timedelta(days=days)).date().isoformat()
    rows=database.execute('SELECT DISTINCT day FROM versions WHERE day<?',(cutoff,)).fetchall()
    return sorted(row['day'] for row in rows)


def _drop_day(settings, database, root, day, removed, freed):
    """删除一天的日报版本、磁盘快照与任务索引。

    任务行必须一起删：保留 done 任务会让下一次publish_dirty把该日重新发布，
    等于“删了又回来”。删掉任务行的代价是失去该 URL 的去重记忆，已记入README。
    """
    reports=settings.data_dir/'reports'/day
    rows=database.execute('SELECT id,path FROM versions WHERE day=?',(day,)).fetchall()
    for row in rows:
        path=Path(row['path'])
        if path.is_file() and path.resolve().is_relative_to(root) and not path.is_symlink():
            try:
                freed+=path.stat().st_size
                path.unlink()
                removed+=1
            except OSError:
                pass
        database.execute('DELETE FROM versions WHERE id=?',(row['id'],))
    if reports.is_dir() and not reports.is_symlink() and reports.resolve().is_relative_to(root):
        for path in sorted(reports.rglob('*'),reverse=True):
            try:
                if path.is_symlink():continue
                if path.is_file():
                    freed+=path.stat().st_size
                    path.unlink()
                    removed+=1
                elif path.is_dir():path.rmdir()
            except OSError:
                continue
        try: reports.rmdir()
        except OSError: pass
    tasks=database.execute('SELECT id FROM tasks WHERE day=?',(day,)).fetchall()
    for task in tasks:
        folder=settings.content_dir/str(task['id'])
        if folder.is_dir() and not folder.is_symlink() and folder.resolve().is_relative_to(root):
            for path in sorted(folder.rglob('*'),reverse=True):
                try:
                    if path.is_symlink():continue
                    if path.is_file():
                        freed+=path.stat().st_size
                        path.unlink()
                        removed+=1
                    elif path.is_dir():path.rmdir()
                except OSError:
                    continue
            try: folder.rmdir()
            except OSError: pass
        database.execute('DELETE FROM tasks WHERE id=?',(task['id'],))
    database.execute('DELETE FROM retractions WHERE day=?',(day,))
    return removed, freed
