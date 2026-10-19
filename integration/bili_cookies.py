"""B站Cookie会话：兼容请求头导入，持久化服务端轮换及有效期。"""
import os
import tempfile
import json
import time
from copy import copy
from http.cookiejar import LWPCookieJar
from pathlib import Path

import requests
from integration.lock import worker_lock
from integration.bili_refresh import BiliRefreshError
from config import BILI_AUTO_REFRESH, BILI_REFRESH_CHECK_SECONDS, BILI_REFRESH_RETRY_SECONDS

META = '# qingniao-digest-login: '
LEGACY_META = '# auto-daily-login: '


def scoped(cookie):
    domain = cookie.domain.lstrip('.').lower()
    return domain == 'bilibili.com' or domain.endswith('.bilibili.com')


class BiliSession(requests.Session):
    def __init__(self, cookie_file=None):
        super().__init__()
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self._snapshot = None
        self._metadata = {}
        self._suspend_persist = False

    def load(self, header, sanitize):
        self.cookies.clear()
        self._metadata = {}
        self._sanitize = sanitize
        if self.cookie_file:
            try:
                self._snapshot = self.cookie_file.read_bytes()
                header = self._snapshot.decode('utf-8')
            except FileNotFoundError:
                return
        if header and header.startswith('#LWP-Cookies-2.0'):
            for line in header.splitlines():
                if line.startswith((META, LEGACY_META)):
                    self._metadata = json.loads(line.split(': ', 1)[1])
                    if not isinstance(self._metadata, dict):
                        raise ValueError('无效登录凭据格式')
            jar = LWPCookieJar()
            # 使用内存解析，不额外读取可能已被人工更新的文件。
            import io
            jar._really_load(io.StringIO(header), '', True, False)
            for cookie in jar:
                if scoped(cookie):
                    cookie.secure = True
                    self.cookies.set_cookie(cookie)
        else:
            for item in sanitize(header).split(';'):
                if '=' not in item:
                    continue
                name, value = item.strip().split('=', 1)
                self.cookies.set(name, value, domain='.bilibili.com', path='/', secure=True)

    def send(self, request, **kwargs):
        response = super().send(request, **kwargs)
        self.persist()
        return response

    def persist(self):
        if self._suspend_persist:
            return
        if not self.cookie_file or self._snapshot is None:
            return
        try:
            with worker_lock(str(self.cookie_file) + '.lock'):
                self._save()
        except (OSError, BiliRefreshError):
            print('B站Cookie保存失败，请检查文件权限或稍后重试')

    def _save(self):
        temporary = None
        try:
            # 其他会话或用户更新后，旧会话不得覆盖新凭据。
            if self.cookie_file.read_bytes() != self._snapshot:
                raise BiliRefreshError()
            jar = LWPCookieJar()
            for cookie in self.cookies:
                if scoped(cookie) and not cookie.is_expired():
                    item = copy(cookie)
                    item.secure = True
                    jar.set_cookie(item)
            metadata = META + json.dumps(self._metadata, ensure_ascii=True) + '\n' if self._metadata else ''
            content = ('#LWP-Cookies-2.0\n' + metadata + jar.as_lwp_str(ignore_discard=True)).encode('utf-8')
            if content == self._snapshot:
                return
            fd, temporary = tempfile.mkstemp(dir=self.cookie_file.parent, prefix='.cookie-')
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
            os.chmod(temporary, 0o600)
            if self.cookie_file.read_bytes() != self._snapshot:
                raise BiliRefreshError()
            os.replace(temporary, self.cookie_file)
            self._snapshot = content
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def import_login(self, header, token, sanitize):
        """一次性导入配套Cookie与刷新口令；不发起网络请求。"""
        if not self.cookie_file or not token or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError('请提供有效Cookie文件路径和刷新口令')
        with worker_lock(str(self.cookie_file) + '.lock'):
            snapshot = self.cookie_file.read_bytes() if self.cookie_file.exists() else b''
            self._sanitize = sanitize
            self.cookies.clear()
            for item in sanitize(header).split(';'):
                if '=' in item:
                    name, value = item.strip().split('=', 1)
                    self.cookies.set(name, value, domain='.bilibili.com', path='/', secure=True)
            from integration.bili_refresh import cookie_value
            if not cookie_value(self, 'SESSDATA') or not cookie_value(self, 'bili_jct'):
                raise ValueError('Cookie缺少SESSDATA或bili_jct')
            self._snapshot = snapshot
            self._metadata = {'refresh_token': token}
            if not self.cookie_file.exists():
                fd = os.open(self.cookie_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            self._save()

    def ensure_login(self, enabled=BILI_AUTO_REFRESH, check_seconds=BILI_REFRESH_CHECK_SECONDS,
                     retry_seconds=BILI_REFRESH_RETRY_SECONDS):
        """按需检查；同文件锁串行轮换，先保存新凭据，再废止旧凭据。"""
        if not enabled or not self.cookie_file or self._snapshot is None:
            return
        try:
            with worker_lock(str(self.cookie_file) + '.lock'):
                if self.cookie_file.read_bytes() != self._snapshot:
                    self.load(None, self._sanitize)
                meta = self._metadata
                token = meta.get('refresh_token')
                if not token:
                    return
                now = time.time()
                if meta.get('error') and now - meta.get('last_attempt', 0) < retry_seconds:
                    raise BiliRefreshError(meta['error'])
                if not meta.get('pending_confirm') and now - meta.get('last_check', 0) < check_seconds:
                    return
                from integration.bili_refresh import check, rotate, confirm
                old_cookies = copy(self.cookies)
                old_meta = dict(meta)
                committed = False
                self._suspend_persist = True
                try:
                    # 在发请求前落盘尝试时间，崩溃重启也不会立即反复刷新。
                    meta['last_attempt'] = now
                    meta['error'] = 'refresh'
                    self._save()
                    if meta.get('pending_confirm'):
                        confirm(self, meta['pending_confirm'])
                        meta.pop('pending_confirm')
                        meta.pop('error', None)
                        meta['last_check'] = now
                        self._save()
                        return
                    info = check(self)
                    if info['refresh']:
                        new_token = rotate(self, token, info.get('timestamp'))
                        meta['refresh_token'] = new_token
                        meta['pending_confirm'] = token
                        self._save()
                        committed = True
                        confirm(self, token)
                        meta.pop('pending_confirm')
                    meta['last_check'] = now
                    meta.pop('error', None)
                    self._save()
                except Exception as exc:
                    if not committed:
                        # 确认恢复分支已有持久的新凭据，不回退到过期凭据。
                        self.cookies = old_cookies
                        self._metadata = dict(old_meta)
                    self._metadata.update(last_attempt=now, error=getattr(exc, 'code', 'refresh'))
                    try:
                        self._save()
                    except Exception:
                        pass
                    raise BiliRefreshError(getattr(exc, 'code', 'refresh')) from None
                finally:
                    self._suspend_persist = False
        except BiliRefreshError:
            raise
        except Exception:
            raise BiliRefreshError() from None
