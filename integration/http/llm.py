"""集中云端传输；拒绝空正文与截断输出。"""
import time
import requests
from urllib.parse import urlsplit

class CloudError(RuntimeError):
    pass

def complete(settings, messages, *, json_mode=False):
    if not settings.llm_key:
        raise CloudError('缺少 LLM_BRAIN_API_KEY')
    body = {'model': settings.get('llm', 'model'), 'messages': messages, 'temperature': 0, 'max_tokens': 8000}
    if settings.get('llm', 'thinking_disabled', True):
        body['thinking'] = {'type': 'disabled'}
    if json_mode:
        body['response_format'] = {'type': 'json_object'}
    url = settings.get('llm', 'base_url').rstrip('/') + '/chat/completions'
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise CloudError("模型接口必须使用无内嵌凭据的HTTPS地址")
    for attempt in range(3):
        try:
            with requests.post(url, headers={'Authorization': 'Bearer ' + settings.llm_key}, json=body, allow_redirects=False, timeout=(20, settings.get('llm', 'timeout', 120))) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                if response.status_code != 200:
                    raise CloudError(f'LLM HTTP {response.status_code}')
                choice = response.json()['choices'][0]
                text = choice['message'].get('content') or ''
                if choice.get('finish_reason') != 'stop' or not text.strip():
                    raise CloudError('LLM输出为空或未完整结束')
                return text
        except requests.RequestException:
            if attempt == 2:
                raise CloudError('LLM网络请求失败') from None
            time.sleep(2 ** attempt)
    raise CloudError('LLM失败')
