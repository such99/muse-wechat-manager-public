"""Whitelist release assembly. Never copy live runtime, profiles, config or secrets."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.0-preview-r10'
PYTHON_URL = 'https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip'
PYTHON_SHA = '90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907'
REAL = '''common.py credential_vault.py manager_core.py manager_service.py mimo_manager.py windows_job.py
manager.html muse_service.py bridge_service.py instance_pages.py strict_engine.py journal.py native_chat.py native_policy.py
native_ui.js native_feed.js main_feed.py feed_store.py feed_bridge.py incoming_queue.py
message_policy.py media_memory.py browser_recovery.py portal.html wechat.html'''.split()
VENDOR = {'muse2api-live': ['engine.py', 'cdp.py', 'LICENSE'],
          'WeChat-Bridge': ['app/ilink.py', 'app/version.py', 'app/ai_chat.py', 'app/media.py', 'LICENSE']}
RUNTIME_REQUIREMENTS = ['fastapi', 'uvicorn', 'httpx', 'requests', 'psutil', 'qrcode', 'pillow',
                        'pycryptodome', 'websocket-client', 'python-multipart', 'websockets']


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def distributions():
    todo, found = list(RUNTIME_REQUIREMENTS), {}
    while todo:
        name = todo.pop().lower().replace('_', '-')
        if name in found: continue
        dist = metadata.distribution(name)
        found[name] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({'extra': ''}): continue
            todo.append(req.name)
    return found


def build(destination):
    destination = destination.resolve()
    if destination.exists(): raise ValueError('Use a NEW empty release path; existing data is never overwritten')
    locked_dependencies = {Requirement(line).name.lower().replace('_', '-'): Requirement(line)
                           for line in (ROOT / 'requirements-dev.lock.txt').read_text().splitlines()
                           if line.strip() and not line.lstrip().startswith('#')}
    runtime_distributions = distributions()
    for name, dist in runtime_distributions.items():
        requirement = locked_dependencies.get(name)
        if not requirement or dist.version not in requirement.specifier:
            raise ValueError('Installed dependency differs from requirements-dev.lock.txt: ' + name)
    destination.mkdir(parents=True)
    archive = ROOT / 'build-cache' / 'python-3.13.14-embed-amd64.zip'
    if sha(archive) != PYTHON_SHA: raise ValueError('Official Python archive SHA mismatch')
    with zipfile.ZipFile(archive) as z: z.extractall(destination / 'runtime-python')
    runtime = destination / 'runtime-python'
    (runtime / 'python313._pth').write_text('python313.zip\n.\nLib/site-packages\nimport site\n')
    licenses = destination / 'licenses'
    licenses.mkdir()
    shutil.copy2(runtime / 'LICENSE.txt', licenses / 'Python-LICENSE.txt')
    for file in REAL:
        src = ROOT / 'real' / file
        if not src.is_file(): raise ValueError('Missing runtime source: ' + file)
        dst = destination / 'real' / file
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    locked = json.loads((ROOT / 'packaging' / 'upstream-lock.json').read_text(encoding='utf-8'))
    upstream = []
    for repo, files in VENDOR.items():
        for file in files:
            src = ROOT / 'vendor' / repo / file
            dst = destination / 'vendor' / repo / file
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        record = next(item for item in locked if item['name'] == repo)
        # Distributed source contains the selected upstream files, never .git.
        # Pin both provenance and content so a source-only rebuild stays verifiable.
        for file in files:
            if sha(ROOT / 'vendor' / repo / file) != record['files'][file]:
                raise ValueError('Upstream source differs from locked content: ' + repo + '/' + file)
        upstream.append(record)
    deps = []
    site = runtime / 'Lib' / 'site-packages'
    for name, dist in sorted(runtime_distributions.items()):
        count = 0
        for file in dist.files or []:
            if '..' in file.parts or '__pycache__' in file.parts or file.suffix == '.pyc': continue
            src = Path(dist.locate_file(file))
            if not src.is_file(): continue
            dst = site / str(file)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
        deps.append({'name': dist.metadata['Name'], 'version': dist.version, 'files': count,
                     'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'),
                     'license_files': [str(Path('runtime-python/Lib/site-packages') / str(f)).replace('\\', '/')
                                       for f in dist.files or [] if '..' not in f.parts
                                       and any(word in f.name.upper() for word in ('LICENSE', 'NOTICE', 'COPYING'))]})
        # dist-info including upstream LICENSE/NOTICE files accompanies each library.
    notices = ['# 第三方组件与许可证', '',
               'Python 3.13.14：`licenses/Python-LICENSE.txt`。',
               'Muse2API 与 WeChat-Bridge：各自 `vendor/<项目>/LICENSE`；来源及固定提交见 manifest。', '',
               '以下记录随包实际依赖。授权全文保留在对应目录；不包含浏览器再分发。', '']
    for dep in deps:
        notices.append('## ' + dep['name'] + ' ' + dep['version'])
        notices.append('')
        notices.extend('可查授权文件：`' + file + '`' for file in dep['license_files'])
        if not dep['license_files']:
            notices.append('授权声明见对应 `.dist-info/METADATA`。')
        notices.append('')
    (destination / 'THIRD-PARTY-NOTICES.md').write_text('\n'.join(notices), encoding='utf-8')
    (destination / 'instances').mkdir()
    sys.path.insert(0, str(ROOT / 'real'))
    from manager_core import Registry
    registry = Registry(destination)
    for i in range(1, 7): registry.add(f'member{i:02}', f'成员 {i:02}')
    for file in ('USER-GUIDE.md', 'TEST-REPORT.md', 'ACCEPTANCE.md'):
        shutil.copy2(ROOT / 'packaging' / file, destination / file)
    shutil.copy2(ROOT / 'packaging' / 'Launcher.cs', destination / 'Launcher.cs')
    package_sources = destination / 'packaging'
    package_sources.mkdir()
    for file in ('build.py', 'verify_bundle.py', 'Launcher.cs', 'USER-GUIDE.md', 'TEST-REPORT.md', 'ACCEPTANCE.md', 'upstream-lock.json'):
        shutil.copy2(ROOT / 'packaging' / file, package_sources / file)
    # Ship the same portable fake-data regression suite as the source checkout.
    for test in sorted((ROOT / 'real').glob('test_*')):
        if test.is_file() and test.suffix in ('.py', '.cjs'):
            shutil.copy2(test, destination / 'real' / test.name)
    for document in ('README.md', 'RIGHTS.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md'):
        shutil.copy2(ROOT / document, destination / document)
    (destination / 'docs').mkdir()
    for document in sorted((ROOT / 'docs').glob('*.md')):
        shutil.copy2(document, destination / 'docs' / document.name)
    shutil.copy2(ROOT / 'requirements-dev.lock.txt', destination / 'requirements-dev.lock.txt')
    compiler = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/optimize+',
                    '/reference:System.Windows.Forms.dll', '/out:' + str(destination / 'Muse微信管理台.exe'),
                    str(ROOT / 'packaging' / 'Launcher.cs')], check=True)
    manifest = {'version': VERSION, 'python': {'url': PYTHON_URL, 'sha256': PYTHON_SHA},
                'compiler': {'path': str(compiler), 'sha256': sha(compiler)},
                'upstream': upstream, 'dependencies': deps,
                'rebuild': '.venv/Scripts/python.exe -I packaging/build.py --output <NEW_DIRECTORY> (see USER-GUIDE.md)',
                'browser': 'Uses installed Google Chrome; no browser binaries redistributed',
                'files': {str(p.relative_to(destination)).replace('\\', '/'): sha(p)
                          for p in destination.rglob('*') if p.is_file()}}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    # Structural release scan: no copied personal runtime, credentials, profiles or logs.
    forbidden = {'secrets', 'manager-data', 'runtime', 'blank-browser-profile', '.git', '.env'}
    for p in destination.rglob('*'):
        parts = set(p.relative_to(destination).parts)
        if parts & forbidden or p.suffix in ('.dpapi', '.sqlite3', '.log', '.jsonl'):
            raise ValueError('Private data in release: ' + str(p.relative_to(destination)))
    output = destination.parent / (destination.name + '.zip')
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(destination.rglob('*')):
            if p.is_file(): z.write(p, str(p.relative_to(destination)))
    print(json.dumps({'release': str(destination), 'zip': str(output), 'zip_sha256': sha(output),
                      'dependencies': len(deps), 'files': len(manifest['files'])}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
