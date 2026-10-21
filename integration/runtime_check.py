"""离线运行诊断，不触发采集或模型调用。"""
import tempfile
import wave
from pathlib import Path
from config import ROOT


def check_runtime(settings, media=False):
    import yt_dlp
    import trafilatura
    from integration.bili_refresh import correspond_path
    from integration.media import split_audio
    for resource in ('interaction/templates/daily.html', 'interaction/templates/base.html',
                     'interaction/templates/login.html', 'interaction/templates/admin.html',
                     'interaction/static/source-order.js', 'interaction/static/admin-refresh.js',
                     'interaction/static/login.js', 'knowledge/prompts/correct.md',
                     'knowledge/prompts/summarize.md'):
        if not (ROOT / resource).read_text(encoding='utf-8').strip():
            raise ValueError('程序资源缺失')
    # 验证动态导入、正文提取数据和本地加密扩展，不触网。
    from yt_dlp.extractor.bilibili import BiliBiliIE
    if not BiliBiliIE.suitable('https://www.bilibili.com/video/BV1Mbp76oESC'):
        raise ValueError('下载器不可用')
    if not trafilatura.extract('<html><body><article><p>' + 'Offline article content. ' * 100 + '</p></article></body></html>'):
        raise ValueError('正文提取不可用')
    correspond_path(1700000000000)
    results = ['模板、脚本与提示词：可用',
               f'下载器、正文提取与加密：可用（yt-dlp {yt_dlp.version.__version__}）',
               '任务存储：可用']
    if media:
        with tempfile.TemporaryDirectory(dir=settings.cache_dir) as directory:
            path = Path(directory) / 'sample.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b'\0\0' * 32000)
            chunks = list(split_audio(path, directory, seconds=1))
            if len(chunks) != 2 or not all(chunk.stat().st_size > 44 for chunk in chunks):
                raise ValueError('媒体分片检查失败')
        results.append('系统FFmpeg/FFprobe分片：可用')
    return results
