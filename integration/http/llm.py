"""集中云端传输；拒绝空正文与截断输出。"""
import time
import requests
from urllib.parse import urlsplit

class CloudError(RuntimeError):
    @property
    def public_reason(self):
        # Only fixed messages produced here may reach task warnings.
        message=str(self)
        known={'缺少 LLM_BRAIN_API_KEY':'缺少模型凭据','LLM输出被截断':'输出被截断',
               'LLM输出为空':'输出为空','LLM网络请求失败':'网络连接失败',
               '模型接口必须使用无内嵌凭据的HTTPS地址':'接口地址无效'}
        if message in known:return known[message]
        import re
        if re.fullmatch(r'LLM HTTP [1-5][0-9]{2}',message):return message
        return '云端模型调用失败'

def complete(settings, messages, *, json_mode=False):
    if not settings.llm_key:
        raise CloudError('缺少 LLM_BRAIN_API_KEY')
    # 单次输出上限走配置（默认 32000）。实测“思考 token”会先占掉这份额度
    # （纠错单次最高约 17000），额度不足时接口返回空内容而 finish_reason 仍是
    # stop，只能靠“内容为空”识别，所以必须留足余量。
    body = {'model': settings.get('llm', 'model'), 'messages': messages, 'temperature': 0,
            'max_tokens': settings.get('llm', 'max_tokens', 32000)}
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
                payload = response.json()
                choice = payload['choices'][0]
                text = choice['message'].get('content') or ''
                finish = choice.get('finish_reason')
                if finish != 'stop':
                    # 截断是结构性错误，重试同样的请求只会同样被截断。
                    raise CloudError('LLM输出被截断')
                if not text.strip():
                    # 空正文常见于思考 token 吃满配额：同一请求重试通常能出结果，
                    # 所以按可重试处理，而不是直接放弃这一段。
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise CloudError('LLM输出为空')
                return text
        except requests.RequestException:
            if attempt == 2:
                raise CloudError('LLM网络请求失败') from None
            time.sleep(2 ** attempt)
    raise CloudError('LLM失败')
