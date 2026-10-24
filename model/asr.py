import tempfile
import time
from integration.runtime_log import log
from integration.media import split_audio
from integration.http.siliconflow_asr import transcribe_audio

class CloudASR:
    def __init__(self, settings):
        self.settings = settings

    def transcribe(self, path):
        s = self.settings
        if not s.asr_key:
            raise ValueError('缺少 SILICONFLOW_API_KEY')
        texts = []
        with tempfile.TemporaryDirectory(dir=s.cache_dir) as folder:
            for chunk in split_audio(path, folder, s.get('asr', 'segment_seconds', 600)):
                number=len(texts)+1
                started=time.perf_counter()
                log(f'ASR：开始上传第 {number} 个音频分片，等待云端识别')
                texts.append(transcribe_audio(chunk, api_key=s.asr_key, model=s.get('asr', 'model'),
                                              timeout=s.get('asr', 'timeout', 300), endpoint=s.asr_base_url))
                log(f'ASR：第 {number} 个分片完成，返回 {len(texts[-1])} 字，耗时 {time.perf_counter()-started:.1f} 秒')
        text = '\n'.join(texts)
        if not text.strip():
            raise ValueError('ASR返回空正文')
        return text
