"""终端日志唯一出口；只传阶段、计数与异常类型，不传凭据/正文。"""
from datetime import datetime, timedelta, timezone

def log(message):
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{stamp}] {message}',flush=True)
