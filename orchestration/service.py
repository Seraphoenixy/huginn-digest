"""交互唯一门面：订阅、任务、发布、管理。"""
import hashlib
import json
import time
from contextlib import contextmanager
from threading import Event, Thread, Lock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from integration.store import Store
from integration.files import read_text, write_text
from integration.sources.bili import BiliVideoSource, BiliSourceError, extract_bvid_from_url, extract_mid_from_url
from integration.sources.rss import discover
from integration.http.public import public_url
from integration.auth import Sessions, verify_password
from integration.runtime_log import log

TZ=timezone(timedelta(hours=8))

def day_of(stamp):
    return datetime.fromtimestamp(stamp,TZ).date().isoformat()

def normalize_day(day):
    """接受用户输入的宽松日期，统一存成与任务相同的ISO格式。"""
    return datetime.strptime(str(day).strip(),'%Y-%m-%d').date().isoformat()

def is_final(day,now=None):
    now=datetime.fromtimestamp(time.time() if now is None else now,TZ)
    cutoff=datetime.fromisoformat(day).replace(tzinfo=TZ)+timedelta(days=1,hours=8)
    return now>=cutoff

class DailyService:
    def __init__(self,settings):
        self.settings=settings
        self.store=Store(settings.db_path)
        self.pipeline=None
        self.sessions=Sessions(settings.session_secret)
        self._login_lock=Lock()
        self._waiting=False
        self._cleanup_at=0
        self._publish_signature={}

    def session(self,token):
        value=self.sessions.read(token)
        if value.get('admin') and self.store.meta('revoked:'+hashlib.sha256(token.encode()).hexdigest()):
            return {}
        return value

    def logout(self,token):
        if self.session(token).get('admin'):
            self.store.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                               ('revoked:'+hashlib.sha256(token.encode()).hexdigest(),str(time.time())))
        self.store.execute("DELETE FROM meta WHERE key LIKE 'revoked:%' AND CAST(value AS REAL)<?",(time.time()-43200,))

    def new_session(self,admin=False):return self.sessions.issue(admin)

    def login(self,password,ip):
        with self._login_lock:
            return self._login(password,ip)

    def _login(self,password,ip):
        if not self.sessions.ready or not self.settings.admin_hash:
            raise ValueError('请在服务器配置管理密码哈希与至少32位SESSION_SECRET')
        now=time.time()
        limits=self.store.query('SELECT * FROM login_limits WHERE ip=?',(ip,))
        if limits and limits[0]['until']>now and limits[0]['count']>=5:
            return False
        valid=len(password)<=1024 and verify_password(password,self.settings.admin_hash)
        if valid:self.store.execute('DELETE FROM login_limits WHERE ip=?',(ip,))
        else:
            self.store.execute('INSERT INTO login_limits(ip,count,until) VALUES(?,1,?) ON CONFLICT(ip) DO UPDATE SET count=CASE WHEN until<? THEN 1 ELSE count+1 END,until=CASE WHEN until<? THEN excluded.until ELSE until END',(ip,now+900,now,now))
        return valid

    def configure_admin(self, password):
        from orchestration.credentials import configure_credentials
        configure_credentials(self.settings, password)

    def check_runtime(self, media=False):
        if media:
            self.check_media_tools()
        from integration.runtime_check import check_runtime
        from orchestration.pipeline import ContentPipeline
        ContentPipeline(self.settings, self.store)
        return ['LangGraph：可用'] + check_runtime(self.settings, media=media)

    def check_media_tools(self, required=True):
        from integration.media import check_media_tools, MediaToolsError
        try:
            check_media_tools()
        except MediaToolsError as exc:
            if required:
                log(f'后台启动失败：{exc}')
                raise
            log(f'媒体依赖警告：{exc} 公开阅读和管理仍可使用，视频后台需修复后启动。')
            return False
        log('FFmpeg/FFprobe检测通过')
        return True

    @contextmanager
    def background_worker(self):
        """与Web同进程运行；启动完成才接收请求，退出时等待当前轮结束。"""
        if not self.check_media_tools(required=False):
            yield
            return
        stop = Event()
        ready = Event()
        failures = []

        def run():
            try:
                self.run_forever(stop_event=stop, ready_event=ready)
            except BaseException as exc:
                failures.append(exc)
            finally:
                ready.set()

        thread = Thread(target=run, name='daily-worker', daemon=False)
        thread.start()
        try:
            ready.wait()
            if failures:
                raise RuntimeError('后台启动失败，请检查任务锁与运行依赖') from None
            yield
        finally:
            stop.set()
            thread.join()

    def run_forever(self, once=False, stop_event=None, ready_event=None):
        from integration.lock import worker_lock
        with worker_lock(self.settings.data_dir/'worker.lock'):
            self.check_media_tools()
            from orchestration.pipeline import ContentPipeline
            self.pipeline=ContentPipeline(self.settings,self.store)
            recovered=len(self.store.query("SELECT id FROM tasks WHERE status='running'"))
            self.store.recover()
            log(f'后台已启动；来源 {len(self.store.sources())} 个，恢复任务 {recovered} 个；来源检查间隔 {self.settings.get("pipeline","poll_seconds",3600)} 秒')
            if ready_event is not None:
                ready_event.set()
            if once:
                self.tick()
                return
            stop_event = stop_event or Event()
            while not stop_event.is_set():
                try:
                    self.tick()
                    stop_event.wait(self.settings.get('worker','idle_seconds',5))
                except KeyboardInterrupt:
                    log('后台已停止；未完成任务将在下次启动时恢复')
                    return
                except Exception as exc:
                    log(f'后台轮次失败：{type(exc).__name__}')
                    stop_event.wait(self.settings.get('worker','idle_seconds',5))
            log('后台已停止；未完成任务将在下次启动时恢复')

    def add_source(self,kind,target,name):
        kind,target,name=self.normalize_source(kind,target,name)
        return self.store.add_source(kind,target,name)

    @staticmethod
    def normalize_source(kind,target,name):
        target=target.strip()
        if kind=='bili':
            target=extract_mid_from_url(target)
            if not target or not str(target).isdigit():raise ValueError('请输入UP主MID或空间链接')
        elif kind=='rss':public_url(target)
        else:raise ValueError('来源类型无效')
        return kind,str(target),name.strip() or str(target)

    def get_source(self,source_id):
        rows=self.store.query('SELECT * FROM sources WHERE id=?',(source_id,))
        if not rows:raise ValueError('来源不存在')
        return rows[0]

    def reorder_sources(self,ids):
        self.store.reorder_sources(ids)

    def edit_source(self,source_id,kind,target,name):
        kind,target,name=self.normalize_source(kind,target,name)
        self.store.edit_source(source_id,kind,target,name)

    def submit_video(self,target):
        bvid=extract_bvid_from_url(target)
        if not bvid:raise ValueError('请输入BV号或含BV号的视频链接')
        # 查询指定视频元信息；绝不访问UP主列表。
        source=self.bili()
        try:info=source.get_video_info(bvid)
        finally:source.session.close()
        if not info:raise ValueError('视频不可访问，请检查登录态或链接')
        return self.enqueue('bili',{'url':f'https://www.bilibili.com/video/{bvid}','title':info.get('title',bvid),'published':info.get('pubdate',time.time())})

    def bili(self):
        return BiliVideoSource(cookie_file=self.settings.cookie_file or None,download_cache_dir=str(self.settings.cache_dir/'downloads'),auto_refresh=self.settings.bili_auto_refresh,refresh_check_seconds=self.settings.bili_refresh_check_seconds,refresh_retry_seconds=self.settings.bili_refresh_retry_seconds)

    def configure_bili_login(self, cookie, refresh_token):
        """本机凭据导入门面，不访问B站，不将凭据写入任务库。"""
        from integration.bili_cookies import BiliSession
        from integration.sources.bili import sanitize_cookie
        if not self.settings.cookie_file:
            raise ValueError('请先配置 bili.cookie_file')
        with BiliSession(self.settings.cookie_file) as session:
            try:
                session.import_login(cookie, refresh_token, sanitize_cookie)
            except Exception:
                raise ValueError('B站凭据保存失败，请检查Cookie、刷新口令和文件权限') from None

    def enqueue(self,kind,item,source_id=None):
        stamp=float(item['published'])
        if stamp>time.time()+86400:raise ValueError('来源时间异常')
        item=dict(item,kind=kind,source_id=source_id,day=day_of(stamp))
        inserted=self.store.enqueue(item)
        if inserted and item.get('body'):
            row=self.store.query('SELECT id FROM tasks WHERE url=?',(item['url'],))[0]
            write_text(self.settings.content_dir/str(row['id'])/'feed.txt',item['body'])
        return inserted

    def poll(self,day=None):
        now=time.time()
        result={'errors':[],'capped':False}
        start=datetime.strptime(normalize_day(day),'%Y-%m-%d').replace(tzinfo=TZ).timestamp() if day else None
        interval=self.settings.get('pipeline','poll_seconds',3600)
        for source in self.store.sources():
            if not source['enabled'] or (day is None and now-source['last_poll']<interval):continue
            started=time.perf_counter()
            log(f'来源 {source["id"]}（{source["kind"]}）：开始检查新增内容')
            try:
                cutoff=start if day else source['created']-self.settings.get('pipeline','lookback_hours',24)*3600
                if source['kind']=='rss':items=discover(source['target'],now)
                else:
                    client=self.bili();items=[]
                    try:
                        # 逐页、按时间截断；上限防止来源异常导致全量扫描。
                        for page in range(1,11):
                            log(f'来源 {source["id"]}：读取投稿第 {page} 页')
                            videos,_=client.fetch_page(source['target'],page,30)
                            if not videos:break
                            oldest=now
                            for video in videos:
                                stamp=float(video.get('created',0));oldest=min(oldest,stamp)
                                bvid=video.get('bvid')
                                if stamp>=cutoff and bvid:items.append({'url':f'https://www.bilibili.com/video/{bvid}','title':video.get('title',bvid),'published':stamp})
                            if oldest<cutoff or len(videos)<30:break
                            if page==10:result['capped']=True
                    finally:client.session.close()
                count=0
                for item in sorted(items,key=lambda i:i['published']):
                    if item['published']<cutoff:continue
                    if day and item['published']>=start+86400:continue
                    if count>=self.settings.get('pipeline','per_source_limit',20):
                        if not self.store.query('SELECT id FROM tasks WHERE url=?',(item['url'],)):result['capped']=True
                        continue
                    if self.enqueue(source['kind'],item,source['id']):count+=1
                self.store.execute('UPDATE sources SET last_poll=?,error=? WHERE id=?',(now if day is None else source['last_poll'],'',source['id']))
                log(f'来源 {source["id"]}：检查完成，获取 {len(items)} 条候选，新增入队 {count} 条，耗时 {time.perf_counter()-started:.1f} 秒')
            except Exception as exc:
                error=str(exc) if isinstance(exc,BiliSourceError) else type(exc).__name__
                result['errors'].append(source['id'])
                self.store.execute('UPDATE sources SET last_poll=?,error=? WHERE id=?',(now if day is None else source['last_poll'],error,source['id']))
                log(f"来源 {source['id']} 检查失败：{error}；耗时 {time.perf_counter()-started:.1f} 秒，按来源检查间隔等待重试")
        return result

    def publish(self,day,force=False):
        day=normalize_day(day)
        # 只发布成功摘要；单条缺失或损坏不能阻断整轮，也不能把已发布条目挤掉。
        tasks=self.store.query("SELECT * FROM tasks WHERE day=? AND status='done' ORDER BY published,id",(day,))
        if not tasks:return None
        entries=[]
        broken=[]
        for task in tasks:
            try:
                result=json.loads(read_text(task['summary_path']))
                if not isinstance(result,dict) or not isinstance(result.get('bullets'),list):
                    raise ValueError('摘要格式无效')
            except (OSError,TypeError,ValueError):
                broken.append(task['id'])
                continue
            entries.append({'id':task['id'],'title':task['title'],'url':task['url'],'published':task['published'],'topic':result.get('topic'),'bullets':result['bullets'],'source_name':self.source_label(task)})
        if broken:
            log(f'日报 {day}：{len(broken)} 条已完成任务的摘要缺失或损坏，沿用其此前条目，待重建补齐')
        # 未成功（失败、重试中或摘要损坏）的任务继续展示上一次成功条目，日报不因处理失败而缩水。
        last=self.store.latest(day)
        previous=self._previous_entries(last)
        entries=self._merge_previous(entries,previous,{task['id'] for task in self.store.query('SELECT id FROM tasks WHERE day=?',(day,))})
        if len(entries)>len(tasks)-len(broken):
            log(f'日报 {day}：{len(entries)-(len(tasks)-len(broken))} 条未成功任务沿用上一次成功条目')
        if not entries:
            # 没有任何可用条目时不发布空日报，保留上一版（若有）。
            log(f'日报 {day}：没有可用摘要，本次不发布')
            return None
        fingerprint=hashlib.sha256(json.dumps(entries,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        final=is_final(day)
        if last and last['fingerprint']==fingerprint and bool(last['final'])==final and Path(last['path']).is_file():
            if force:log(f'日报 {day}：内容复核完成，与已发布版本一致')
            return last
        # 已存在相同内容且文件完好的版本时只切换活动指针，不再写新文件，避免孤儿文件。
        existing=self.store.version_by_fingerprint(day,fingerprint,final)
        if existing and Path(existing['path']).is_file():
            self.store.set_active(day,existing['id'])
            log(f'日报 {day}：内容回到此前版本，切换活动版本，{len(entries)} 条内容')
            return self.store.latest(day)
        snapshot={'day':day,'updated':time.time(),'final':final,'supplement':bool(last and last['final'] and last['fingerprint']!=fingerprint),'entries':entries}
        path=self.settings.data_dir/'reports'/day/f'{time.time_ns()}.json'
        write_text(path,json.dumps(snapshot,ensure_ascii=False))
        self.store.publish_version(day,path,fingerprint,final,snapshot['updated'])
        log(f'日报 {day}：发布完成，{len(entries)} 条内容，'+('定稿后补充' if snapshot['supplement'] else '已定稿' if final else '持续更新'))
        return self.store.latest(day)

    @staticmethod
    def _previous_entries(last):
        """读取上一次活动版本，用于沿用未成功任务的历史条目。"""
        if not last:return []
        try:
            payload=json.loads(read_text(last['path']))
            entries=payload.get('entries',[])
            return [entry for entry in entries if isinstance(entry,dict) and 'id' in entry]
        except (OSError,ValueError,TypeError):
            return []

    @staticmethod
    def _merge_previous(entries,previous,day_task_ids):
        """未成功任务的旧条目按原顺序保留；已完成任务以最新摘要为准。"""
        current={entry['id'] for entry in entries}
        merged=list(entries)
        for entry in previous:
            if entry['id'] in current:continue
            if entry['id'] not in day_task_ids:continue
            merged.append(entry)
        merged.sort(key=lambda entry:(entry.get('published',0),entry.get('id',0)))
        return merged

    def request_rebuild(self,day):
        day=normalize_day(day)
        return self.store.request_rebuild(day)

    def process_rebuild(self):
        request=self.store.claim_rebuild()
        if request is None:return False
        started=time.perf_counter()
        log(f'日报 {request["day"]}：开始执行重建任务 {request["id"]}')
        try:
            log(f'日报 {request["day"]}：检查启用来源及已有处理产物')
            inspection=self.poll(day=request['day'])
            if self.pipeline is None:
                from orchestration.pipeline import ContentPipeline
                self.pipeline=ContentPipeline(self.settings,self.store)
            tasks=self.store.query('SELECT * FROM tasks WHERE day=?',(request['day'],))
            for task in tasks:
                missing=self.pipeline.repair_needed(task)
                if task['status']!='done' or missing:
                    self.store.execute("UPDATE tasks SET status='queued',available=0,attempts=0,error='' WHERE id=?",(task['id'],))
                    log(f'任务 {task["id"]}：重建检查发现未完成或缺失产物，入队补齐')
                else:log(f'任务 {task["id"]}：处理产物完整，复用已有结果')
            while True:
                task=self.store.claim(day=request['day'])
                if task is None:break
                self.pipeline.run(task)
                self.store.set_meta('heartbeat',time.time())
            incomplete=self.store.query("SELECT id FROM tasks WHERE day=? AND status!='done'",(request['day'],))
            # 允许成功内容更新，失败任务与来源检查不冒充完整成功；publish沿用未成功任务的既有条目。
            published=self.publish(request['day'],force=True)
            if inspection['errors'] or inspection['capped'] or incomplete:
                message=f'来源失败 {len(inspection["errors"])} 个，未完成任务 {len(incomplete)} 个'
                if inspection['capped']:message+='；达到单轮采集上限，请再次重建补齐'
                if published is not None:message+='；已发布内容保留，未成功条目沿用上一次成功版本'
                self.store.execute("UPDATE rebuilds SET status='failed',error=?,updated=? WHERE id=?",(message,time.time(),request['id']))
                log(f'日报 {request["day"]}：重建未全部完成，{message}；已有日报保留')
                return True
            if published is None:
                self.store.execute("UPDATE rebuilds SET status='failed',error=?,updated=? WHERE id=?",('指定日期未发现可发布内容',time.time(),request['id']))
                log(f'日报 {request["day"]}：未发现可发布内容，已有日报保留')
                return True
            self.store.execute("UPDATE rebuilds SET status='done',error='',updated=? WHERE id=?",(time.time(),request['id']))
            log(f'日报 {request["day"]}：重建完成，耗时 {time.perf_counter()-started:.1f} 秒')
        except Exception as exc:
            self.store.execute("UPDATE rebuilds SET status='failed',error=?,updated=? WHERE id=?",(type(exc).__name__,time.time(),request['id']))
            log(f'日报 {request["day"]}：重建失败（{type(exc).__name__}），原日报保留')
        return True

    def report(self,day=None):
        if day is None:
            dates=self.dates()
            if not dates:return None
            day=dates[0]
        row=self.store.latest(day)
        if not row:return None
        try:
            result=json.loads(read_text(row['path']))
        except (OSError,ValueError,TypeError):
            log(f'日报 {day}：活动版本文件不可读，公开页暂无可展示内容')
            return None
        for entry in result.get('entries',[]):
            if 'source_name' not in entry:
                tasks=self.store.query('SELECT * FROM tasks WHERE id=?',(entry['id'],))
                entry['source_name']=self.source_label(tasks[0]) if tasks else '历史来源'
        return result

    def source_label(self,task):
        if task['source_id'] is not None:
            rows=self.store.query('SELECT name FROM sources WHERE id=?',(task['source_id'],))
            return rows[0]['name'] if rows else '已删除的订阅来源'
        return 'B站 · 单独提交' if task['kind']=='bili' else 'RSS · 单独提交'

    def dates(self):
        return [r['day'] for r in self.store.query('SELECT DISTINCT day FROM versions ORDER BY day DESC')]

    def preview(self):
        report=self.report()
        if not report:return {'day':None,'count':0,'titles':[],'updated':None}
        return {key:report[key] for key in ('day','updated','final')}|{'count':len(report['entries']),'titles':[e['title'] for e in report['entries'][:3]]}

    def dashboard(self):
        return {'sources':self.store.sources(),'tasks':self.store.query('SELECT * FROM tasks ORDER BY id DESC LIMIT 100'),'rebuilds':self.store.query('SELECT * FROM rebuilds ORDER BY id DESC LIMIT 20'),'dates':self.dates(),'worker_heartbeat':self.store.meta('heartbeat'),'configured':{'asr':bool(self.settings.asr_key),'llm':bool(self.settings.llm_key)}}

    def source_action(self,source_id,action):
        if action=='delete':self.store.execute('DELETE FROM sources WHERE id=?',(source_id,))
        elif action=='toggle':self.store.execute('UPDATE sources SET enabled=1-enabled WHERE id=?',(source_id,))
        elif action=='refresh':self.store.execute('UPDATE sources SET last_poll=0 WHERE id=?',(source_id,))
        else:raise ValueError('未知操作')

    def retry(self,task_id):self.store.retry(task_id)

    def tick(self):
        self.store.set_meta('heartbeat',time.time())
        rebuilt=self.process_rebuild()
        if rebuilt:self._waiting=False
        self.poll()
        task=self.store.claim()
        if task:
            self._waiting=False
            if self.pipeline is None:
                from orchestration.pipeline import ContentPipeline
                self.pipeline=ContentPipeline(self.settings,self.store)
            self.pipeline.run(task)
        elif not self._waiting:
            queued=len(self.store.query("SELECT id FROM tasks WHERE status='queued'"))
            log(f'等待中：暂无可立即执行的任务，待重试 {queued} 个；将自动检查新任务和到期来源')
            self._waiting=True
        self.publish_dirty()
        # tick仅由持锁Worker执行；此时没有运行中的下载或模型调用。
        if time.time()-self._cleanup_at>=3600:
            from integration.cleanup import cleanup
            result=cleanup(self.settings,self.store.query('SELECT id,status FROM tasks'))
            self.store.prune_events(time.time()-30*86400)
            self._cleanup_at=time.time()
            if result['files']:log(f'空间清理：移除 {result["files"]} 个缓存文件，释放 {result["bytes"]/1048576:.1f} MB')

    def publish_dirty(self,force=False):
        """只重算完成状态变化的日期；逐日隔离异常，单日失败不阻断其他日期。"""
        signature=self.store.done_signature()
        for day in sorted(signature):
            if not force and self._publish_signature.get(day)==signature[day]:continue
            try:
                self.publish(day,force=force)
            except Exception as exc:
                # 保留旧签名，下一轮继续重试该日期，其他日期不受影响。
                log(f'日报 {day}：发布失败（{type(exc).__name__}），其他日期继续')
                continue
            self._publish_signature[day]=signature[day]
        for day in list(self._publish_signature):
            if day not in signature:
                self._publish_signature.pop(day,None)
