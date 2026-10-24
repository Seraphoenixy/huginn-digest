"""配置唯一出口：显式参数 > 非空环境变量 > config.toml。

[llm] 文本模型段支持运行中热重载；其余设置仍在启动时读取，修改后需重启。
热重载只替换模型配置，不重建应用、不改变 Web 监听、会话密钥或数据目录。
"""
import os
import threading
import time
import tomllib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from orchestration.status import DEFAULT_TOPIC_ORDER

ROOT = Path(__file__).resolve().parent
APP_ROOT = Path(sys.executable).resolve().parent.parent if getattr(sys, 'frozen', False) else ROOT
DEFAULT_ASR_ENDPOINT = 'https://api.siliconflow.cn/v1/audio/transcriptions'
# 单次模型输出上限。默认值按“思考 token 先占额度”实测标定：纠错单次思考
# 最高约 17000，旧值 16000 会在正文写出前就被耗尽，表现为返回空内容。
DEFAULT_MAX_TOKENS = 32000
BILI_AUTO_REFRESH = True
BILI_REFRESH_CHECK_SECONDS = 86400
BILI_REFRESH_RETRY_SECONDS = 3600


def parse_https_url(name,value):
    """接口地址统一校验：必须https、有主机名、不带内嵌凭据。"""
    if not isinstance(value,str) or not value.strip():
        raise ValueError(f'配置 {name} 必须是非空HTTPS地址')
    parts=urlsplit(value)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password:
        raise ValueError(f'配置 {name} 必须是使用无内嵌凭据的HTTPS地址')
    return value


def parse_llm_config(values):
    """校验并提取 [llm] 段；热重载与启动读取共用同一规则。"""
    section=values.get('llm',{})
    if not isinstance(section,dict):
        raise ValueError('配置 [llm] 必须是表')
    base_url=parse_https_url('llm.base_url',section.get('base_url',''))
    model=section.get('model','')
    if not isinstance(model,str) or not model.strip():
        raise ValueError('配置 llm.model 必须是非空字符串')
    timeout=section.get('timeout',120)
    if not isinstance(timeout,int) or isinstance(timeout,bool) or not 1<=timeout<=1800:
        raise ValueError('配置 llm.timeout 超出支持范围')
    chunk_chars=section.get('chunk_chars',12000)
    if not isinstance(chunk_chars,int) or isinstance(chunk_chars,bool) or not 1000<=chunk_chars<=12000:
        raise ValueError('配置 llm.chunk_chars 超出支持范围')
    thinking_disabled=section.get('thinking_disabled',True)
    if not isinstance(thinking_disabled,bool):
        raise ValueError('配置 llm.thinking_disabled 必须是布尔值')
    # 单次输出上限。拆条后一次要返回十几到几十个事件，而思考 token 会先吃掉
    # 这部分额度（实测纠错单次思考可达 17000），额度不足会返回空内容。
    max_tokens=section.get('max_tokens',DEFAULT_MAX_TOKENS)
    if not isinstance(max_tokens,int) or isinstance(max_tokens,bool) or not 1000<=max_tokens<=128000:
        raise ValueError('配置 llm.max_tokens 超出支持范围')
    api_key=section.get('api_key','')
    if not isinstance(api_key,str):
        raise ValueError('配置 llm.api_key 必须是字符串')
    return {'base_url':base_url,'model':model,'timeout':timeout,'chunk_chars':chunk_chars,
            'thinking_disabled':thinking_disabled,'max_tokens':max_tokens,'api_key':api_key}

