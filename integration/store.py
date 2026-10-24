"""任务、订阅与发布索引唯一出口；正文只存文件路径。"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,last_poll REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',UNIQUE(kind,target));
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,source_id INTEGER,kind TEXT NOT NULL,url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,published REAL NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',stage TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,available REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',warning TEXT NOT NULL DEFAULT '',raw_path TEXT,corrected_path TEXT,summary_path TEXT,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY,day TEXT NOT NULL,path TEXT NOT NULL,fingerprint TEXT NOT NULL,final INTEGER NOT NULL,created REAL NOT NULL,active INTEGER NOT NULL DEFAULT 0,UNIQUE(day,fingerprint,final));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,task_id INTEGER,stage TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_limits(ip TEXT PRIMARY KEY,count INTEGER NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rebuilds(id INTEGER PRIMARY KEY,day TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',error TEXT NOT NULL DEFAULT '',created REAL NOT NULL DEFAULT 0,started REAL NOT NULL DEFAULT 0,updated REAL NOT NULL,batch TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS retractions(id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,day TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',created REAL NOT NULL,UNIQUE(task_id));
CREATE UNIQUE INDEX IF NOT EXISTS rebuilds_active_day ON rebuilds(day) WHERE status IN ('queued','running');
'''

class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            # WAL是数据库文件级持久设置；必须在任何DDL/DML事务之前设置。
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(sources)')}
            if 'position' not in columns:
                db.execute('ALTER TABLE sources ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
                rows=db.execute('SELECT id FROM sources ORDER BY id DESC').fetchall()
                db.executemany('UPDATE sources SET position=? WHERE id=?',[(i,row['id']) for i,row in enumerate(rows)])
            version_columns = {row['name'] for row in db.execute('PRAGMA table_info(versions)')}
            if 'active' not in version_columns:
                # 旧库按“每天最后写入的版本”迁移为活动版本。
                db.execute('ALTER TABLE versions ADD COLUMN active INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE versions SET active=1 WHERE id IN (SELECT MAX(id) FROM versions GROUP BY day)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS versions_active_day ON versions(day) WHERE active=1')
            event_columns={row['name'] for row in db.execute('PRAGMA table_info(events)')}
            if 'detail' not in event_columns:
                db.execute("ALTER TABLE events ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
            rebuild_columns={row['name'] for row in db.execute('PRAGMA table_info(rebuilds)')}
            if 'created' not in rebuild_columns:
                db.execute('ALTER TABLE rebuilds ADD COLUMN created REAL NOT NULL DEFAULT 0')
                db.execute('UPDATE rebuilds SET created=updated WHERE created=0')
            if 'started' not in rebuild_columns:
                db.execute('ALTER TABLE rebuilds ADD COLUMN started REAL NOT NULL DEFAULT 0')
            if 'batch' not in rebuild_columns:
                db.execute("ALTER TABLE rebuilds ADD COLUMN batch TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def query(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def add_source(self, kind, target, name):
        """显式分配单调递增的来源ID。

        SQLite 的 INTEGER PRIMARY KEY 在删除最大行号后会复用该号，导致旧任务
        的 source_id 指向新来源（旧内容显示为新来源）。这里用 meta 计数保证
        已用过的ID永不再分配；种子同时考虑 sources 与 tasks 的历史引用。
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM meta WHERE key='source_id_seq'").fetchone()
            seed=0
            if row is not None:
                seed=int(row['value'])
            else:
                for sql in ('SELECT COALESCE(MAX(id),0) AS value FROM sources',
                            'SELECT COALESCE(MAX(source_id),0) AS value FROM tasks'):
                    seed=max(seed,int(db.execute(sql).fetchone()['value']))
            next_id=seed+1
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('source_id_seq',?)",(str(next_id),))
            db.execute('INSERT INTO sources(id,kind,target,name,created,position) VALUES(?,?,?,?,?,'
                       '(SELECT COALESCE(MAX(position),-1)+1 FROM sources))',
                       (next_id,kind,target,name,time.time()))
            return next_id

    def sources(self):
        return self.query('SELECT * FROM sources ORDER BY position,id DESC')

    def source_exists(self, source_id):
        return bool(self.query('SELECT id FROM sources WHERE id=?',(source_id,)))

    def delete_source(self, source_id):
        """删除来源并取消其未完成任务；已发布日报与已完成摘要保持不变。

        仅删除索引行不会阻止已入队任务继续下载该来源的正文，因此这里把
        queued 与 failed 一并标记为 canceled：failed 任务若保留，管理页会显示
        一个点了就重新入队的“重试”按钮，等于绕过删除继续抓取。
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now=time.time()
            db.execute("UPDATE tasks SET status='canceled',available=0,error='source: SourceDeleted',updated=? "
                       "WHERE status IN ('queued','failed') AND source_id=?",(now,source_id))
            db.execute('DELETE FROM sources WHERE id=?',(source_id,))

    def cancel_orphan_tasks(self):
        """兜底：来源已不存在（含手工改库、删除时正在运行）的任务不再被认领或重试。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor=db.execute("UPDATE tasks SET status='canceled',available=0,error='source: SourceDeleted',updated=? "
                              "WHERE status IN ('queued','failed','running') AND source_id IS NOT NULL "
                              'AND source_id NOT IN (SELECT id FROM sources)',(time.time(),))
            return cursor.rowcount

    def reorder_sources(self,ids):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current={row['id'] for row in db.execute('SELECT id FROM sources')}
            if len(ids)!=len(set(ids)) or set(ids)!=current:raise ValueError('来源列表已变化，请刷新后重试')
            db.executemany('UPDATE sources SET position=? WHERE id=?',[(i,source_id) for i,source_id in enumerate(ids)])

    def edit_source(self,source_id,kind,target,name):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
            if old is None:raise ValueError('来源不存在')
            changed=old['kind']!=kind or old['target']!=target
            db.execute('UPDATE sources SET kind=?,target=?,name=?,created=?,last_poll=?,error=? WHERE id=?',
                       (kind,target,name,time.time() if changed else old['created'],0 if changed else old['last_poll'],'' if changed else old['error'],source_id))

    def enqueue(self, item):
        source_id=item.get('source_id')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if source_id is not None and db.execute('SELECT id FROM sources WHERE id=?',(source_id,)).fetchone() is None:
                # 来源已被删除时不再入队，避免删除与发现并发时补回内容。
                return False
            result = db.execute('INSERT OR IGNORE INTO tasks(source_id,kind,url,title,published,day,updated) VALUES(?,?,?,?,?,?,?)', (source_id,item['kind'],item['url'],item['title'],item['published'],item['day'],time.time()))
            return result.rowcount > 0

    def claim(self,day=None):
        """原子认领；来源已删除的任务不认领，防止删除后仍抓取。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sql=("SELECT * FROM tasks WHERE status='queued' AND available<=? "
                 'AND (source_id IS NULL OR source_id IN (SELECT id FROM sources))')
            args=[time.time()]
            if day is not None:sql+=' AND day=?';args.append(day)
            task = db.execute(sql+' ORDER BY id LIMIT 1',args).fetchone()
            if task is None:
                return None
            db.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated=? WHERE id=?", (time.time(),task['id']))
            return self.task_dict(task)

    @staticmethod
    def task_dict(task):
        return dict(task)

    def task(self, task_id):
        rows = self.query('SELECT * FROM tasks WHERE id=?',(task_id,))
        if not rows:
            raise ValueError('任务不存在')
        return rows[0]

    def update(self, task_id, **fields):
        allowed = {'status','stage','error','warning','raw_path','corrected_path','summary_path','available'}
        if not fields or set(fields) - allowed:
            raise ValueError('非法任务字段')
        fields['updated'] = time.time()
        self.execute('UPDATE tasks SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(),task_id))

    def event(self, task_id, stage, status, detail=''):
        self.execute('INSERT INTO events(task_id,stage,status,detail,created) VALUES(?,?,?,?,?)',
                     (task_id,stage,status,str(detail)[:200],time.time()))

    def recent_events(self, limit=12):
        return self.query('SELECT e.*,COALESCE(t.title,\'\') AS title FROM events e '
                          'LEFT JOIN tasks t ON t.id=e.task_id ORDER BY e.id DESC LIMIT ?',(int(limit),))

    def recover(self):
        """崩溃恢复：running 任务重新排队；来源已删除的排队任务直接取消。

        同时清除范围重建的扫描标记：批次中途崩溃意味着那次扫描之后可能又有新内容，
        沿用旧标记会让整批在重启后不再采集，等于静默漏掉内容。
        """
        self.execute("UPDATE tasks SET status='queued' WHERE status='running' "
                     'AND (source_id IS NULL OR source_id IN (SELECT id FROM sources))')
        self.execute("UPDATE tasks SET status='canceled' WHERE status='running' "
                     "AND source_id IS NOT NULL AND source_id NOT IN (SELECT id FROM sources)")
        self.execute("UPDATE rebuilds SET status='queued' WHERE status='running'")
        self.execute("DELETE FROM meta WHERE key LIKE 'rebuild_scan:%'")

    def request_rebuild(self,day,batch=''):
        now=time.time()
        with self.connect() as db:
            result=db.execute('INSERT OR IGNORE INTO rebuilds(day,status,error,created,started,updated,batch) VALUES(?,?,?,?,?,?,?)',
                              (day,'queued','',now,0,now,batch))
            return result.rowcount>0

    def request_rebuild_range(self,days,batch):
        """把一段日期展开为多条目重建任务，共用batch便于聚合展示。

        复用按日队列而非新增“区间任务”类型：保留每日去重与失败隔离，
        也不必改动唯一索引和既有重建流程。
        """
        now=time.time()
        inserted=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for day in days:
                result=db.execute('INSERT OR IGNORE INTO rebuilds(day,status,error,created,started,updated,batch) '
                                  'VALUES(?,?,?,?,?,?,?)',(day,'queued','',now,0,now,batch))
                if result.rowcount>0:inserted.append(day)
            return inserted

    def days_with_content(self,days):
        """筛出有任务或已发布版本的日期，供范围重建跳过完全空白的日期。"""
        days=[day for day in days if day]
        if not days:return []
        placeholders=','.join('?' for _ in days)
        rows=self.query('SELECT day FROM tasks WHERE day IN ('+placeholders+') '
                        'UNION SELECT day FROM versions WHERE day IN ('+placeholders+')',tuple(days)*2)
        found={row['day'] for row in rows}
        return sorted(day for day in days if day in found)

    def batch_progress(self,batch):
        rows=self.query('SELECT status,COUNT(*) AS count FROM rebuilds WHERE batch=? GROUP BY status',(batch,))
        counts={row['status']:int(row['count']) for row in rows}
        for status in ('queued','running','done','failed'):
            counts.setdefault(status,0)
        counts['total']=sum(counts.values())
        return counts

    def claim_rebuild(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM rebuilds WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:return None
            now=time.time()
            db.execute("UPDATE rebuilds SET status='running',started=?,updated=? WHERE id=?",(now,now,row['id']))
            return dict(row)

    def finish_rebuild(self, rebuild_id, status, error=''):
        self.execute('UPDATE rebuilds SET status=?,error=?,updated=? WHERE id=?',(status,str(error)[:500],time.time(),rebuild_id))

    def rebuilds(self, limit=20):
        return self.query('SELECT * FROM rebuilds ORDER BY id DESC LIMIT ?',(int(limit),))

    def active_rebuilds(self, limit=5):
        return self.query("SELECT * FROM rebuilds WHERE status IN ('queued','running') ORDER BY id LIMIT ?",(int(limit),))

    def retry(self, task_id):
        """手动重试：放行一次新尝试，但不重置累计次数。

        原先重置attempts=0会让每次点击都重新获得完整的自动重试额度，
        等于无限重试；现在保留累计值，由worker.max_total_attempts兜底。
        """
        self.execute("UPDATE tasks SET status='queued',available=0,error='',updated=? "
                     "WHERE id=? AND status='failed' "
                     'AND (source_id IS NULL OR source_id IN (SELECT id FROM sources))',(time.time(),task_id))

    def task_counts(self):
        rows=self.query('SELECT status,COUNT(*) AS count FROM tasks GROUP BY status')
        counts={row['status']:int(row['count']) for row in rows}
        for status in ('queued','running','done','failed','canceled'):
            counts.setdefault(status,0)
        counts['all']=sum(counts.values())
        # 撤回是发布层状态，不是任务状态；单独计数，不并入 done。
        rows=self.query('SELECT COUNT(*) AS count FROM retractions')
        counts['retracted']=int(rows[0]['count']) if rows else 0
        return counts

    def day_progress(self, days):
        """按日汇总任务总数与完成数，供重建进度展示；空列表直接返回。"""
        days=[day for day in days if day]
        if not days:return {}
        placeholders=','.join('?' for _ in days)
        rows=self.query('SELECT day,COUNT(*) AS total,'
                        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
                        'FROM tasks WHERE day IN ('+placeholders+') GROUP BY day',tuple(days))
        return {row['day']:(int(row['total']),int(row['done'] or 0)) for row in rows}

    def dashboard_tasks(self, view='all', day=None, limit=100):
        """管理页任务视图；状态白名单在存储层映射，不接受拼接SQL。"""
        statuses={'active':('queued','running'),'failed':('failed',),'done':('done',),
                  'canceled':('canceled',)}.get(view)
        clauses=[];args=[]
        if statuses:
            clauses.append('t.status IN ('+','.join('?' for _ in statuses)+')')
            args.extend(statuses)
        if day:
            clauses.append('t.day=?')
            args.append(day)
        if view=='retracted':
            clauses.append('r.task_id IS NOT NULL')
        where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
        args.append(int(limit))
        sql=('SELECT t.*,CASE WHEN t.source_id IS NOT NULL THEN COALESCE(s.name,\'已删除的订阅来源\') '
             'WHEN t.kind=\'bili\' THEN \'B站 · 单独提交\' ELSE \'RSS · 单独提交\' END AS source_name, '
             'CASE WHEN r.task_id IS NOT NULL THEN 1 ELSE 0 END AS retracted, '
             'COALESCE(r.reason,\'\') AS retract_reason, COALESCE(r.created,0) AS retracted_at '
             'FROM tasks t LEFT JOIN sources s ON s.id=t.source_id '
             'LEFT JOIN retractions r ON r.task_id=t.id'+where+
             " ORDER BY CASE t.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 "
             "WHEN 'canceled' THEN 3 ELSE 4 END,t.id DESC LIMIT ?")
        return self.query(sql,tuple(args))

    def latest(self, day):
        rows = self.query('SELECT * FROM versions WHERE day=? AND active=1 LIMIT 1',(day,))
        return rows[0] if rows else None

    def clear_active(self, day):
        """撤回全部条目后移除当天的活动指针；历史版本行与文件保留。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE versions SET active=0 WHERE day=? AND active=1',(day,))

    def retract(self, task_id, day, reason=''):
        """记录撤回决定；不删除摘要、任务行或日报版本，保证可恢复。"""
        self.execute('INSERT OR IGNORE INTO retractions(task_id,day,reason,created) VALUES(?,?,?,?)',
                     (task_id,day,str(reason)[:200],time.time()))

    def restore(self, task_id):
        """撤销撤回；返回是否真的删掉了一条记录，便于区分无效恢复。"""
        with self.connect() as db:
            return db.execute('DELETE FROM retractions WHERE task_id=?',(task_id,)).rowcount>0

    def retracted_ids(self, day):
        return {row['task_id'] for row in self.query('SELECT task_id FROM retractions WHERE day=?',(day,))}

    def version_by_fingerprint(self, day, fingerprint, final):
        rows = self.query('SELECT * FROM versions WHERE day=? AND fingerprint=? AND final=?',(day,fingerprint,int(final)))
        return rows[0] if rows else None

    def publish_version(self, day, path, fingerprint, final, created):
        """写入新版本并切换活动指针；同一事务内保证每天只有一个活动版本。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM versions WHERE day=? AND fingerprint=? AND final=?',
                                  (day,fingerprint,int(final))).fetchone()
            if existing is None:
                cursor = db.execute('INSERT INTO versions(day,path,fingerprint,final,created,active) VALUES(?,?,?,?,?,0)',
                                    (day,str(path),fingerprint,int(final),created))
                version_id = cursor.lastrowid
            else:
                version_id = existing['id']
                # 文件丢失的同内容版本要重新指向新快照，否则活动指针会指向不存在的文件。
                if not Path(existing['path']).is_file():
                    db.execute('UPDATE versions SET path=?,created=? WHERE id=?',(str(path),created,version_id))
            db.execute('UPDATE versions SET active=0 WHERE day=? AND active=1 AND id<>?',(day,version_id))
            db.execute('UPDATE versions SET active=1 WHERE id=?',(version_id,))
            return version_id

    def set_active(self, day, version_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE versions SET active=0 WHERE day=? AND active=1 AND id<>?',(day,version_id))
            db.execute('UPDATE versions SET active=1 WHERE id=?',(version_id,))

    def versions(self, day):
        return self.query('SELECT * FROM versions WHERE day=? ORDER BY id',(day,))

    def drop_version(self, version_id):
        self.execute('DELETE FROM versions WHERE id=?',(version_id,))

    def done_signature(self):
        """廉价发布签名：只在任务完成状态或更新时间变化时才需要重算日报。"""
        rows = self.query("SELECT day,COUNT(*) AS total,COALESCE(SUM(id),0) AS ids,COALESCE(MAX(updated),0) AS updated FROM tasks WHERE status='done' GROUP BY day")
        return {row['day']:(row['total'],row['ids'],round(row['updated'],3)) for row in rows}

    def prune_events(self, before):
        self.execute('DELETE FROM events WHERE created<?',(before,))

    def meta(self, key):
        rows = self.query('SELECT value FROM meta WHERE key=?',(key,))
        return rows[0]['value'] if rows else ''

    def set_meta(self,key,value):
        self.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
