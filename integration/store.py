"""任务、订阅与发布索引唯一出口；正文只存文件路径。"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,last_poll REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',UNIQUE(kind,target));
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,source_id INTEGER,kind TEXT NOT NULL,url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,published REAL NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',stage TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,available REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',warning TEXT NOT NULL DEFAULT '',raw_path TEXT,corrected_path TEXT,summary_path TEXT,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY,day TEXT NOT NULL,path TEXT NOT NULL,fingerprint TEXT NOT NULL,final INTEGER NOT NULL,created REAL NOT NULL,active INTEGER NOT NULL DEFAULT 0,UNIQUE(day,fingerprint,final));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,task_id INTEGER,stage TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_limits(ip TEXT PRIMARY KEY,count INTEGER NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rebuilds(id INTEGER PRIMARY KEY,day TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
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
        return self.execute('INSERT INTO sources(kind,target,name,created,position) VALUES(?,?,?,?,(SELECT COALESCE(MAX(position),-1)+1 FROM sources))', (kind, target, name, time.time()))

    def sources(self):
        return self.query('SELECT * FROM sources ORDER BY position,id DESC')

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
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO tasks(source_id,kind,url,title,published,day,updated) VALUES(?,?,?,?,?,?,?)', (item.get('source_id'),item['kind'],item['url'],item['title'],item['published'],item['day'],time.time()))
            return result.rowcount > 0

    def claim(self,day=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sql="SELECT * FROM tasks WHERE status='queued' AND available<=?"
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

    def event(self, task_id, stage, status):
        self.execute('INSERT INTO events(task_id,stage,status,created) VALUES(?,?,?,?)',(task_id,stage,status,time.time()))

    def recover(self):
        self.execute("UPDATE tasks SET status='queued' WHERE status='running'")
        self.execute("UPDATE rebuilds SET status='queued' WHERE status='running'")

    def request_rebuild(self,day):
        with self.connect() as db:
            result=db.execute('INSERT OR IGNORE INTO rebuilds(day,updated) VALUES(?,?)',(day,time.time()))
            return result.rowcount>0

    def claim_rebuild(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM rebuilds WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:return None
            db.execute("UPDATE rebuilds SET status='running',updated=? WHERE id=?",(time.time(),row['id']))
            return dict(row)

    def retry(self, task_id):
        self.execute("UPDATE tasks SET status='queued',available=0,attempts=0,error='',updated=? WHERE id=? AND status='failed'",(time.time(),task_id))

    def latest(self, day):
        rows = self.query('SELECT * FROM versions WHERE day=? AND active=1 LIMIT 1',(day,))
        return rows[0] if rows else None

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
