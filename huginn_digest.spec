# 只收集明确的程序资源；不递归复制仓库、配置、Cookie或data。
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / 'interaction' / 'templates'), 'interaction/templates'),
         (str(root / 'interaction' / 'static'), 'interaction/static'),
         (str(root / 'knowledge' / 'prompts'), 'knowledge/prompts')]
hidden = []
for package in ('langgraph', 'yt_dlp', 'uvicorn.logging', 'uvicorn.loops',
                'uvicorn.protocols.http', 'uvicorn.lifespan'):
    hidden += collect_submodules(package)
for package in ('trafilatura', 'justext'):
    datas += collect_data_files(package)
for distribution in ('langgraph', 'langgraph-checkpoint', 'langgraph-prebuilt',
                     'langgraph-sdk', 'langchain-core', 'yt-dlp', 'trafilatura'):
    datas += copy_metadata(distribution)

a = Analysis([str(root / 'huginn_digest.py')], pathex=[str(root)], binaries=[],
             datas=datas, hiddenimports=hidden,
             excludes=['dotenv', 'torch', 'transformers', 'tensorflow', 'tkinter',
                       'pytest', 'IPython', 'matplotlib', 'pandas', 'scipy'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='huginn-digest',
          debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='app')
