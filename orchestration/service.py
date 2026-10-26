"""交互唯一门面：订阅、任务、发布、管理。"""
import hashlib
import json
import time
from contextlib import contextmanager
from threading import Event, Thread, Lock
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from pathlib import Path
from integration.store import Store
from integration.files import read_text, write_text
from integration.sources.bili import BiliVideoSource, BiliSourceError, extract_bvid_from_url, extract_mid_from_url
from integration.sources.rss import discover
from integration.http.public import public_url
from integration.auth import Sessions, verify_password
from integration.runtime_log import log
from orchestration.runtime import RuntimeState
from orchestration.status import stage_label, task_state, rebuild_state, explain_error, topic_order, topic_name

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

# 同题包含匹配的最小键长：两字主体（游戏、Kimi）参与包含判断会大面积误判。
SUBJECT_MIN_LENGTH=6


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
        self.runtime=RuntimeState()
        self._last_heartbeat=0.0
        self._config_error_logged=''
        # 处理中检测到的配置变更只登记，任务结束后由下一轮 tick 应用。
        self._config_deferred=False

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
        self._ensure_pipeline()
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

    def _touch_heartbeat(self, force=False):
        """长任务内也保持心跳新鲜；写库按两秒节流，页面实时状态走内存。"""
        now=time.time()
        if force or now-self._last_heartbeat>=2:
            self.store.set_meta('heartbeat',now)
            self._last_heartbeat=now

    def refresh_config(self, force=False):
        """在任务之间应用[llm]变更；同一任务内不混用两个模型。"""
        activity=self.runtime.snapshot()
        if activity['kind'] in ('task','rebuild'):
            # 正在处理时只标记待应用，避免摘要分片和缓存指纹跨模型。
            if self.settings.llm_state()['pending'] or force:
                self._config_deferred=True
            return False
        return self._apply_config(force=force)

    def _pipeline_progress(self,task_id,stage,detail,current=None,total=None):
        """由流水线回调：任务号在途时只更新当前任务的进度，避免串到其他任务。"""
        activity=self.runtime.snapshot()
        if task_id is not None and activity.get('task_id') not in (None,task_id):return
        self.runtime.update(stage=stage,detail=detail,current=current,total=total)
        self._touch_heartbeat()

    def _ensure_pipeline(self):
        if self.pipeline is None:
            from orchestration.pipeline import ContentPipeline
            self.pipeline=ContentPipeline(self.settings,self.store,progress=self._pipeline_progress)
        return self.pipeline

    def _run_task(self,task,parent=''):
        """串行执行一个任务；活动状态只在持锁Worker线程内切换。

        重建内的任务保留“重建日报”为主标签，任务号与标题作为细节，
        这样刷新时不会在两种活动之间跳动。
        """
        if parent=='rebuild':
            self.runtime.begin('rebuild',f'重建日报 {task["day"]}',day=task['day'],stage='process',
                               detail='处理任务',parent='rebuild',
                               task_id=task['id'],title=task['title'])
        else:
            self.runtime.begin('task',f'处理任务 #{task["id"]}',detail='准备开始',
                               task_id=task['id'],title=task['title'],day=task['day'],parent=parent)
        self._touch_heartbeat(force=True)
        try:
            self._ensure_pipeline().run(task)
        finally:
            if parent=='rebuild':
                self.runtime.begin('rebuild',f'重建日报 {task["day"]}',day=task['day'],stage='process',
                                   detail='任务结束，继续补齐',parent='rebuild')
            else:
                self.runtime.finish('空闲','等待下一轮调度')
            self._touch_heartbeat(force=True)

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
        self.runtime.start()
        self._touch_heartbeat(force=True)
        try:
            with worker_lock(self.settings.data_dir/'worker.lock'):
                self.check_media_tools()
                self._ensure_pipeline()
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
        finally:
            self.runtime.stop()

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

    def configure_models(self,llm_key='',llm_base_url='',llm_model='',asr_key='',asr_model='',asr_base_url=''):
        """管理页保存模型与密钥：只写 config.toml 的 [llm]/[asr]，密钥不回显、不落库。

        空值表示“保持不变”，这样只改模型名时不必重新输入密钥。文本模型保存后
        立即热加载（处理中则登记待应用）；转写配置在调用时读取，保存即生效。
        """
        from config import parse_https_url, parse_llm_config
        from integration.config_file import save_values
        llm_key=llm_key.strip();llm_base_url=llm_base_url.strip()
        llm_model=llm_model.strip();asr_key=asr_key.strip();asr_model=asr_model.strip()
        asr_base_url=asr_base_url.strip()
        llm_patch={}
        if llm_base_url or llm_model:
            candidate=dict(self.settings.values.get('llm') or {})
            if llm_base_url:candidate['base_url']=llm_base_url
            if llm_model:candidate['model']=llm_model
            llm=parse_llm_config({'llm':candidate})
            if llm_base_url:llm_patch['base_url']=llm['base_url']
            if llm_model:llm_patch['model']=llm['model']
        if llm_key:llm_patch['api_key']=llm_key
        if llm_patch:save_values(self.settings.config_file,'llm',llm_patch)
        asr_patch={}
        if asr_model:
            if len(asr_model)>200:raise ValueError('转写模型名称过长')
            asr_patch['model']=asr_model
        if asr_base_url:asr_patch['base_url']=parse_https_url('asr.base_url',asr_base_url)
        if asr_key:asr_patch['api_key']=asr_key
        if not llm_patch and not asr_patch:raise ValueError('没有需要保存的修改')
        if asr_patch:save_values(self.settings.config_file,'asr',asr_patch)
        if asr_patch:
            # 转写配置在每次调用时读取，写入后同步内存值即可生效，无需重启。
            if 'api_key' in asr_patch:self.settings.asr_key=asr_patch['api_key']
            if 'model' in asr_patch:self.settings.values.setdefault('asr',{})['model']=asr_patch['model']
            if 'base_url' in asr_patch:
                self.settings.asr_base_url=asr_patch['base_url']
                self.settings.values.setdefault('asr',{})['base_url']=asr_patch['base_url']
        changed,state,deferred=False,self.settings.llm_state(),False
        if llm_patch:changed,state,deferred=self.reload_model_config()
        return {'llm':state,'deferred':deferred,'llm_saved':bool(llm_patch),'asr_saved':bool(asr_patch)}

    def enqueue(self,kind,item,source_id=None):
        stamp=float(item['published'])
        if stamp>time.time()+86400:raise ValueError('来源时间异常')
        item=dict(item,kind=kind,source_id=source_id,day=day_of(stamp))
        inserted=self.store.enqueue(item)
        if inserted and item.get('body'):
            row=self.store.query('SELECT id FROM tasks WHERE url=?',(item['url'],))[0]
            write_text(self.settings.content_dir/str(row['id'])/'feed.txt',item['body'])
        return inserted

    def poll(self,day=None,end_day=None,parent=''):
        """检查来源；day与end_day同时给出时按日期区间收集，供范围重建一次扫完。

        逐日调用会让每个来源被重复扫描N遍，区间模式把候选一次取回后按天归入，
        既省请求又避免B站分页上限被多日重复消耗。
        """
        now=time.time()
        result={'errors':[],'capped':False}
        start=datetime.strptime(normalize_day(day),'%Y-%m-%d').replace(tzinfo=TZ).timestamp() if day else None
        end=None
        if start is not None and end_day:
            end_exclusive=datetime.strptime(normalize_day(end_day),'%Y-%m-%d').replace(tzinfo=TZ).timestamp()+86400
            end=end_exclusive if end_exclusive>start else start+86400
        interval=self.settings.get('pipeline','poll_seconds',3600)
        pending=[source for source in self.store.sources()
                 if source['enabled'] and not (day is None and now-source['last_poll']<interval)]
        if pending and parent!='rebuild':
            # 普通轮次的来源检查自成一个活动；重建内的检查归属重建，避免状态串台。
            self.runtime.begin('poll','检查订阅来源',day=day,stage='acquire',detail='读取启用来源')
        for index,source in enumerate(pending,1):
            self.runtime.update(detail=f'检查来源 {index}/{len(pending)}：{source["name"]}',current=index,total=len(pending))
            self._touch_heartbeat()
            started=time.perf_counter()
            log(f'来源 {source["id"]}（{source["kind"]}）：开始检查新增内容')
            try:
                # 区间模式下回溯到区间起点，不再受首次24小时限制。
                cutoff=start if start is not None else source['created']-self.settings.get('pipeline','lookback_hours',24)*3600
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
                    if end is not None and item['published']>=end:continue
                    if end is None and start is not None and item['published']>=start+86400:continue
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

    def publish(self,day,force=False,allow_empty=False):
        day=normalize_day(day)
        # 只发布成功摘要；单条缺失或损坏不能阻断整轮，也不能把已发布条目挤掉。
        tasks=self.store.query("SELECT * FROM tasks WHERE day=? AND status='done' ORDER BY published,id",(day,))
        if not tasks and not allow_empty:return None
        # 上一条活动版本要在构造条目前读取：来源删除后重新发布时沿用其中记录的
        # 来源名快照，否则同一篇历史内容会降级成“已删除的订阅来源”并被新版本固化。
        last=self.store.latest(day)
        previous=self._previous_entries(last)
        historical={entry['id']:entry['source_name'] for entry in previous
                    if entry.get('source_name') and isinstance(entry['source_name'],str)}
        entries=[]
        broken=[]
        for task in tasks:
            try:
                payload=json.loads(read_text(task['summary_path']))
                # 磁盘结构是 {"events":[...]}；裸数组与旧单条 dict 也要能读，
                # 否则升级当刻的旧摘要会被整批判成损坏。
                if isinstance(payload,dict) and isinstance(payload.get('events'),list):
                    events=payload['events']
                elif isinstance(payload,list):
                    events=payload
                else:
                    events=[payload]
                if not events or not all(isinstance(e,dict) and isinstance(e.get('bullets'),list) for e in events):
                    raise ValueError('摘要格式无效')
            except (OSError,TypeError,ValueError):
                broken.append(task['id'])
                continue
            source_name=self._entry_source_name(task,historical)
            for index,event in enumerate(events):
                # 一个任务可以派生多条事件：id 用 task:index，锚点与撤回都按任务回指。
                entries.append({'id':f"{task['id']}:{index}" if len(events)>1 else task['id'],
                                'task_id':task['id'],
                                'title':self._entry_title(task,event),
                                'url':self._entry_url(task,event),
                                'published':task['published'],
                                'topic':event.get('topic'),
                                'bullets':event['bullets'],
                                'subject':event.get('subject') or '',
                                'source_name':source_name})
        if broken:
            log(f'日报 {day}：{len(broken)} 条已完成任务的摘要缺失或损坏，沿用其此前条目，待重建补齐')
        # 未成功（失败、重试中或摘要损坏）的任务继续展示上一次成功条目，日报不因处理失败而缩水。
        entries=self._merge_previous(entries,previous,{task['id'] for task in self.store.query('SELECT id FROM tasks WHERE day=?',(day,))})
        # 撤回是发布层的否决，必须放在沿用旧条目之后，否则旧版本会把被撤内容带回来。
        # 撤回粒度是任务：一个任务派生的事件一起撤，不允许只撤其中几条，
        # 否则同一来源的事件会处在“撤了一半”的不一致状态。
        retracted=self.store.retracted_ids(day)
        if retracted:
            before=len(entries)
            entries=[entry for entry in entries if entry.get('task_id',entry['id']) not in retracted]
            if len(entries)<before:log(f'日报 {day}：{before-len(entries)} 条已撤回条目未包含在本次发布')
        # 统计沿用了几条历史条目。一个任务可派生多条事件，条目数不再等于任务数，
        # 所以按任务归属算：本次没有产出的任务，其旧条目才算“沿用”。
        current_tasks={task['id'] for task in tasks}
        carried=[]
        for entry in entries:
            owner=entry.get('task_id',entry['id'])
            if isinstance(owner,str) and ':' in owner:owner=owner.split(':',1)[0]
            try:owner=int(owner)
            except (TypeError,ValueError):owner=None
            if owner not in current_tasks:carried.append(entry)
        if carried:
            log(f'日报 {day}：{len(carried)} 条未成功任务的条目沿用上一次成功版本')
        if not entries:
            if not allow_empty:
                # 没有任何可用条目时不发布空日报，保留上一版（若有）。
                log(f'日报 {day}：没有可用摘要，本次不发布')
                return None
            # 撤回全部内容：不写空快照，只移除活动指针，归档导航随之不再列出该日。
            self.store.clear_active(day)
            log(f'日报 {day}：全部条目已撤回，该日不再展示；历史版本与产物保留')
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
    def _entry_title(task,event):
        """标题优先用摘要里的模型标题：来源原始标题常是口语化、标题党或与正文不符。

        历史摘要没有该字段，回退到任务保存的来源标题，保证旧日期不改写。
        """
        title=event.get('title') if isinstance(event,dict) else None
        if isinstance(title,str) and title.strip():return title.strip()
        return task['title']

    @staticmethod
    def _entry_url(task,event):
        """事件自己的原文链接优先，没有就回退到该来源主链接。

        链接已在解析层过了白名单，这里只做兜底：单条视频或纯文本转写拆出的
        事件没有各自的链接，只能指回来源本身。
        """
        url=event.get('url') if isinstance(event,dict) else None
        if isinstance(url,str) and url.startswith(('http://','https://')):return url
        return task['url']

    def _entry_source_name(self,task,historical):
        """来源名优先沿用该任务已发布过的快照。

        来源被删除或改名后重新发布时，若重新推导就会把历史内容标成
        “已删除的订阅来源”或新名字，等于改写历史。已发布过的名字更忠实；
        只有从未发布过（新任务）或旧版本缺该字段时才回退推导。
        """
        published_name=historical.get(task['id'])
        if published_name:return published_name
        return self.source_label(task)

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
        """未成功任务的旧条目按原顺序保留；已完成任务以最新摘要为准。

        条目 id 可能是 task:index，也可能是旧的纯 task；两种都要能识别归属，
        否则拆分上线后旧快照里的条目会被当成陌生 id 而丢失。
        """
        def owner(entry):
            value=entry.get('task_id',entry.get('id'))
            if isinstance(value,str) and ':' in value:value=value.split(':',1)[0]
            try:return int(value)
            except (TypeError,ValueError):return None

        current={owner(entry) for entry in entries}
        merged=list(entries)
        for entry in previous:
            task=owner(entry)
            if task in current:continue
            if task not in day_task_ids:continue
            merged.append(entry)
        merged.sort(key=lambda entry:(entry.get('published',0),str(entry.get('id',0))))
        return merged

    def retract_entry(self,task_id,reason=''):
        """撤回单条已发布条目：只重算该日，不删除摘要、版本或任务行。

        重复撤回按幂等处理：唯一一条被撤回后该日已无活动版本，此时再提交
        同一条不应报错（否则刷新页面重发表单就会看到 409）。
        """
        task=self.store.task(task_id)
        if task['status']=='canceled':
            raise ValueError('该任务已因来源删除而取消，无需撤回')
        if task['status']!='done':
            raise ValueError('只能撤回已处理完成的条目')
        already=task_id in self.store.retracted_ids(task['day'])
        if not already:
            if not self.store.latest(task['day']) and not self.store.versions(task['day']):
                raise ValueError('该日期没有已发布日报')
            self.store.retract(task_id,task['day'],reason)
            self.store.event(task_id,'retract','ok',reason or '撤回条目')
            log(f'条目 {task_id}：已撤回（{task["day"]}）')
        return self.publish(task['day'],force=True,allow_empty=True)

    def restore_entry(self,task_id):
        """恢复已撤回条目；只会让日报增加内容，无需空日报例外。"""
        task=self.store.task(task_id)
        if not self.store.restore(task_id):
            raise ValueError('该条目未被撤回')
        self.store.event(task_id,'retract','restored','恢复条目')
        log(f'条目 {task_id}：已恢复（{task["day"]}）')
        return self.publish(task['day'],force=True)

    def request_rebuild(self,day):
        day=normalize_day(day)
        return self.store.request_rebuild(day)

    def request_rebuild_range(self,days):
        """重建最近若干天；按日展开并入同一批次。

        只入队“有内容或已发布”的日期：完全空白的日期没有可核对产物，
        入队只会产生一串必然失败的记录。
        """
        count=int(days)
        if not 1<=count<=30:raise ValueError('重建天数需在 1 至 30 之间')
        today=datetime.now(TZ).date()
        candidates=[(today-timedelta(days=offset)).isoformat() for offset in range(count)]
        known=self.store.days_with_content(candidates)
        if not known:return []
        batch=f'range-{int(time.time())}'
        return self.store.request_rebuild_range(known,batch)

    def request_rebuild_week(self):
        """重建最近7天（含今天）的便捷入口。"""
        return self.request_rebuild_range(7)

    def _rebuild_inspection(self,request,day):
        """范围重建时整批只扫一次来源，逐日重建仍按单日扫描。

        逐日调用poll会把每个来源重复扫描N遍，既浪费请求，
        也可能让B站分页上限被多日重复消耗。用meta记录批次已扫描的区间，
        后续同批次任务直接复用一个空结果，不再触发来源请求。
        """
        batch=request.get('batch') or ''
        if not batch:
            return self.poll(day=day,parent='rebuild')
        key=f'rebuild_scan:{batch}'
        if self.store.meta(key):
            return {'errors':[],'capped':False,'reused':True}
        remaining=self.store.query("SELECT day FROM rebuilds WHERE batch=? AND status IN ('queued','running') "
                                   'ORDER BY day',(batch,))
        days=[row['day'] for row in remaining]
        if not days:days=[day]
        start,end=min(days),max(days)
        log(f'批量重建：一次检查 {start} 至 {end} 的来源内容（共 {len(days)} 天）')
        result=self.poll(day=start,end_day=end,parent='rebuild')
        self.store.set_meta(key,str(time.time()))
        return result

    def process_rebuild(self):
        request=self.store.claim_rebuild()
        if request is None:return False
        started=time.perf_counter()
        day=request['day']
        self.runtime.begin('rebuild',f'重建日报 {day}',day=day,stage='acquire',detail='检查启用来源')
        self._touch_heartbeat(force=True)
        log(f'日报 {day}：开始执行重建任务 {request["id"]}')
        try:
            log(f'日报 {day}：检查启用来源及已有处理产物')
            inspection=self._rebuild_inspection(request,day)
            self._ensure_pipeline()
            self.runtime.update(stage='check',detail='核对已有处理产物',current=None,total=None)
            tasks=self.store.query('SELECT * FROM tasks WHERE day=?',(day,))
            pending=[]
            orphaned=0
            for index,task in enumerate(tasks,1):
                self._touch_heartbeat()
                self.runtime.update(detail=f'核对产物 {index}/{len(tasks)}',current=index,total=len(tasks))
                if task['source_id'] is not None and not self.store.source_exists(task['source_id']):
                    # 来源已删除：不复活、不重抓，保持取消状态并计入未完成说明。
                    if task['status']!='canceled':
                        self.store.execute("UPDATE tasks SET status='canceled',available=0,"
                                           "error='source: SourceDeleted',updated=? WHERE id=?",
                                           (time.time(),task['id']))
                    orphaned+=1
                    log(f'任务 {task["id"]}：来源已删除，跳过重建')
                    continue
                missing=self.pipeline.repair_needed(task)
                if task['status'] not in ('done','canceled') or missing:
                    # 重建只重置本轮等待时间与错误，不重置累计尝试次数：
                    # 否则每次重建都等于给失败任务一份全新的自动重试额度。
                    total_limit=self.settings.get('worker','max_total_attempts',10)
                    if task['status']=='failed' and task['attempts']>=total_limit:
                        log(f'任务 {task["id"]}：已达终身尝试上限（{total_limit} 次），重建跳过')
                        continue
                    self.store.execute("UPDATE tasks SET status='queued',available=0,error='' WHERE id=?",(task['id'],))
                    pending.append(task['id'])
                    log(f'任务 {task["id"]}：重建检查发现未完成或缺失产物，入队补齐')
                else:log(f'任务 {task["id"]}：处理产物完整，复用已有结果')
            self.runtime.update(stage='process',detail=f'待补齐 {len(pending)} 个任务',current=0,total=len(pending) or None)
            done=0
            while True:
                task=self.store.claim(day=day)
                if task is None:break
                done+=1
                # _run_task 会以同一标签重建活动并带上当前任务号，进度不因切换而丢失。
                self._run_task(task,parent='rebuild')
                self.runtime.update(stage='process',detail=f'已处理 {done}/{len(pending) or done} 个任务',
                                    current=done,total=len(pending) or done)
            self.runtime.begin('rebuild',f'重建日报 {day}',day=day,stage='finish',detail='汇集并发布日报')
            self._touch_heartbeat(force=True)
            incomplete=self.store.query("SELECT id FROM tasks WHERE day=? AND status NOT IN ('done','canceled')",(day,))
            # 允许成功内容更新，失败任务与来源检查不冒充完整成功；publish沿用未成功任务的既有条目。
            published=self.publish(day,force=True)
            if inspection['errors'] or inspection['capped'] or incomplete:
                message=f'来源失败 {len(inspection["errors"])} 个，未完成任务 {len(incomplete)} 个'
                if orphaned:message+=f'；{orphaned} 个任务因来源已删除被跳过'
                if inspection['capped']:message+='；达到单轮采集上限，请再次重建补齐'
                if published is not None:message+='；已发布内容保留，未成功条目沿用上一次成功版本'
                self.store.finish_rebuild(request['id'],'failed',message)
                log(f'日报 {day}：重建未全部完成，{message}；已有日报保留')
                return True
            if published is None:
                message='指定日期未发现可发布内容'
                if orphaned:message+=f'；{orphaned} 个任务因来源已删除被跳过'
                self.store.finish_rebuild(request['id'],'failed',message)
                log(f'日报 {day}：{message}，已有日报保留')
                return True
            self.store.finish_rebuild(request['id'],'done','')
            log(f'日报 {day}：重建完成，耗时 {time.perf_counter()-started:.1f} 秒')
        except Exception as exc:
            self.store.finish_rebuild(request['id'],'failed',type(exc).__name__)
            log(f'日报 {day}：重建失败（{type(exc).__name__}），原日报保留')
        finally:
            # 批次最后一目完成后才退回扫描标记，避免下一目重复扫描来源。
            batch=request.get('batch') or ''
            if batch:
                left=self.store.query("SELECT id FROM rebuilds WHERE batch=? AND status IN ('queued','running')",(batch,))
                if not left:
                    self.store.execute("DELETE FROM meta WHERE key=?",(f'rebuild_scan:{batch}',))
            self.runtime.finish('空闲','等待下一轮调度')
            self._touch_heartbeat(force=True)
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
        """只列有活动版本的日期；无活动版本的日期不再出现在归档导航，避免死链。"""
        return [r['day'] for r in self.store.query(
            'SELECT DISTINCT day FROM versions WHERE active=1 ORDER BY day DESC')]

    def briefing(self,day=None):
        """公开日报页的唯一数据入口：分组、统计、翻页、归档一次算清。

        分组不能放在模板里：Jinja 的 groupby 会先按 key 排序，条目缺
        topic（None）时与字符串比较直接抛 TypeError，整页 500。
        """
        dates=self.dates()
        latest=dates[0] if dates else None
        target=day or latest
        report=self.report(target) if target else None
        return {'report':report,
                'sections':self.sections(report,self.settings.topic_order),
                'stats':self.report_stats(report,dates,self.settings.topic_order),
                'neighbors':self.neighbors(target,dates),
                'archive':self._archive(dates,target if report else latest),
                'dates':dates,'latest':latest,'today':day_of(time.time())}

    @staticmethod
    def sections(report,topics=None):
        """按词表分节；缺失或未知主题归入“其他”，空节不出现，顺序即编辑顺序。

        同时标注同题：同一事件常被多个来源分别报道（B站视频版与文字早报），
        合并会丢内容，所以只在条目上标出“另有 N 个来源报道同一事件”，
        由读者自己决定看哪一条。
        """
        order=topic_order(topics)
        buckets={topic:[] for topic in order}
        entries=[entry for entry in (report or {}).get('entries',[]) if isinstance(entry,dict)]
        for entry in entries:
            buckets[topic_name(entry.get('topic'),topics)].append(entry)
        # 同一事件在不同来源的写法常是“主体”与“主体+动作”：
        # 「Kimi K3.1 Pro」与「Kimi K3.1 Pro 发布」指的是同一件事。
        # 精确相等会漏掉这种，所以按“一方包含另一方”成对归组；
        # 条目数很小（一天几十条），直接两两比较比维护并查集更直白。
        keys=[DailyService._subject_key(entry.get('subject')) for entry in entries]

        def same_event(left,right):
            if not left or not right:return False
            if left==right:return True
            # 包含匹配只对足够长的键成立：'游戏'、'Kimi' 这种过短主体一旦
            # 参与包含判断，会把「游戏引擎更新」「Kimi 登月计划」全判成同题。
            if len(left)<SUBJECT_MIN_LENGTH or len(right)<SUBJECT_MIN_LENGTH:return False
            return left in right or right in left

        counts=[0]*len(keys)
        for i,key in enumerate(keys):
            for j,other in enumerate(keys):
                if same_event(key,other):counts[i]+=1
        for entry,repeat in zip(entries,counts):
            # 只在多来源重复时给提示；单条事件不需要“唯一”这种噪音。
            if repeat>1:entry['repeat']=repeat
        # 分组视图：同题条目收进同一个块，块标题写明有几个来源。
        # 页面据此直接呈现“这几条讲的是同一件事”，不必让读者逐条比标记。
        # 归一化键不同但互为包含的（「Kimi K3.1 Pro」与「Kimi K3.1 Pro 发布」）
        # 必须聚成一组，所以按“与组内任一条同题”归并，而不是按键相等。
        # 单条事件各自成组，模板用同一套循环渲染，不需要分支。
        clusters=[]
        groups=[]
        for entry,key,repeat in zip(entries,keys,counts):
            if repeat<=1 or not key:
                groups.append({'label':'','size':1,'entries':[entry]})
                continue
            target=None
            for group in clusters:
                if any(same_event(key,other) for other in group['keys']):
                    target=group;break
            if target is None:
                target={'entries':[],'keys':set()}
                clusters.append(target)
                groups.append(target)
            target['entries'].append(entry)
            target['keys'].add(key)
        for group in groups:
            group['size']=len(group['entries'])
            group.pop('keys',None)
            group['label']=f'同一事件 · {group["size"]} 个来源' if group['size']>1 else ''
        # 每组只挂在本组首条所属的节下，跨节同名不会被拆到两处。
        by_topic={topic:[] for topic in order}
        for group in groups:
            topic=topic_name(group['entries'][0].get('topic'),topics)
            by_topic[topic].append(group)
        return [{'topic':topic,'entries':buckets[topic],'groups':by_topic[topic]}
                for topic in order if buckets[topic]]

    @staticmethod
    def _subject_key(subject):
        """同题比较用的归一化键：忽略空白、大小写与首尾标点。"""
        if not isinstance(subject,str):return ''
        value=''.join(subject.split()).lower()
        return value.strip('，,。.、:：?!！?')

    @staticmethod
    def report_stats(report,dates=None,topics=None):
        entries=(report or {}).get('entries',[]) or []
        stamps=[float(entry['published']) for entry in entries
                if isinstance(entry.get('published'),(int,float)) and entry['published']>0]
        return {'entries':len(entries),
                'sources':len({entry.get('source_name') for entry in entries if entry.get('source_name')}),
                'topics':len({topic_name(entry.get('topic'),topics) for entry in entries}),
                'first':min(stamps) if stamps else None,
                'last':max(stamps) if stamps else None,
                'archive_days':len(dates or [])}

    @staticmethod
    def neighbors(day,dates=None):
        """归档日期是倒序的：prev 指更早一天，next 指更新一天。"""
        values=list(dates or [])
        if not day or day not in values:return {'prev':None,'next':None}
        index=values.index(day)
        return {'prev':values[index+1] if index+1<len(values) else None,
                'next':values[index-1] if index>0 else None}

    def archive(self,active_day=None):
        return self._archive(self.dates(),active_day)

    @staticmethod
    def _archive(dates,active_day=None):
        """把归档日期组织成年→月两层，供侧栏折叠展示。

        只读 dates()，不改公开 /api/public/days 的扁平数组契约。
        当前查看日期所在的年与月标记为展开，其余默认收起，
        这样归档多时侧栏不会一次铺开成几十行。
        """
        years={}
        for day in dates:
            years.setdefault(day[:4],{}).setdefault(day[5:7],[]).append(day)
        tree=[]
        for year in sorted(years,reverse=True):
            months=[]
            for month in sorted(years[year],reverse=True):
                days=years[year][month]
                months.append({'key':f'{year}-{month}','month':month,'days':days,'count':len(days),
                               'open':bool(active_day and active_day[:7]==f'{year}-{month}')})
            tree.append({'year':year,'months':months,'count':sum(item['count'] for item in months),
                         'open':bool(active_day and active_day[:4]==year)})
        return tree

    def preview(self):
        report=self.report()
        if not report:return {'day':None,'count':0,'titles':[],'updated':None}
        return {key:report[key] for key in ('day','updated','final')}|{'count':len(report['entries']),'titles':[e['title'] for e in report['entries'][:3]]}

    TASK_PAGE_SIZE=50

    def dashboard(self, view='all', day=None, events_page=1, tasks_page=1):
        """管理页唯一数据来源；不含凭据，不触发采集、模型调用或配置变更。"""
        counts=self.store.task_counts()
        size=self.TASK_PAGE_SIZE
        total_tasks=self.store.count_tasks(view,day)
        task_pages=max(1,(total_tasks+size-1)//size)
        try:task_page=int(tasks_page)
        except (TypeError,ValueError):task_page=1
        task_page=min(max(1,task_page),task_pages)
        tasks=self.store.dashboard_tasks(view,day,size,(task_page-1)*size)
        now=time.time()
        total_limit=self.settings.get('worker','max_total_attempts',10)
        # 一条任务可能派生多条日报条目（汇总类来源），列表里标出数量，
        # 便于判断某个来源到底贡献了几件事、撤回会牵连多少条。
        published_counts={}
        # 按列表里出现的日期逐个取最新版本：默认视图不传 day，只看一天会算成 0。
        for task_day in {task.get('day') for task in tasks if task.get('day')}:
            latest=self.store.latest(task_day)
            if not latest:continue
            for entry in self._previous_entries(latest):
                owner=entry.get('task_id',entry.get('id'))
                if isinstance(owner,str) and ':' in owner:owner=owner.split(':',1)[0]
                try:owner=int(owner)
                except (TypeError,ValueError):continue
                published_counts[owner]=published_counts.get(owner,0)+1
        for task in tasks:
            task['entry_count']=published_counts.get(task['id'],0)
            task['exhausted']=task['status']=='failed' and task['attempts']>=total_limit
            task.update(task_state(task,now))
            task['error_text']=explain_error(task.get('error'))
            task['warning_text']=task.get('warning') or ''
            task['stage_text']=stage_label(task.get('stage'),task.get('kind'))
        rebuilds=self.store.rebuilds(20)
        progress=self.store.day_progress([job['day'] for job in rebuilds])
        for job in rebuilds:
            job.update(rebuild_state(job,now))
            job['error_text']=explain_error(job.get('error'))
            total,done=progress.get(job['day'],(0,0))
            job['total'],job['done']=total,done
            job['progress']=round(100*done/total) if total else None
        events=self.event_log(events_page)
        return {'sources':self.store.sources(),'tasks':tasks,'task_counts':counts,'view':view,'day_filter':day or '',
                'rebuilds':rebuilds,'active_rebuilds':self.store.active_rebuilds(),
                'events':events['groups'],'events_page':events['page'],'events_pages':events['pages'],
                'events_total':events['total'],'dates':self.dates(),
                'tasks_page':task_page,'tasks_pages':task_pages,'tasks_total':total_tasks,
                'worker_heartbeat':self.store.meta('heartbeat'),'activity':self.runtime.snapshot(),
                'configured':{'asr':bool(self.settings.asr_key),'llm':bool(self.settings.llm_key)},
                # 地址与模型名按用户要求完整回显；密钥始终只回“是否配置”。
                'llm':{**self.settings.llm_state(),'base_url':self.settings.llm_base_url},
                'asr':{'configured':bool(self.settings.asr_key),'model':self.settings.get('asr','model',''),
                       'host':urlsplit(self.settings.asr_base_url).hostname or '',
                       'base_url':self.settings.asr_base_url},
                'storage':self.storage_state(),'now':now}

    EVENT_PAGE_SIZE=20
    # 事件记录双上限：按天保证长期静默也能清干净，按行保证活跃时不失控。
    EVENT_RETENTION_DAYS=30
    EVENT_MAX_ROWS=20000

    def event_log(self, page=1):
        """阶段记录分页并按任务聚合，一次处理显示为一条时间线。

        事件按“阶段开始/结束”成对写入，逐行铺开既长又重复；这里把同一任务
        聚合成一轮，行内只保留阶段与耗时，详情收进可展开的说明。
        """
        size=self.EVENT_PAGE_SIZE
        total=self.store.count_events()
        pages=max(1,(total+size-1)//size)
        try:page=int(page)
        except (TypeError,ValueError):page=1
        page=min(max(1,page),pages)
        rows=self.store.recent_events(size,(page-1)*size)
        groups=[]
        index={}
        for row in reversed(rows):
            key=row['task_id']
            group=index.get(key)
            if group is None:
                group={'task_id':key,'title':row['title'],'stages':[],'latest':row['created'],
                       'failed':False,'running':False}
                index[key]=group
                groups.append(group)
            label=stage_label(row['stage'],row.get('kind'))
            status=row['status']
            if status=='started':
                group['stages'].append({'label':label,'state':'run','detail':'','at':row['created']})
                group['running']=True
            else:
                target=None
                for stage in reversed(group['stages']):
                    if stage['label']==label and stage['state']=='run':target=stage;break
                if target is None:
                    target={'label':label,'state':'ok','detail':'','at':row['created']}
                    group['stages'].append(target)
                target['state']='ok' if status=='ok' else 'danger'
                target['detail']=row['detail'] or ''
                target['at']=row['created']
                if status!='ok':group['failed']=True
            group['latest']=max(group['latest'],row['created'])
        for group in groups:
            group['running']=any(stage['state']=='run' for stage in group['stages'])
            group['state']='danger' if group['failed'] else ('run' if group['running'] else 'ok')
            group['state_label']='失败' if group['failed'] else ('处理中' if group['running'] else '完成')
        groups.sort(key=lambda item:(item['running'],item['latest']),reverse=True)
        return {'groups':groups,'page':page,'pages':pages,'total':total}

    def activity_status(self):
        """轻量实时状态；供管理页高频轮询，不读取任务列表或日报。"""
        snapshot=self.runtime.snapshot()
        snapshot['task_counts']=self.store.task_counts()
        active=self.store.active_rebuilds()
        progress=self.store.day_progress([job['day'] for job in active])
        snapshot['active_rebuilds']=[]
        for job in active:
            item={**job,**rebuild_state(job)}
            total,done=progress.get(job['day'],(0,0))
            item['total'],item['done']=total,done
            item['progress']=round(100*done/total) if total else None
            snapshot['active_rebuilds'].append(item)
        snapshot['worker_heartbeat']=self.store.meta('heartbeat')
        snapshot['llm']=self.settings.llm_state()
        snapshot['now']=time.time()
        return snapshot

    def reload_model_config(self):
        """管理页手动触发模型配置重载；处理中只登记待应用，失败保留旧配置。"""
        activity=self.runtime.snapshot()
        if activity['kind'] in ('task','rebuild'):
            self._config_deferred=True
            log('文本模型配置重载已登记：当前任务结束后应用，避免同一份摘要混用两个模型')
            return False,self.settings.llm_state(),True
        changed,state=self._apply_config(force=True)
        return changed,state,False

    def _apply_config(self, force=False):
        before=self.settings.llm_revision
        changed=self.settings.refresh_llm(force=force)
        state=self.settings.llm_state()
        self._config_deferred=bool(state.get('pending'))
        if changed and state['revision']!=before:
            log(f'文本模型配置已热加载：版本 {state["revision"]}，模型 {state["model"]}')
        elif state['error'] and state['error']!=self._config_error_logged:
            log(f'文本模型配置热加载失败：{state["error"]}；继续使用上一版配置')
        self._config_error_logged=state['error'] if state['error'] else ''
        return changed,state

    def source_action(self,source_id,action):
        if action=='delete':
            self.store.delete_source(source_id)
            log(f'来源 {source_id}：已删除，其未处理任务已取消；已发布日报保留')
        elif action=='toggle':self.store.execute('UPDATE sources SET enabled=1-enabled WHERE id=?',(source_id,))
        elif action=='refresh':self.store.execute('UPDATE sources SET last_poll=0 WHERE id=?',(source_id,))
        else:raise ValueError('未知操作')

    def retry(self,task_id):
        """手动重试；达到终身上限的任务拒绝再次排队，避免无限重试。"""
        task=self.store.task(task_id)
        total_limit=self.settings.get('worker','max_total_attempts',10)
        if task['status']=='failed' and task['attempts']>=total_limit:
            raise ValueError(f'该任务已尝试 {task["attempts"]} 次，达到上限；请修正原因后重建该日日报')
        self.store.retry(task_id)

    def tick(self):
        """单轮调度；活动标签只描述实际执行的步骤，不在无事可做时声称正在检查。"""
        self.refresh_config()
        self._touch_heartbeat(force=True)
        canceled=self.store.cancel_orphan_tasks()
        if canceled:log(f'清理：{canceled} 个排队任务的来源已不存在，已取消')
        rebuilt=self.process_rebuild()
        if rebuilt:self._waiting=False
        self.poll()
        task=self.store.claim()
        if task:
            self._waiting=False
            self._run_task(task)
        elif not self._waiting:
            queued=len(self.store.query("SELECT id FROM tasks WHERE status='queued'"))
            log(f'等待中：暂无可立即执行的任务，待重试 {queued} 个；将自动检查新任务和到期来源')
            self._waiting=True
            self.runtime.finish('空闲',f'待重试 {queued} 个任务；将自动检查新任务和到期来源')
        else:
            self.runtime.finish('空闲','等待下一轮调度')
        if self.store.done_signature()!=self._publish_signature:
            self.runtime.begin('publish','发布日报',detail='汇集已完成摘要')
            self._touch_heartbeat(force=True)
            self.publish_dirty()
            self.runtime.finish('空闲','等待下一轮调度')
        # tick仅由持锁Worker执行；此时没有运行中的下载或模型调用。
        if time.time()-self._cleanup_at>=3600:
            self.run_cleanup()
        self._touch_heartbeat(force=True)

    def run_cleanup(self):
        """执行一次空间清理并记录可核对的结果；Web手动入口与定时任务共用。

        文件清理与事件清理互不依赖，分别兜住各自的异常：文件清理失败时
        仍要整理事件记录，否则一次磁盘异常会让事件表长期不收敛。
        """
        from integration.cleanup import cleanup, disk_usage
        started=time.time()
        self._cleanup_at=started
        self.runtime.begin('cleanup','清理存储空间',detail='按保留期删除可再生产物')
        self._touch_heartbeat(force=True)
        try:
            result={'files':0,'bytes':0}
            file_error=''
            try:
                result=cleanup(self.settings,self.store.query('SELECT id,status FROM tasks'))
            except Exception as exc:
                file_error=type(exc).__name__
                log(f'空间清理失败（{file_error}），改为只整理事件记录；下一轮重试')
            self.store.prune_events(started-self.EVENT_RETENTION_DAYS*86400,self.EVENT_MAX_ROWS)
            usage=disk_usage(self.settings)
            self.store.set_meta('cleanup_at',started)
            self.store.set_meta('cleanup_files',result['files'])
            self.store.set_meta('cleanup_bytes',result['bytes'])
            self.store.set_meta('cleanup_usage',usage['total'])
            self.store.set_meta('cleanup_error',file_error)
            if file_error:
                # 文件没清干净要如实返回失败，不能因为事件清理成功就报成功。
                return None
            if result['files']:
                log(f'空间清理：移除 {result["files"]} 个文件，释放 {result["bytes"]/1048576:.1f} MB；'
                    f'当前占用 {usage["total"]/1048576:.1f} MB')
            else:
                log(f'空间清理：没有可移除的文件；当前占用 {usage["total"]/1048576:.1f} MB')
            return result
        except Exception as exc:
            # 清理失败不能影响后续轮次；按异常类型记录，不写文件内容。
            self.store.set_meta('cleanup_error',type(exc).__name__)
            log(f'空间清理失败（{type(exc).__name__}），下一轮继续')
            return None
        finally:
            self.runtime.finish('空闲','等待下一轮调度')
            self._touch_heartbeat(force=True)

    def storage_state(self):
        """管理页存储卡片：保留期设置、上次清理结果与当前占用。"""
        from integration.cleanup import disk_usage
        usage=disk_usage(self.settings)
        last=float(self.store.meta('cleanup_at') or 0)
        return {
            'usage':usage,
            'retention':{
                'transcript_days':self.settings.get('storage','transcript_days',30),
                'intermediate_days':self.settings.get('storage','intermediate_days',7),
                'report_days':self.settings.report_retention_days,
                'cache_days':self.settings.get('storage','cache_days',1),
                'cache_max_mb':self.settings.get('storage','cache_max_mb',512),
            },
            'last_cleanup':last,
            'last_files':self.store.meta('cleanup_files') or '0',
            'last_bytes':self.store.meta('cleanup_bytes') or '0',
            'last_error':self.store.meta('cleanup_error') or '',
            'next_cleanup':(last or time.time())+3600,
        }

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
