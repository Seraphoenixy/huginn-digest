"""源码和冻结程序共用入口；业务操作仅经DailyService。"""
import argparse
import getpass
from pathlib import Path

from config import Settings
from orchestration.service import DailyService


def main(argv=None):
    parser = argparse.ArgumentParser(prog='huginn-digest', description='鸦闻 · 每日简报')
    parser.add_argument('--config', type=Path, help='本机config.toml路径')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('serve', help='启动网页与后台处理（默认）')
    commands.add_parser('web', help='启动网页与后台处理（兼容入口）')
    worker = commands.add_parser('worker', help='启动单进程后台')
    worker.add_argument('--once', action='store_true', help='运行一轮后退出（会处理已有来源和任务）')
    commands.add_parser('setup-admin', help='隐藏输入并保存管理密码')
    commands.add_parser('setup-bili', help='隐藏输入并保存配套B站凭据')
    doctor = commands.add_parser('doctor', help='离线检查依赖、资源与存储')
    doctor.add_argument('--media', action='store_true', help='使用系统FFmpeg/FFprobe验证音频分片')
    args = parser.parse_args(argv)
    try:
        settings = Settings(**({'config_file': args.config} if args.config else {}))
    except FileNotFoundError:
        # 首次运行最常见的失败：还没复制config.example.toml。
        print('未找到配置文件。请先复制 config.example.toml 为 config.toml（发行包位于app目录上一层），填写凭据后重试。')
        return 1
    except Exception as exc:
        # 配置解析异常可能带凭据；只输出固定提示与异常类型。
        print(f'操作失败（{type(exc).__name__}），请检查配置、文件权限和运行依赖。')
        return 1
    try:
        if args.command in (None, 'serve', 'web'):
            import uvicorn
            from interaction.web import create_app
            uvicorn.run(create_app(settings), host=settings.web_host, port=settings.web_port,
                        loop='asyncio', http='h11', ws='none', access_log=False, proxy_headers=False)
            return 0
        service = DailyService(settings)
        if args.command == 'worker':
            service.run_forever(once=args.once)
        elif args.command == 'setup-admin':
            password = getpass.getpass('设置管理密码（至少10位）：')
            if password != getpass.getpass('再次输入：'):
                raise ValueError('两次密码不同')
            service.configure_admin(password)
            print('管理凭据已保存，重启服务后生效。')
        elif args.command == 'setup-bili':
            if not settings.cookie_file:
                raise ValueError('请先配置 bili.cookie_file')
            cookie = getpass.getpass('同一次登录的完整Cookie：').strip()
            token = getpass.getpass('localStorage中的ac_time_value：').strip()
            service.configure_bili_login(cookie, token)
            print('B站配套凭据已保存；未调用真实API。')
        elif args.command == 'doctor':
            for result in service.check_runtime(media=args.media):
                print(result)
        return 0
    except Exception as exc:
        # 设置或第三方异常可能带凭据；只输出固定提示与异常类型。
        print(f'操作失败（{type(exc).__name__}），请检查配置、文件权限和运行依赖。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
