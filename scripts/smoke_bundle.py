"""对复制到独立目录的成品执行离线检查；HTTP仅访问本机。"""
import argparse
import re
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import sys
import urllib.request
import urllib.parse
from http.cookiejar import CookieJar
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('executable', type=Path)
    parser.add_argument('--media', action='store_true')
    args = parser.parse_args()
    executable = args.executable.resolve()
    env = os.environ.copy()
    for key in ('DAILY_CONFIG', 'DAILY_DATA_DIR', 'SILICONFLOW_API_KEY', 'LLM_BRAIN_API_KEY',
                'ADMIN_PASSWORD_HASH', 'SESSION_SECRET', 'BILI_COOKIE_FILE', 'COOKIE_SECURE',
                'PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install = root / 'install'
        shutil.copytree(executable.parent, install / 'app', symlinks=True)
        binary = install / 'app' / executable.name
        elsewhere = root / 'unrelated'
        elsewhere.mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        template = (ROOT / 'config.example.toml').read_text(encoding='utf-8')
        config = template.replace('port = 8000', f'port = {port}').replace(
            'cookie_file = ""', 'cookie_file = "data/bili-cookie.txt"')
        (install / 'config.toml').write_text(config, encoding='utf-8')
        subprocess.run([str(binary), '--help'], cwd=elsewhere, env=env, check=True, capture_output=True)
        subprocess.run([str(binary), 'doctor'] + (['--media'] if args.media else []),
                       cwd=elsewhere, env=env, check=True, timeout=120)
        password = 'offline-fixture-password'
        if os.name == 'nt':
            # Windows getpass直接读控制台，不能用管道模拟；仅创建登录测试夹具。
            sys.path.insert(0, str(ROOT))
            from integration.auth import hash_password
            from integration.config_file import save_security
            save_security(install / 'config.toml', hash_password(password), 'offline-session-fixture-' * 3)
        else:
            # 新会话没有控制终端，getpass回退读取管道中的离线夹具。
            subprocess.run([str(binary), 'setup-admin'], input=f'{password}\n{password}\n', text=True,
                           cwd=elsewhere, env=env, check=True, timeout=30, capture_output=True,
                           start_new_session=True)
            subprocess.run([str(binary), 'setup-bili'],
                           input='SESSDATA=offline; bili_jct=offline-csrf\noffline-token\n', text=True,
                           cwd=elsewhere, env=env, check=True, timeout=30, capture_output=True,
                           start_new_session=True)
        subprocess.run([str(binary), 'worker', '--once'], cwd=elsewhere, env=env, check=True, timeout=120)
        assert (install / 'data' / 'daily.sqlite').is_file(), '数据未保存到外部安装目录'
        # 命令行指定其他配置后，相对数据路径必须跟随该配置。
        custom = root / 'custom'
        custom.mkdir()
        (custom / 'config.toml').write_text(config, encoding='utf-8')
        subprocess.run([str(binary), '--config', str(custom / 'config.toml'), 'worker', '--once'],
                       cwd=elsewhere, env=env, check=True, timeout=120)
        assert (custom / 'data' / 'daily.sqlite').is_file(), '自定义配置的相对路径错误'
        with closing(sqlite3.connect(install / 'data' / 'daily.sqlite')) as db:
            assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        with closing(sqlite3.connect(install / 'data' / 'daily.sqlite')) as db:
            db.execute("DELETE FROM meta WHERE key='heartbeat'")
            db.commit()
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([str(binary)], cwd=elsewhere, env=env, stdout=log, stderr=log)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                     urllib.request.HTTPCookieProcessor(CookieJar()))
                base = f'http://127.0.0.1:{port}'
                for attempt in range(120):
                    if process.poll() is not None:
                        raise RuntimeError('打包Web进程提前退出')
                    try:
                        with opener.open(base + '/health', timeout=1) as response:
                            assert response.status == 200
                        break
                    except OSError:
                        time.sleep(0.25)
                else:
                    raise RuntimeError('打包Web启动超时')
                # 统一进程必须自动产生后台心跳。
                for attempt in range(120):
                    with closing(sqlite3.connect(install / 'data' / 'daily.sqlite')) as db:
                        heartbeat = db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
                    if heartbeat:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError('统一进程未启动后台')
                for path in ('/', '/admin/login', '/api/public/days', '/static/source-order.js'):
                    with opener.open(base + path, timeout=5) as response:
                        assert response.status == 200, path
                        assert response.read(), path
                with opener.open(base + '/admin/login', timeout=5) as response:
                    html = response.read().decode('utf-8')
                token = re.search(r'name="token" value="([^"]+)"', html).group(1)
                form = urllib.parse.urlencode({'password': password, 'token': token}).encode()
                with opener.open(base + '/admin/login', data=form, timeout=5) as response:
                    assert response.status == 200
                    assert response.url.endswith('/admin')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        assert not (elsewhere / 'data').exists(), '不应向当前工作目录写数据'
        assert not (install / 'app' / '_internal' / 'data').exists(), '不应向内置资源写数据'
    print('打包程序检查通过：离线依赖、资源、后台、外置配置与Web。')


if __name__ == '__main__':
    main()