@dataclass
class Settings:
    data_dir: Path | None = None
    config_file: Path = field(default_factory=lambda: Path(os.getenv('DAILY_CONFIG') or APP_ROOT / 'config.toml'))
    asr_key: str | None = field(default=None, repr=False)
    llm_key: str | None = field(default=None, repr=False)
    admin_hash: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)
    cookie_file: str | None = None
    bili_auto_refresh: bool | None = None
    secure_cookie: bool | None = None

    def __post_init__(self):
        self.config_file=Path(self.config_file).resolve()
        with open(self.config_file, 'rb') as stream:
            self.values = tomllib.load(stream)
        llm_key_explicit=self.llm_key is not None
        self.site_name=self.get('site','name','鸦闻 · 每日简报')
        if not isinstance(self.site_name,str) or not self.site_name.strip():raise ValueError('配置 site.name 必须是非空字符串')
        # 站点地址可为空：为空时页面不输出 canonical，而不是整站500。
        # site.nav_url 已废弃：页头不再提供“返回导航”，旧配置里残留该键也不会报错。
        self.site_public_url=self.get('site','public_url','') or ''
        if not isinstance(self.site_public_url,str):raise ValueError('配置 site.public_url 必须是字符串')
        if self.site_public_url and not self.site_public_url.startswith(('https://','http://')):
            raise ValueError('配置 site.public_url 必须是HTTP(S)地址')
        for attribute,env,section,key in [('asr_key','SILICONFLOW_API_KEY','asr','api_key'),('llm_key','LLM_BRAIN_API_KEY','llm','api_key'),('admin_hash','ADMIN_PASSWORD_HASH','security','password_hash'),('session_secret','SESSION_SECRET','security','session_secret'),('cookie_file','BILI_COOKIE_FILE','bili','cookie_file')]:
            if getattr(self,attribute) is None:
                value=os.getenv(env) or self.get(section,key,'')
                if not isinstance(value,str):raise ValueError(f'配置 {section}.{key} 必须是字符串')
                setattr(self,attribute,value)
        if self.secure_cookie is None:
            value=os.getenv('COOKIE_SECURE')
            if value:
                if value.lower() not in {'true','false'}:raise ValueError('COOKIE_SECURE 需要 true 或 false')
                self.secure_cookie=value.lower()=='true'
            else:self.secure_cookie=self.get('security','secure_cookie',True)
        if not isinstance(self.secure_cookie,bool):raise ValueError('security.secure_cookie 必须是布尔值')
        if self.bili_auto_refresh is None:
            self.bili_auto_refresh=self.get('bili','auto_refresh',BILI_AUTO_REFRESH)
        if not isinstance(self.bili_auto_refresh,bool):raise ValueError('bili.auto_refresh 必须是布尔值')
        for key,default,minimum,maximum in [('refresh_check_seconds',BILI_REFRESH_CHECK_SECONDS,3600,604800),('refresh_retry_seconds',BILI_REFRESH_RETRY_SECONDS,60,86400)]:
            value=self.get('bili',key,default)
            if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=maximum:
                raise ValueError(f'配置 bili.{key} 超出支持范围')
        self.bili_refresh_check_seconds=self.get('bili','refresh_check_seconds',BILI_REFRESH_CHECK_SECONDS)
        self.bili_refresh_retry_seconds=self.get('bili','refresh_retry_seconds',BILI_REFRESH_RETRY_SECONDS)
        self.data_dir=Path(self.data_dir if self.data_dir is not None else os.getenv('DAILY_DATA_DIR') or self.get('storage','data_dir','data'))
        if not self.data_dir.is_absolute():self.data_dir=self.config_file.parent/self.data_dir
        if self.cookie_file and not Path(self.cookie_file).is_absolute():self.cookie_file=str(self.config_file.parent/self.cookie_file)
        self.web_host=self.get('server','host','127.0.0.1')
        self.web_port=self.get('server','port',8000)
        self.admin_refresh_seconds=self.get('server','admin_refresh_seconds',5)
        if type(self.admin_refresh_seconds) is not int or not 1<=self.admin_refresh_seconds<=300:raise ValueError('server.admin_refresh_seconds 超出支持范围')
        if not isinstance(self.web_host,str) or not self.web_host.strip():raise ValueError('server.host 必须是非空字符串')
        if type(self.web_port) is not int or not 1<=self.web_port<=65535:raise ValueError('server.port 超出支持范围')
        self.web_limits={}
        for key,default,minimum,maximum in [
            ('request_body_bytes',65536,1024,1048576),
            ('password_chars',1024,32,1024),
            ('target_chars',4096,256,16384),
            ('source_name_chars',200,1,1000),
            ('source_kind_chars',16,4,32),
            ('csrf_token_chars',128,43,256),
            ('source_order_chars',16384,256,65536),
            ('day_chars',10,10,10),
        ]:
            value=self.get('web_limits',key,default)
            if type(value) is not int or not minimum<=value<=maximum:
                raise ValueError(f'配置 web_limits.{key} 超出支持范围')
            self.web_limits[key]=value
        # 每项都带默认值：旧配置缺段或缺键时沿用默认，不会因升级直接拒绝启动。
        # [llm] 的 timeout/chunk_chars 由 parse_llm_config 统一校验，不在此重复。
        for section,key,default,minimum,maximum in [
            ('pipeline','poll_seconds',3600,60,86400),
            ('pipeline','per_source_limit',20,1,20),
            ('pipeline','lookback_hours',24,1,24),
            ('asr','segment_seconds',600,1,600),
            ('asr','timeout',300,1,1800),
            ('worker','idle_seconds',5,1,60),
            ('worker','max_attempts',3,1,5),
            ('worker','max_total_attempts',10,2,100),
        ]:
            value=self.get(section,key,default)
            if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=maximum:
                raise ValueError(f'配置 {section}.{key} 超出支持范围')
        llm=parse_llm_config(self.values)
        self.llm_base_url=llm['base_url']
        self.llm_max_tokens=llm['max_tokens']
        value=self.get('asr','model','')
        if not isinstance(value,str) or not value.strip():raise ValueError('配置 asr.model 必须是非空字符串')
        raw_url=self.get('asr','base_url','')
        if raw_url is None:raw_url=''
        if not isinstance(raw_url,str):raise ValueError('配置 asr.base_url 必须是非空HTTPS地址')
        # 留空表示使用默认地址，避免旧配置升级后因为缺键而拒绝启动。
        self.asr_base_url=parse_https_url('asr.base_url',raw_url.strip() or DEFAULT_ASR_ENDPOINT)
        for key,default,minimum,maximum in [('chunk_chars',4000,1000,8000),('context_chars',600,0,2000)]:
            value=self.get('correction',key,default)
            if type(value) is not int or not minimum<=value<=maximum:raise ValueError(f'配置 correction.{key} 超出支持范围')
        self.correction_terms=self.get('correction','terms',[])
        if not isinstance(self.correction_terms,list) or len(self.correction_terms)>200:
            raise ValueError('配置 correction.terms 必须为最多200项的列表')
        import re
        aliases={}
        for term in self.correction_terms:
            if not isinstance(term,dict) or set(term)!={'video','name','aliases','evidence'}:
                raise ValueError('配置 correction.terms 缺少或包含不支持的字段')
            if not isinstance(term['video'],str) or not re.fullmatch(r'BV[A-Za-z0-9]{10}',term['video']):
                raise ValueError('配置 correction.terms.video 必须为单个BV号')
            for key,limit in [('name',128),('evidence',500)]:
                value=term[key]
                if not isinstance(value,str) or not value.strip() or len(value)>limit or any(ord(c)<32 for c in value):
                    raise ValueError('配置 correction.terms 文本格式无效')
            if not isinstance(term['aliases'],list) or not 1<=len(term['aliases'])<=20:
                raise ValueError('配置 correction.terms.aliases 必须为1至20项的列表')
            for word in [term['name'],*term['aliases']]:
                if not isinstance(word,str) or not word.strip() or len(word)>128 or any(ord(c)<32 for c in word):
                    raise ValueError('配置 correction.terms.aliases 文本格式无效')
                key=(term['video'],word.casefold())
                if key in aliases and aliases[key]!=term['name']:
                    raise ValueError('配置 correction.terms 存在冲突的别名')
                aliases[key]=term['name']
        self.correction_min_length_ratio=self.get('correction','min_length_ratio',0.75)
        if type(self.correction_min_length_ratio) not in (int,float) or not 0.5<=self.correction_min_length_ratio<=1:
            raise ValueError('配置 correction.min_length_ratio 超出支持范围')
        self.pipeline_correction=self.get('pipeline','correction',True)
        if not isinstance(self.pipeline_correction,bool):raise ValueError('配置 pipeline.correction 必须是布尔值')
        self.db_path = self.data_dir / 'daily.sqlite'
        for key,default,maximum in [('cache_days',1,365),('intermediate_days',7,3650),('cache_max_mb',512,1048576)]:
            value=self.get('storage',key,default)
            if not isinstance(value,int) or isinstance(value,bool) or not 1<=value<=maximum:
                raise ValueError(f'配置 storage.{key} 超出支持范围')
        # transcript_days允许0：表示永久保留转写原文（升级前的行为）。
        transcript_days=self.get('storage','transcript_days',30)
        if not isinstance(transcript_days,int) or isinstance(transcript_days,bool) or not 0<=transcript_days<=3650:
            raise ValueError('配置 storage.transcript_days 超出支持范围')
        # report_days允许0：表示日报永久保留（升级前的行为）。
        self.report_retention_days=self.get('storage','report_days',1095)
        if not isinstance(self.report_retention_days,int) or isinstance(self.report_retention_days,bool) \
                or not 0<=self.report_retention_days<=36500:
            raise ValueError('配置 storage.report_days 超出支持范围')
        # 主题词表可配置：末项是兜底项，模型输出不在词表内时归到这里。
        raw_topics=self.get('topics','order',None)
        if raw_topics is None or raw_topics==[]:
            self.topic_order=DEFAULT_TOPIC_ORDER
        else:
            if not isinstance(raw_topics,list) or not 2<=len(raw_topics)<=20:
                raise ValueError('配置 topics.order 必须是2到20项的主题列表')
            for name in raw_topics:
                if not isinstance(name,str) or not name.strip() or len(name)>12:
                    raise ValueError('配置 topics.order 的每一项必须是1至12字的非空字符串')
            if len(set(raw_topics))!=len(raw_topics):
                raise ValueError('配置 topics.order 不能有重复主题')
            self.topic_order=tuple(raw_topics)
        self.content_dir = self.data_dir / 'content'
        self.cache_dir = self.data_dir / 'cache'
        for p in (self.data_dir, self.content_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)
        # 热重载元数据；不在repr或日志中保存任何凭据。
        self._llm_key_explicit=llm_key_explicit
        self._llm_lock=threading.RLock()
        self._llm_stamp=self._config_stamp()
        self.llm_revision=1
        self.llm_reloaded_at=time.time()
        self.llm_reload_error=''

    def get(self, section, key, default=None):
        return self.values.get(section, {}).get(key, default)

    def _config_stamp(self):
        """用文件元数据判断是否变化；先stat，只有变化时才重新解析。"""
        try:
            stat=os.stat(self.config_file)
            return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            return None

    def refresh_llm(self, force=False):
        """原子替换[llm]配置；失败保留旧配置并记录不含内容的错误。

        只应用文本模型段，不改变监听地址、会话密钥、数据目录或来源等启动期设置。
        环境变量LLM_BRAIN_API_KEY仍优先于配置文件；显式构造的llm_key优先于两者。
        """
        stamp=self._config_stamp()
        with self._llm_lock:
            if not force and stamp==self._llm_stamp:
                return False
            self._llm_stamp=stamp
            try:
                with open(self.config_file, 'rb') as stream:
                    values=tomllib.load(stream)
                llm=parse_llm_config(values)
                key=self.llm_key if self._llm_key_explicit else (os.getenv('LLM_BRAIN_API_KEY') or llm['api_key'])
                if not isinstance(key,str):
                    raise ValueError('配置 llm.api_key 必须是字符串')
                self.values['llm']=values.get('llm',{})
                self.llm_key=key
                self.llm_base_url=llm['base_url']
                self.llm_max_tokens=llm['max_tokens']
                self.llm_revision+=1
                self.llm_reloaded_at=time.time()
                self.llm_reload_error=''
                return True
            except (OSError,tomllib.TOMLDecodeError):
                self.llm_reload_error='配置文件读取或解析失败'
                return False
            except ValueError as exc:
                self.llm_reload_error=str(exc)
                return False
            except Exception:
                self.llm_reload_error='配置热加载失败'
                return False

    def llm_state(self):
        """管理页可见的模型状态；不返回Key或完整接口路径。"""
        with self._llm_lock:
            model=self.values.get('llm',{}).get('model','')
            return {'model':model,'host':urlsplit(self.llm_base_url).hostname or '',
                    'revision':self.llm_revision,'reloaded_at':self.llm_reloaded_at,
                    'error':self.llm_reload_error,'configured':bool(self.llm_key),
                    'max_tokens':getattr(self,'llm_max_tokens',DEFAULT_MAX_TOKENS),
                    'pending':self._config_stamp()!=self._llm_stamp}
