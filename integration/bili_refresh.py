"""B站Web登录续期协议；仅由BiliSession调用，错误不包含响应正文。"""
import re
import requests
from html.parser import HTMLParser


class BiliRefreshError(RuntimeError):
    def __init__(self, code='refresh'):
        self.code = code if code in (-101, -352, 412, 429) else 'refresh'
        super().__init__('B站登录续期失败，请检查配套凭据或稍后重试')


def correspond_path(timestamp):
    # B站Web端公开RSA公钥，不是用户凭据。
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_public_key(b'''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----''')
    return key.encrypt(f'refresh_{timestamp}'.encode(), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).hex()


class RefreshHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.value = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'div' and dict(attrs).get('id') == '1-name':
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'div':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.value += data


def cookie_value(session, name):
    request = session.prepare_request(requests.Request(
        'GET', 'https://passport.bilibili.com/'))
    from http.cookies import SimpleCookie
    cookies = SimpleCookie()
    cookies.load(request.headers.get('Cookie', ''))
    return cookies[name].value if name in cookies else ''


def request(session, method, url, **kwargs):
    response = session.request(method, url, timeout=15, allow_redirects=False, **kwargs)
    if response.status_code != 200:
        raise BiliRefreshError(response.status_code)
    return response


def payload(response):
    data = response.json()
    if not isinstance(data, dict) or type(data.get('code')) is not int or data['code'] != 0:
        raise BiliRefreshError(data.get('code') if isinstance(data, dict) else None)
    return data


BASE = 'https://passport.bilibili.com/x/passport-login/web/'


def check(session):
    data = payload(request(session, 'GET', BASE + 'cookie/info',
                           params={'csrf': cookie_value(session, 'bili_jct')})).get('data')
    if not isinstance(data, dict) or type(data.get('refresh')) is not bool:
        raise BiliRefreshError()
    return data


def rotate(session, token, timestamp):
    if type(timestamp) is not int or timestamp <= 0:
        raise BiliRefreshError()
    response = request(session, 'GET', 'https://www.bilibili.com/correspond/1/' + correspond_path(timestamp))
    parser = RefreshHTML()
    parser.feed(response.text)
    csrf = parser.value.strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,512}', csrf):
        raise BiliRefreshError()
    response = request(session, 'POST', BASE + 'cookie/refresh', data={
        'csrf': cookie_value(session, 'bili_jct'), 'refresh_csrf': csrf,
        'source': 'main_web', 'refresh_token': token})
    data = payload(response).get('data')
    if not isinstance(data, dict) or data.get('status') != 0:
        raise BiliRefreshError()
    new_token = data.get('refresh_token')
    if not isinstance(new_token, str) or not new_token or len(new_token) > 4096:
        raise BiliRefreshError()
    from integration.bili_cookies import scoped
    updated = [cookie for cookie in response.cookies if scoped(cookie) and not cookie.is_expired()]
    if not {'SESSDATA', 'bili_jct'}.issubset({c.name for c in updated if c.value}):
        raise BiliRefreshError()
    # 导入请求头会丢失原始作用域，清除同名旧值，避免新旧凭据同时发送。
    for cookie in updated:
        for old in list(session.cookies):
            if old.name == cookie.name:
                session.cookies.clear(old.domain, old.path, old.name)
        cookie.secure = True
        session.cookies.set_cookie(cookie)
    return new_token


def confirm(session, old_token):
    payload(request(session, 'POST', BASE + 'confirm/refresh', data={
        'csrf': cookie_value(session, 'bili_jct'), 'refresh_token': old_token}))
