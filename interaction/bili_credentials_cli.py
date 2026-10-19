"""本机隐藏输入配套B站凭据；只调用DailyService。"""
import getpass
from config import Settings
from orchestration.service import DailyService


def main():
    service = DailyService(Settings())
    if not service.settings.cookie_file:
        raise SystemExit('请先在config.toml配置 bili.cookie_file，再运行本命令。')
    print('请从同一次B站登录复制完整Cookie和localStorage的ac_time_value。输入不会回显。')
    cookie = getpass.getpass('完整Cookie：').strip()
    token = getpass.getpass('ac_time_value：').strip()
    try:
        service.configure_bili_login(cookie, token)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print('B站配套凭据已保存；采集时将按配置检查并主动续期。未调用真实API。')


if __name__ == '__main__':
    main()
