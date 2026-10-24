"""硅基流动音频上传；错误不回显响应正文、请求头或密钥。"""
import time
import requests
from config import DEFAULT_ASR_ENDPOINT


def transcribe_audio(path, *, api_key, model, timeout, endpoint=DEFAULT_ASR_ENDPOINT):
    for attempt in range(3):
        response = None
        try:
            with open(path, 'rb') as audio:
                response = requests.post(
                    endpoint,
                    headers={'Authorization': 'Bearer ' + api_key},
                    data={'model': model}, files={'file': ('audio.wav', audio, 'audio/wav')},
                    allow_redirects=False, timeout=(30, timeout))
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            if response.status_code != 200:
                raise RuntimeError(f'硅基流动 ASR HTTP {response.status_code}；请检查权限、额度或稍后重试')
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError('硅基流动 ASR 返回无效 JSON') from None
            if not isinstance(payload, dict) or not isinstance(payload.get('text'), str):
                raise RuntimeError('硅基流动 ASR 响应缺少 text 字段')
            return payload['text'].strip()
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError('硅基流动 ASR 网络请求失败或超时，请重试') from None
            time.sleep(2 ** attempt)
        finally:
            if response is not None:
                response.close()
