"""Linux/WSL共用构建入口；发行包只包含明确白名单。"""
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('本构建目标需要x86_64 Linux（可使用WSL或GitHub Actions）。')
    libc, version = platform.libc_ver()
    if libc != 'glibc' or tuple(int(part) for part in version.split('.')[:2]) > (2, 35):
        raise SystemExit('本发行包面向glibc 2.35及以上系统，请使用Ubuntu22.04构建。')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    str(ROOT / 'huginn_digest.spec')], cwd=ROOT, check=True)
    output = ROOT / 'dist'
    stage = output / 'huginn-digest-linux-x86_64'
    # 不覆盖旧产物；使用独立目录，方便比较和回滚。
    stage.mkdir(exist_ok=False)
    shutil.copytree(output / 'app', stage / 'app', symlinks=True)
    shutil.copytree(ROOT / 'deploy', stage / 'deploy')
    shutil.copy2(ROOT / 'config.example.toml', stage)
    shutil.copy2(ROOT / 'README.md', stage)
    (stage / 'docs').mkdir()
    for document in ('architecture.md', 'validation.md'):
        shutil.copy2(ROOT / 'docs' / document, stage / 'docs')
    shutil.copy2(ROOT / 'AGENTS.md', stage)
    # 仅记录依赖名/版本，不写pip环境、索引地址或部署凭据。
    from importlib.metadata import distributions
    dependencies = sorted({f"{d.metadata['Name']}=={d.version}" for d in distributions()})
    (stage / 'build-info.json').write_text(json.dumps({
        'python': platform.python_version(), 'platform': platform.platform(),
        'dependencies': dependencies}, indent=2), encoding='utf-8')
    archive = output / (stage.name + '.tar.gz')
    with tarfile.open(archive, 'w:gz', dereference=False) as tar:
        tar.add(stage, arcname=stage.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + '.sha256')).write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(archive.name)


if __name__ == '__main__':
    main()
