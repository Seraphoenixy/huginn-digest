"""读取公开RSS/正文；拒绝本机、私网和不受控重定向。"""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError


class PublicConnectionMixin:
    def _new_conn(self):
        # 直接连接已验证的sockaddr，保留原主机名用于Host、SNI及证书校验。
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError('不能访问私有网络地址')
        for family, kind, proto, _, address in addresses:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(address)
                return sock
            except OSError:
                sock.close()
        raise NewConnectionError(self, '公开地址连接失败') from None


class PublicHTTPConnection(PublicConnectionMixin, HTTPConnection):pass
class PublicHTTPSConnection(PublicConnectionMixin, HTTPSConnection):pass
class PublicHTTPPool(HTTPConnectionPool):ConnectionCls = PublicHTTPConnection
class PublicHTTPSPool(HTTPSConnectionPool):ConnectionCls = PublicHTTPSConnection


class PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http': PublicHTTPPool, 'https': PublicHTTPSPool}


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in {'https','http'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('需要公开HTTP(S)地址')
    if parts.port not in (None,80,443):
        raise ValueError('只允许标准HTTP端口')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('不能访问私有网络地址')
    return url

def fetch(url):
    session = requests.Session()
    session.trust_env = False
    session.mount("http://", PublicAdapter())
    session.mount("https://", PublicAdapter())
    try:
        for _ in range(5):
            public_url(url)
            with session.get(url, timeout=(10,30), allow_redirects=False, stream=True, headers={'User-Agent':'huginn-digest/1.0'}) as response:
                if response.status_code in (301,302,303,307,308):
                    url = urljoin(url,response.headers['Location'])
                    continue
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 5_000_000:
                        raise ValueError('正文超过5MB限制')
                return bytes(body)
        raise ValueError('重定向过多')
    finally:
        session.close()
