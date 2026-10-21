"""RSS读取与正文抽取，不做推理。"""
import calendar
import time
from urllib.parse import urljoin
import feedparser
import trafilatura
from integration.http.public import fetch, public_url
from integration.runtime_log import log


def discover(url, now=None):
    """解析订阅条目；单条非法链接只跳过该条，不让整源检查失败。

    缺少发布时间的条目按发现时刻归日，避免新条目被写进建立订阅当天的历史日报。
    """
    feed = feedparser.parse(fetch(url))
    if feed.bozo and not feed.entries:
        raise ValueError('无法解析RSS')
    discovered = now if now is not None else time.time()
    entries = []
    skipped = 0
    for item in feed.entries:
        link = urljoin(url, item.get('link', ''))
        if not item.get('link'):
            continue
        try:
            public_url(link)
        except ValueError:
            skipped += 1
            continue
        stamp = item.get('published_parsed') or item.get('updated_parsed')
        published = calendar.timegm(stamp) if stamp else discovered
        body = '\n'.join(c.get('value', '') for c in item.get('content', [])) or item.get('summary', '')
        entries.append({'url': link, 'title': item.get('title', link), 'published': published, 'body': body})
    if skipped:
        log(f'RSS：跳过 {skipped} 条不可访问的链接')
    return entries


def extract(url, fallback=''):
    try:
        text = trafilatura.extract(fetch(url), include_comments=False, include_tables=True)
        if text and len(text.strip()) >= 80:
            return text, ''
    except Exception:
        pass
    text = trafilatura.extract(fallback) if fallback else ''
    if not text or not text.strip():
        # Feed text can be plain text rather than HTML.
        from html.parser import HTMLParser
        class Plain(HTMLParser):
            def __init__(self): super().__init__(); self.parts=[]
            def handle_data(self,data): self.parts.append(data)
        parser=Plain(); parser.feed(fallback)
        text=' '.join(parser.parts)
    if not text or not text.strip():
        raise ValueError('无法获取文章正文或RSS摘要')
    return text, '仅获取到RSS正文或摘要，信息可能不完整'
