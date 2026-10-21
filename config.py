"""配置唯一出口：显式参数 > 非空环境变量 > config.toml。"""
import os
import tomllib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
APP_ROOT = Path(sys.executable).resolve().parent.parent if getattr(sys, 'frozen', False) else ROOT
BILI_AUTO_REFRESH = True
BILI_REFRESH_CHECK_SECONDS = 86400
BILI_REFRESH_RETRY_SECONDS = 3600

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
        self.site_name=self.get('site','name','鸦闻 · 每日简报')
        if not isinstance(self.site_name,str) or not self.site_name.strip():raise ValueError('配置 site.name 必须是非空字符串')
        # 站点地址可为空：为空时页面不输出 canonical/导航链接，而不是整站500。
        self.site_public_url=self.get('site','public_url','') or ''
        self.site_nav_url=self.get('site','nav_url','') or ''
        for key,value in [('public_url',self.site_public_url),('nav_url',self.site_nav_url)]:
            if not isinstance(value,str):raise ValueError(f'配置 site.{key} 必须是字符串')
            if value and not (value.startswith('https://') or value.startswith('http://')):
                raise ValueError(f'配置 site.{key} 必须是HTTP(S)地址')
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
        for section,key,default,minimum,maximum in [
            ('pipeline','poll_seconds',3600,60,86400),
            ('pipeline','per_source_limit',20,1,20),
            ('pipeline','lookback_hours',24,1,24),
            ('asr','segment_seconds',600,1,600),
            ('asr','timeout',300,1,1800),
            ('llm','chunk_chars',12000,1000,12000),
            ('llm','timeout',120,1,1800),
            ('worker','idle_seconds',5,1,60),
            ('worker','max_attempts',3,1,5),
        ]:
            value=self.get(section,key,default)
            if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=maximum:
                raise ValueError(f'配置 {section}.{key} 超出支持范围')
        self.llm_base_url=self.get('llm','base_url','')
        if not isinstance(self.llm_base_url,str) or not self.llm_base_url.strip():
            raise ValueError('配置 llm.base_url 必须是非空HTTPS地址')
        parts=urlsplit(self.llm_base_url)
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('配置 llm.base_url 必须是使用无内嵌凭据的HTTPS地址')
        for section,key in [('asr','model'),('llm','model')]:
            value=self.get(section,key,'')
            if not isinstance(value,str) or not value.strip():raise ValueError(f'配置 {section}.{key} 必须是非空字符串')
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
        self.content_dir = self.data_dir / 'content'
        self.cache_dir = self.data_dir / 'cache'
        for p in (self.data_dir, self.content_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)

    def get(self, section, key, default=None):
        return self.values.get(section, {}).get(key, default)
