"""将管理凭据直接保存到本地配置，不回显密码或密钥。"""
import getpass
from config import Settings
from orchestration.credentials import configure_credentials

if __name__=='__main__':
    password=getpass.getpass('设置管理密码（至少10位）：')
    if password!=getpass.getpass('再次输入：'):raise SystemExit('两次密码不同')
    configure_credentials(Settings(),password)
    print('管理密码哈希和会话密钥已保存到 config.toml，重启网页服务后生效。')
