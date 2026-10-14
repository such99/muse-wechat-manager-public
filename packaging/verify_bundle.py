"""Clean-copy integration checks against the delivered EXE, not source Python."""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import zipfile
import httpx
import psutil


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('Port must be between 1 and 65535')
    return port


def verify(archive, directory, port=18700):
    port = port_number(port)
    archive, directory = archive.resolve(), directory.resolve()
    if directory.exists(): raise ValueError('Use a new clean extraction directory')
    with zipfile.ZipFile(archive) as z: z.extractall(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    for file, expected in manifest['files'].items():
        assert hashlib.sha256((directory / file).read_bytes()).hexdigest() == expected, file
    result = {'manifest_file_hashes': 'PASS', 'source_zip_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    # Change only the disposable, hash-verified extraction, never live configs.
    # The first test instance must not compete with another installed copy.
    config_path = sorted((directory / 'instances').glob('*.json'))[0]
    config = json.loads(config_path.read_text(encoding='utf-8'))
    with ExitStack() as stack:
        for key in ('muse_port', 'bridge_port', 'cdp_port'):
            reserved = stack.enter_context(socket.socket())
            reserved.bind(('127.0.0.1', 0))
            config[key] = reserved.getsockname()[1]
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    result['isolated_test_worker_ports'] = {key: config[key] for key in ('muse_port', 'bridge_port', 'cdp_port')}
    result['manager_port'] = port
    base = f'http://127.0.0.1:{port}'
    launch_args = ['--no-browser', '--no-dialog', '--port', str(port)]
    exe = directory / 'Muse微信管理台.exe'
    owned_launcher = None
    try:
        with socket.socket() as s: s.bind(('127.0.0.1', port))
        owned_launcher = subprocess.Popen([str(exe), *launch_args], cwd=directory,
                                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with httpx.Client(trust_env=False, timeout=12) as client:
            for _ in range(80):
                if owned_launcher.poll() is not None: raise AssertionError('Launcher exited before ready')
                try:
                    r = client.get(base + '/health')
                    if r.status_code == 200: break
                except httpx.HTTPError: pass
                time.sleep(.2)
            else: raise AssertionError('Manager startup timed out')
            assert r.json()['root_id'] == hashlib.sha256(str(directory).lower().encode()).hexdigest()
            result['clean_exe_startup'] = 'PASS'
            assert client.get(base + '/local/overview').status_code == 401
            client.get(base + '/')
            headers = {'Origin': base, 'X-Local-Action': '1'}
            before = client.get(base + '/local/overview').json()
            assert len(before['instances']) == 6 and not any(r['running'] for r in before['instances'])
            result['six_clean_inactive_configurations'] = 'PASS'
            duplicate = subprocess.Popen([str(exe), *launch_args], cwd=directory,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            assert duplicate.wait(timeout=15) == 0
            result['duplicate_exe_does_not_spawn_second_manager'] = 'PASS'
            name = before['instances'][0]['instance']
            def action(verb):
                p = client.post(base + '/local/plan', headers=headers, json={'instance': name, 'action': verb}).json()
                response = client.post(base + '/local/execute', headers=headers, json={'id': p['id'], 'confirm': True}, timeout=60)
                assert response.status_code == 200, response.text
                return response.json()
            action('start')
            result['bundled_worker_startup'] = 'PASS'
            action('restart')
            result['bundled_worker_restart'] = 'PASS'
            assert client.post(base + '/local/plan', headers={'Origin': 'https://evil.example', 'X-Local-Action': '1'}, json={}).status_code == 403
            result['cross_site_mutation_denied'] = 'PASS'
            # Stop manager, preserving autostart desire; no login, browser or API calls.
            assert client.post(base + '/local/shutdown', headers=headers, json={'confirm_stop_managed_instances': True}).status_code == 200
            assert owned_launcher.wait(timeout=60) == 0
            result['graceful_exe_shutdown'] = 'PASS'
            owned_launcher = subprocess.Popen([str(exe), *launch_args], cwd=directory,
                                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for _ in range(100):
                try:
                    client.get(base + '/')
                    overview = client.get(base + '/local/overview').json()
                    if any(r['instance'] == name and r['owned'] and r['running'] for r in overview['instances']): break
                except (httpx.HTTPError, KeyError): pass
                time.sleep(.2)
            else: raise AssertionError('Autostart after relaunch failed')
            result['autostart_after_exe_relaunch'] = 'PASS'
            assert client.post(base + '/local/shutdown', headers=headers, json={'confirm_stop_managed_instances': True}).status_code == 200
            assert owned_launcher.wait(timeout=60) == 0
            with socket.socket() as s: s.bind(('127.0.0.1', port))
            result['manager_port_released'] = 'PASS'
        # The diagnostic flag exercises the same error branches without a modal.
        missing = directory / 'missing-runtime-check'
        missing.mkdir()
        missing_exe = missing / exe.name
        shutil.copy2(exe, missing_exe)
        check = subprocess.run([str(missing_exe), *launch_args],
            capture_output=True, timeout=10)
        assert check.returncode == 3 and check.stderr
        result['missing_runtime_launcher_error'] = 'PASS'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))
            listener.listen()
            conflict = subprocess.run([str(exe), *launch_args],
                capture_output=True, timeout=15)
            assert conflict.returncode == 4 and conflict.stderr
            assert listener.fileno() != -1
            with socket.socket() as probe: probe.connect(('127.0.0.1', port))
        result['launcher_port_collision_keeps_existing_listener'] = 'PASS'
        for invalid in ('0', '65536', 'invalid'):
            check = subprocess.run([str(exe), '--no-browser', '--no-dialog', '--port', invalid],
                capture_output=True, timeout=10)
            assert check.returncode == 6 and check.stderr
        result['invalid_launcher_port_rejected'] = 'PASS'
        python = directory / 'runtime-python' / 'python.exe'
        check = subprocess.run([str(python), '-I', '-c',
            'import fastapi,httpx,requests,psutil,qrcode,PIL,Crypto,websocket,uvicorn,websockets;print("imports_ok")'],
            capture_output=True, text=True, timeout=10)
        assert check.returncode == 0 and 'imports_ok' in check.stdout, check.stderr
        result['bundled_native_dependencies_import'] = 'PASS'
    finally:
        if owned_launcher and owned_launcher.poll() is None:
            # Only this verified launcher and descendants can be force-stopped.
            owner = psutil.Process(owned_launcher.pid)
            children = owner.children(recursive=True)
            for p in reversed(children):
                try: p.terminate()
                except psutil.NoSuchProcess: pass
            owned_launcher.terminate()
    result['real_Muse_WeChat_MiMo'] = 'NOT_RUN_requires_human_login_and_local_API_key'
    (directory / 'bundle-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--port', type=port_number, default=18700,
                        help='Unused local manager port (default: 18700)')
    args = parser.parse_args()
    verify(args.archive, args.directory, args.port)
