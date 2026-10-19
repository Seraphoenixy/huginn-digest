"""单worker进程入口，调度参数从config读取。"""
from config import Settings
from orchestration.service import DailyService

def main():
    settings=Settings()
    DailyService(settings).run_forever()

if __name__=='__main__':main()
