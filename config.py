"""配置唯一出口：显式参数 > 非空环境变量 > config.toml。"""
import os
import tomllib
import sys
from dataclasses import dataclass, field
from pathlib import Path

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
        for section,key,minimum,maximum in [('pipeline','poll_seconds',60,86400),('pipeline','per_source_limit',1,20),('pipeline','lookback_hours',1,24),('asr','segment_seconds',1,600),('asr','timeout',1,1800),('llm','chunk_chars',1000,12000),('llm','timeout',1,1800),('worker','idle_seconds',1,60),('worker','max_attempts',1,5)]:
            value=self.get(section,key)
            if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=maximum:
                raise ValueError(f'配置 {section}.{key} 超出支持范围')
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
