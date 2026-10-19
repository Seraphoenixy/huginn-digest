"""仅在Worker持锁且流水线空闲时清理可再生产物，不跟随链接。"""
import time


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
        for archive in folder.glob('previous_summary_*'):
            if archive.is_dir():collect(archive,settings.get('storage','intermediate_days',7))
    total=sum(item[2] for item in candidates)
    limit=settings.get('storage','cache_max_mb',512)*1024*1024
    removed=freed=0
    for stamp,path,size,days in sorted(candidates,key=lambda item:item[0]):
        if now-stamp<days*86400 and total<=limit:continue
        try:path.unlink()
        except OSError:continue
        total-=size;freed+=size;removed+=1
    return {'files':removed,'bytes':freed,'remaining_bytes':total}
