#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站视频源（与转写处理无关）

职责边界：给定 mid / BV号，负责「拿到媒体文件」——
- 分页拉取用户投稿列表（wbi 签名 + 设备 Cookie 防风控）
- 查询视频分P/CID 详情
- yt-dlp 下载最低音频流（或最低画质完整视频）

本模块不 import 任何转写相关依赖（torch/qwen_asr），
替换转写引擎或更换视频来源时互不影响。
"""

import os
import re
import time
import hashlib
import urllib.parse

from copy import copy
from integration.bili_cookies import BiliSession
from integration.bili_refresh import BiliRefreshError
from integration.runtime_log import log
from config import BILI_AUTO_REFRESH, BILI_REFRESH_CHECK_SECONDS, BILI_REFRESH_RETRY_SECONDS


class _DownloadLogger:
    def debug(self, message):pass
    def warning(self, message):pass
    def error(self, message):pass


class BiliSourceError(RuntimeError):
    """固定、脱敏的来源错误，禁止将失败伪装成空投稿列表。"""

    def __init__(self, code, login_status=None):
        self.code = code
        if code == -352:
            message = 'B站风控拦截（-352），请检查登录Cookie和访问环境，稍后检查更新'
        elif code in (412, 429):
            message = f'B站请求受限（HTTP {code}），请稍后检查更新'
        elif code == -101:
            message = 'B站登录已失效（-101），请更新Cookie后检查更新'
        elif code == 'refresh':
            message = 'B站登录续期失败或正在冷却，请检查配套Cookie与刷新口令，稍后重试'
        else:
            message = 'B站投稿列表获取失败，请稍后检查更新'
        if login_status is False and code != -101:
            message += '；登录检查显示当前未登录，请更新Cookie'
        super().__init__(message)


# wbi 签名固定混淆表（B站前端算法，长期稳定）
_WBI_MIXIN_KEY_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 空间搜索接口需要携带的设备指纹参数（固定示例值即可通过校验）
_DM_PARAMS = {
    'dm_img_list': '[]',
    'dm_img_str': 'V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ',
    'dm_cover_img_str': 'QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgNjMwICgweDM4OTIpIERpcmVjdDNEMTEgdnNfNV8wIHBzXzVfMCwgRDNEMTEp',
    'dm_img_inter': '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


def sanitize_cookie(cookie_value):
    """
    规范化 Cookie，去掉无效的非 ASCII 字符，并忽略空/注释段。
    HTTP Cookie 头必须是 ASCII，B站浏览器复制的 Cookie 可能带有异常字符。
    """
    if cookie_value is None:
        return ""

    cleaned_parts = []
    for raw_line in str(cookie_value).splitlines():
        for item in raw_line.split(';'):
            item = item.strip()
            if not item or item.startswith('#'):
                continue
            if '=' not in item:
                continue
            name, value = item.split('=', 1)
            name = ''.join(ch for ch in name.strip() if ch.isascii() and ch not in ('\r', '\n', '\t'))
            value = ''.join(ch for ch in value.strip() if ch.isascii())
            if not name:
                continue
            cleaned_parts.append(f"{name}={value}")

    return '; '.join(cleaned_parts)


def extract_mid_from_url(url_or_mid):
    """
    从B站空间URL或纯数字中提取用户ID。

    只接受纯数字或 space.bilibili.com 链接，避免把任意含数字的地址当成UP主。
    """
    value = str(url_or_mid).strip()
    if value.isdigit():
        return value
    parts = urllib.parse.urlsplit(value if '//' in value else 'https://' + value)
    if (parts.hostname or '').lower() not in {'space.bilibili.com', 'm.bilibili.com'}:
        return None
    match = re.search(r'/(\d+)(?:/|$)', parts.path)
    return match.group(1) if match else None


def extract_bvid_from_url(url_or_bvid):
    """
    从 B 站视频 URL 或 BV 号中提取 BV 号。

    支持例如：
    - BV1xx411c7mD
    - https://www.bilibili.com/video/BV1xx411c7mD/
    - 带 query string 的视频 URL
    """
    if url_or_bvid is None:
        return None
    value = str(url_or_bvid).strip()
    if not value:
        return None

    match = re.search(r'(BV[0-9A-Za-z]{10})', value, re.IGNORECASE)
    if not match:
        return None
    bvid = match.group(1)
    # BV 前缀是固定大写；其余字符保持用户输入，避免改动有效 ID。
    return 'BV' + bvid[2:]


def extract_aid_from_url(url_or_aid):
    """从 av 号、aid 参数或纯数字中提取稿件 aid。"""
    if url_or_aid is None:
        return None
    value = str(url_or_aid).strip()
    if not value:
        return None
    if value.isdigit():
        return value

    match = re.search(r'(?i)(?:av|aid[=/]|/video/)\s*(\d+)', value)
    return match.group(1) if match else None


class BiliVideoSource:
    """
    B站视频源。

    cookie / cookie_file: 登录态（空间接口需要，否则易触发 -352/412 风控）
    download_cache_dir: 媒体下载缓存目录
    keep_media: True 时下载最低画质完整视频并保留；False 只拉音频流
    """

    def __init__(self, cookie=None, cookie_file=None,
                 download_cache_dir=None, keep_media=False, auto_refresh=BILI_AUTO_REFRESH,
                 refresh_check_seconds=BILI_REFRESH_CHECK_SECONDS, refresh_retry_seconds=BILI_REFRESH_RETRY_SECONDS):
        # 默认使用系统临时目录，避免集成层反查配置；编排层始终显式传入data/cache/downloads。
        if download_cache_dir is None:
            import tempfile
            download_cache_dir = os.path.join(tempfile.gettempdir(), 'huginn-digest-downloads')
        self.download_cache_dir = download_cache_dir
        self.keep_media = keep_media
        self.auto_refresh = auto_refresh
        self.refresh_check_seconds = refresh_check_seconds
        self.refresh_retry_seconds = refresh_retry_seconds

        self.session = BiliSession(cookie_file)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://www.bilibili.com/',
        })
        try:
            self.session.load(cookie, sanitize_cookie)
        except (OSError, ValueError, UnicodeError):
            self.session.close()
            raise BiliSourceError('cookie_load') from None
        self._wbi_keys = None  # (img_key, sub_key) 缓存
        self._login_status = None
        # 设备 Cookie 延迟到第一次访问 B 站接口时获取。这样仅处理本地文件时，
        # 创建 TranscriptionPipeline 不会产生无关的网络请求。
        self._device_cookies_ready = False

    # ==================== Cookie / wbi 签名 ====================

    def _ensure_device_cookies(self):
        """
        确保 buvid3/buvid4/b_nut 等设备 Cookie 存在，否则通过官方 spi 接口获取。
        缺少这些 Cookie 时，空间类接口会返回 -352 风控校验失败。
        若用户 Cookie 中已包含 buvid3，则不覆盖。
        """
        try:
            self.session.ensure_login(self.auto_refresh, self.refresh_check_seconds, self.refresh_retry_seconds)
        except BiliRefreshError as exc:
            raise BiliSourceError(exc.code) from None
        if self._device_cookies_ready:
            return
        self._device_cookies_ready = True
        try:
            if any(c.name == 'buvid3' and not c.is_expired() for c in self.session.cookies):
                return
            resp = self.session.get(
                'https://api.bilibili.com/x/frontend/finger/spi', timeout=15)
            data = resp.json().get('data', {})
            if data.get('b_3'):
                self.session.cookies.set('buvid3', data['b_3'], domain='.bilibili.com')
            if data.get('b_4'):
                self.session.cookies.set('buvid4', data['b_4'], domain='.bilibili.com')
        except Exception:
            log('B站设备Cookie获取失败（不影响已登录请求）')

    def _get_wbi_mixin_key(self):
        """
        从 nav 接口获取 wbi 密钥对，按混淆表生成 mixin_key。
        密钥会定期轮换，这里每次启动重新获取。
        """
        self._ensure_device_cookies()
        if self._wbi_keys is None:
            resp = self.session.get(
                'https://api.bilibili.com/x/web-interface/nav', timeout=15)
            payload = resp.json()
            if payload.get('code') == -101:
                self._login_status = False
                raise BiliSourceError(-101)
            elif payload.get('code') == 0:
                self._login_status = bool((payload.get('data') or {}).get('isLogin'))
            wbi_img = payload['data']['wbi_img']
            img_key = wbi_img['img_url'].rsplit('/', 1)[1].split('.')[0]
            sub_key = wbi_img['sub_url'].rsplit('/', 1)[1].split('.')[0]
            self._wbi_keys = (img_key, sub_key)
        img_key, sub_key = self._wbi_keys
        orig = img_key + sub_key
        return ''.join(orig[i] for i in _WBI_MIXIN_KEY_TAB)[:32]

    def _sign_wbi_params(self, params):
        """
        对请求参数进行 wbi 签名：追加 wts 时间戳，排序后计算 w_rid。
        返回带签名的新参数字典。
        """
        mixin_key = self._get_wbi_mixin_key()
        params = dict(params)
        params['wts'] = int(time.time())
        params = dict(sorted(params.items()))
        # B站要求过滤参数值中的特殊字符
        skip_chars = "!()*'"
        params = {
            k: ''.join(ch for ch in str(v) if ch not in skip_chars)
            for k, v in params.items()
        }
        query = urllib.parse.urlencode(params)
        params['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return params

    # ==================== 投稿列表与分P详情 ====================

    def fetch_page(self, mid, pn, ps):
        """
        调用 /x/space/wbi/arc/search（wbi 签名）获取一页投稿列表
        返回 (vlist, total_count)
        """
        self._ensure_device_cookies()
        url = 'https://api.bilibili.com/x/space/wbi/arc/search'
        params = {
            'mid': str(mid),
            'ps': ps,
            'pn': pn,
            'order': 'pubdate',
            'platform': 'web',
            'web_location': '1550101',
        }
        params.update(_DM_PARAMS)

        try:
            signed = self._sign_wbi_params(params)
            resp = self.session.get(
                url, params=signed, timeout=20,
                headers={
                    'Referer': f'https://space.bilibili.com/{mid}/video',
                    'Origin': 'https://space.bilibili.com',
                })
            if resp.status_code in (412, 429):
                raise BiliSourceError(resp.status_code, self._login_status)
            resp.raise_for_status()
            data = resp.json()
            code = data.get('code', -1)
            if code != 0:
                raise BiliSourceError(code, self._login_status)

            page_data = data['data']
            total = (page_data.get('page') or {}).get('count', 0)
            vlist = []
            for it in (page_data.get('list') or {}).get('vlist', []):
                bvid = it.get('bvid', '')
                aid = it.get('aid', '')
                if not bvid and not aid:
                    continue
                vlist.append({
                    'bvid': bvid,
                    'aid': aid,
                    'title': it.get('title', ''),
                    'play': it.get('play', 0),
                    'created': it.get('created', 0),
                    'duration': it.get('duration', 0),
                    'description': it.get('description', ''),
                })
            return vlist, total
        except BiliSourceError:
            raise
        except Exception:
            raise BiliSourceError(None, self._login_status) from None

    def get_video_info(self, url_or_id):
        """
        获取单个视频详情，支持 BV 号、视频 URL、av 号和 aid。
        成功返回 B 站 view 接口的 data 字典，失败返回 None。
        """
        self._ensure_device_cookies()

        bvid = extract_bvid_from_url(url_or_id)
        aid = None if bvid else extract_aid_from_url(url_or_id)
        if not bvid and not aid:
            log('B站：无法识别视频地址或BV/av号')
            return None

        url = "https://api.bilibili.com/x/web-interface/view"
        params = {'bvid': bvid} if bvid else {'aid': aid}
        try:
            resp = self.session.get(url, params=params, timeout=15)
            data = resp.json()
            if data.get('code') != 0:
                log('B站：获取视频详情失败，请检查视频或登录状态')
                return None
            return data.get('data') or None
        except Exception:
            log('B站：获取视频详情出错，请检查网络或登录状态')
            return None

    def get_video_pages(self, bvid):
        """
        通过 BV/av 号或视频 URL 获取视频所有分P的 cid 和标题。
        返回 (pages_info, video_title)：
        - pages_info: [{'cid': ..., 'page_title': ..., 'page': 1}, ...]
        - video_title: 视频总标题
        """
        info = self.get_video_info(bvid)
        if not info:
            return [], None

        title = info.get('title', extract_bvid_from_url(bvid) or str(bvid))
        pages = info.get('pages', [])
        if not pages:
            return [], title
        pages_info = []
        for p in pages:
            pages_info.append({
                'cid': p.get('cid'),
                'page_title': p.get('part', ''),
                'page': p.get('page', 1),
                'duration': p.get('duration', 0),
            })
        return pages_info, title

    # ==================== 媒体下载（yt-dlp） ====================

    def _populate_download_cookies(self, jar):
        """只在内存中转入B站域Cookie，不创建含凭据的临时文件。"""
        for cookie in self.session.cookies:
            domain=cookie.domain.lstrip('.').lower()
            if not cookie.is_expired() and (domain=='bilibili.com' or domain.endswith('.bilibili.com')):
                scoped=copy(cookie)
                scoped.secure=True
                jar.set_cookie(scoped)

    def download_media(self, bvid, page_num=1):
        """
        下载单个分P的最低音频流（keep_media=True 时为最低画质完整视频）。
        返回下载产物路径；失败抛出 RuntimeError。
        """
        self._ensure_device_cookies()
        import yt_dlp

        page_dir = os.path.join(self.download_cache_dir, bvid)
        os.makedirs(page_dir, exist_ok=True)
        out_base = os.path.join(page_dir, f"P{page_num}")
        # 已下载过则直接复用
        existing = self._find_downloaded_file(page_dir, f"P{page_num}")
        if existing:
            return existing

        url = f"https://www.bilibili.com/video/{bvid}"
        if page_num > 1:
            url += f"?p={page_num}"

        headers = {
            'Referer': f'https://www.bilibili.com/video/{bvid}',
            'User-Agent': self.session.headers.get('User-Agent', ''),
        }
        # 转写只需音频：worstaudio 体积最小；keep_media 时取最低画质完整视频
        fmt = ('worst/worstvideo*+worstaudio*/worstvideo+worstaudio'
               if self.keep_media else 'worstaudio/worstaudio*/worst')
        ydl_opts = {
            'format': fmt,
            'outtmpl': out_base + '.%(ext)s',
            'http_headers': headers,
            'quiet': True,
            'logger': _DownloadLogger(),
            'no_warnings': True,
            'noplaylist': True,
            'concurrent_fragment_downloads': 1,
            'retries': 3,
            'socket_timeout': 30,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self._populate_download_cookies(ydl.cookiejar)
                from integration.media import system_media_environment
                with system_media_environment():
                    ydl.download([url])
        except Exception:
            raise RuntimeError("视频下载失败，请检查网络或登录状态") from None

        media_path = self._find_downloaded_file(page_dir, f"P{page_num}")
        if not media_path:
            raise RuntimeError("下载完成但未找到产物文件")
        return media_path

    @staticmethod
    def _find_downloaded_file(page_dir, prefix):
        """
        在 page_dir 中查找以 prefix 开头的完整下载产物。

        yt-dlp的未完成文件以 .part/.ytdl 结尾，绝不能当作成品交给ASR，
        否则中断的下载会产出截断转写且不再重试。
        """
        if not os.path.isdir(page_dir):
            return None
        candidates = []
        for name in sorted(os.listdir(page_dir)):
            path = os.path.join(page_dir, name)
            if not os.path.isfile(path) or not name.startswith(prefix + '.'):
                continue
            if name.endswith(('.part', '.ytdl', '.temp', '.tmp')):
                continue
            candidates.append(path)
        return candidates[0] if candidates else None
