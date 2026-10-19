"""沿用16kHz单声道分片，使用系统ffmpeg。"""
import json
import subprocess
import os
import sys
from contextlib import contextmanager
from pathlib import Path


class MediaToolsError(RuntimeError):
    def __init__(self, failures):
        # 只由本模块生成的工具名及固定原因构成，不输出子进程内容。
        super().__init__('；'.join(failures) + '。请安装FFmpeg（含ffprobe）并确保服务账号的PATH可找到它们；Debian/Ubuntu可运行 sudo apt-get install ffmpeg，安装后重启服务。')


def check_media_tools():
    failures = []
    for tool in ('ffmpeg', 'ffprobe'):
        try:
            result = subprocess.run([tool, '-version'], capture_output=True, timeout=10,
                                    check=False, env=system_media_env())
            output = result.stdout or b''
            if result.returncode != 0 or not output.startswith((tool + ' version ').encode()):
                failures.append(f'{tool}无法正常运行')
        except FileNotFoundError:
            failures.append(f'未找到{tool}')
        except subprocess.TimeoutExpired:
            failures.append(f'{tool}检测超时')
        except OSError:
            failures.append(f'{tool}无法执行（请检查权限或动态库）')
    if failures:
        raise MediaToolsError(failures)


def system_media_env():
    """系统媒体工具使用系统动态库，避免继承冻结程序的库路径。"""
    env = os.environ.copy()
    if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
        original = env.get('LD_LIBRARY_PATH_ORIG')
        if original:
            env['LD_LIBRARY_PATH'] = original
        else:
            env.pop('LD_LIBRARY_PATH', None)
    return env


@contextmanager
def system_media_environment():
    """仅串行后台下载调用；yt-dlp内部子进程不暴露env参数。"""
    if not getattr(sys, 'frozen', False) or not sys.platform.startswith('linux'):
        yield
        return
    previous = os.environ.get('LD_LIBRARY_PATH')
    try:
        clean = system_media_env().get('LD_LIBRARY_PATH')
        if clean is None:
            os.environ.pop('LD_LIBRARY_PATH', None)
        else:
            os.environ['LD_LIBRARY_PATH'] = clean
        yield
    finally:
        if previous is None:
            os.environ.pop('LD_LIBRARY_PATH', None)
        else:
            os.environ['LD_LIBRARY_PATH'] = previous

def split_audio(media, directory, seconds=600):
    seconds = min(600, max(1, int(seconds)))
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(media)], capture_output=True, text=True, check=True, timeout=60, env=system_media_env())
    duration = float(json.loads(result.stdout)['format']['duration'])
    if duration <= 0:
        raise ValueError('无效音频时长')
    for index, start in enumerate(range(0, int(duration) + 1, seconds)):
        if start >= duration:
            break
        path = Path(directory) / f'{index:04d}.wav'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-ss', str(start), '-t', str(seconds), '-i', str(media), '-vn', '-ac', '1', '-ar', '16000', str(path)], check=True, timeout=600, env=system_media_env())
        yield path
