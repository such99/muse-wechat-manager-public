"""Instance configuration and owned process supervision; never operate by PID alone."""
import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
from windows_job import OwnedJob

NAME = re.compile(r'[a-z][a-z0-9_-]{0,30}')


class ManagerError(ValueError):
    pass


def atomic_json(path, data):
    pending = path.with_suffix('.new')
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(pending, path)


class Registry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'instances'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def get(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ManagerError('实例 ID 只能使用小写字母、数字、下划线和连字符。')
        path = self.directory / (name + '.json')
        if path.is_symlink() or not path.is_file():
            raise ManagerError('实例不存在。')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('instance') != name:
            raise ManagerError('实例配置不匹配。')
        return data

    def all(self):
        return [self.get(p.stem) for p in sorted(self.directory.glob('*.json'))]

    def add(self, name, label):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ManagerError('实例 ID 需以小写字母开头，最长 31 字符。')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
            raise ManagerError('请填写 1 至 60 字的实例名称。')
        with self.lock:
            path = self.directory / (name + '.json')
            if path.exists():
                raise ManagerError('实例 ID 已存在。')
            if (self.root / 'runtime' / name).exists():
                raise ManagerError('该 ID 仍有保留数据，请使用新的实例 ID。')
            used = {cfg[k] for cfg in self.all() for k in ('muse_port', 'bridge_port', 'cdp_port')}
            ports = []
            for port in range(20000, 50000):
                if port in used:
                    continue
                try:
                    with socket.socket() as check:
                        check.bind(('127.0.0.1', port))
                    ports.append(port)
                except OSError:
                    continue
                if len(ports) == 3:
                    break
            if len(ports) != 3:
                raise ManagerError('没有可用端口。')
            data = dict(zip(('muse_port', 'bridge_port', 'cdp_port'), ports))
            data.update(instance=name, display_name=label.strip(), site='https://muse.ai/', model='muse-spark',
                        managed=True, autostart=False, credential_storage='windows_dpapi',
                        routing={'mode': 'native_main', 'forward_all': True},
                        logging={'enabled': True, 'chat_bodies': False})
            atomic_json(path, data)
            return data

    def configure(self, name, **changes):
        if set(changes) - {'autostart', 'display_name'}:
            raise ManagerError('不允许修改该配置项。')
        if 'display_name' in changes:
            value = changes['display_name']
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
                raise ManagerError('实例名称需为 1 至 60 字。')
            changes['display_name'] = value.strip()
        with self.lock:
            data = self.get(name)
            data.update(changes)
            atomic_json(self.directory / (name + '.json'), data)
            return data

    def remove(self, name, erase):
        with self.lock:
            cfg = self.get(name)
            if not cfg.get('managed'):
                raise ManagerError('现有独立运行实例需要人工迁移，不能在管理器内删除。')
            target = self.root / 'runtime' / name
            if target.is_symlink() or target.is_junction() or target.resolve().parent != (self.root / 'runtime').resolve():
                raise ManagerError('实例数据路径不安全。')
            if erase and target.exists():
                # Refuse reparse points anywhere below the verified instance root.
                if any(p.is_symlink() or p.is_junction() for p in target.rglob('*')):
                    raise ManagerError('数据目录包含链接，请人工检查。')
                shutil.rmtree(target)
            (self.directory / (name + '.json')).unlink()


class Supervisor:
    def __init__(self, root, code_root=None):
        self.registry = Registry(root)
        self.root = self.registry.root
        self.code_root = Path(code_root or Path(__file__).resolve().parents[1]).resolve()
        self.processes = {}
        self.locks = {}
        self.errors = {}

    def lock(self, name):
        return self.locks.setdefault(name, asyncio.Lock())

    def audit(self, name, action, result):
        directory = self.root / 'manager-data'
        directory.mkdir(exist_ok=True)
        with (directory / 'audit.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'time': int(time.time()), 'instance': name,
                                    'action': action, 'result': result}) + '\n')

    async def call(self, name, service, action, body=None, timeout=50):
        cfg = self.registry.get(name)
        if service not in ('muse', 'bridge'):
            raise ManagerError('无效服务。')
        url = f"http://127.0.0.1:{cfg[service + '_port']}"
        # Only fixed plain-HTTP loopback URLs: do not load Windows TLS roots for
        # every inactive instance on each dashboard refresh.
        async with httpx.AsyncClient(trust_env=False, verify=False, timeout=timeout) as client:
            if body is None:
                response = await client.get(url + '/local/' + action)
            else:
                response = await client.post(url + '/local/' + action, json=body,
                    headers={'Origin': url, 'X-Local-Action': '1'})
        if response.status_code >= 400:
            # Do not echo a remote body that could contain credential diagnostics.
            code = None
            try: code = response.json().get('code')
            except (ValueError, AttributeError): pass
            notices = {'browser_component_missing': '服务器未找到 Google Chrome，请安装 Chrome，或用 MUSE_CHROMIUM 指定安装位置。',
                       'recovery_owner_mismatch': '本实例登录的 Muse 不是原账号，请重新登录原账号。',
                       'authentication_failed': '本实例 Muse 认证已过期，请重新登录原账号。',
                       'wechat_reconnect_required': '微信授权已失效，请先点击本实例“重连”，再重新扫码。'}
            if isinstance(code, str) and code in notices: raise ManagerError(notices[code])
            raise ManagerError(f'操作未完成（HTTP {response.status_code}），请查看本实例登录或绑定状态。')
        return response

    async def status(self, name):
        cfg = self.registry.get(name)
        result = {'instance': name, 'display_name': cfg.get('display_name', name),
                  'managed': cfg.get('managed') is True, 'autostart': cfg.get('autostart') is True,
                  'owned': name in self.processes and self.processes[name]['process'].poll() is None,
                  'running': False, 'muse_authenticated': False, 'wechat_connected': False,
                  'wechat_user_bound': False, 'real_roundtrip_verified': False,
                  'main_bound': False, 'last_error': self.errors.get(name),
                  'muse_url': f"http://127.0.0.1:{cfg['muse_port']}/",
                  'wechat_url': f"http://127.0.0.1:{cfg['bridge_port']}/"}
        try:
            state = (await self.call(name, 'muse', 'status', timeout=2)).json()
            if state.get('instance') != name:
                raise ManagerError('端口属于另一个实例。')
            result.update(running=True, main_bound=bool(state.get('native_main', {}).get('bound')),
                          last_error=state.get('last_error') or result['last_error'],
                          recovery=state.get('credential_recovery', 'not_configured'))
            for k in ('muse_authenticated', 'wechat_connected', 'wechat_user_bound', 'real_roundtrip_verified'):
                result[k] = state.get('stages', {}).get(k) is True
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return result

    async def authenticated_state(self, name):
        """A managed service must prove its per-launch nonce before mutation."""
        record = self.processes.get(name)
        if not record or record['process'].poll() is not None:
            raise ManagerError('请先从管理器启动此实例。')
        state = (await self.call(name, 'muse', 'status', timeout=3)).json()
        if state.get('manager_nonce') != record['nonce'] or state.get('instance') != name:
            raise ManagerError('实例端口归属发生变化，已拒绝操作。')
        return state

    async def _start(self, name):
        cfg = self.registry.get(name)
        if not cfg.get('managed'):
            raise ManagerError('这是现有独立运行实例。请使用原入口；管理器不会接管或重启它。')
        if name in self.processes and self.processes[name]['process'].poll() is None:
            return await self.status(name)
        reservations = []
        try:
            for k in ('muse_port', 'bridge_port', 'cdp_port'):
                s = socket.socket()
                reservations.append(s)
                s.bind(('127.0.0.1', cfg[k]))
        except OSError:
            raise ManagerError('实例端口已占用，请检查冲突；没有停止任何现有进程。') from None
        finally:
            for s in reservations:
                s.close()
        nonce = secrets.token_hex(24)
        # No inherited AI credentials, browser profiles, or recovery handles.
        env = {k: v for k, v in os.environ.items()
               if not any(x in k.upper() for x in ('TOKEN', 'COOKIE', 'SECRET', 'PASSWORD', 'API_KEY'))
               and k not in ('MUSE_RETAINED_BRIDGE', 'PYTHONPATH')}
        env.update(MUSE_APP_ROOT=str(self.root), MUSE_MANAGER_NONCE=nonce)
        p = subprocess.Popen([sys.executable, '-I', str(self.code_root / 'real' / 'muse_service.py'), name],
                             env=env, cwd=self.code_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try: job = OwnedJob(p)
        except OSError:
            p.terminate()
            await asyncio.to_thread(p.wait, 5)
            raise ManagerError('无法建立实例进程隔离，请检查 Windows 运行权限。') from None
        self.processes[name] = {'process': p, 'nonce': nonce, 'created': psutil.Process(p.pid).create_time(), 'job': job}
        for _ in range(80):
            if p.poll() is not None:
                break
            try:
                state = (await self.call(name, 'muse', 'status', timeout=1)).json()
                if state.get('manager_nonce') == nonce and state.get('instance') == name:
                    self.registry.configure(name, autostart=True)
                    self.errors.pop(name, None)
                    self.audit(name, 'start', 'success')
                    return await self.status(name)
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(.15)
        await self._stop(name, save_desire=False)
        self.errors[name] = 'startup_failed'
        raise ManagerError('服务启动失败，请检查软件依赖和端口。')

    async def start(self, name):
        async with self.lock(name):
            return await self._start(name)

    async def _stop(self, name, save_desire=True):
        record = self.processes.get(name)
        if not record:
            if (await self.status(name))['running']:
                raise ManagerError('此进程由其他入口启动，管理器没有停止权限。')
            if save_desire:
                self.registry.configure(name, autostart=False)
            return {'ok': True}
        p = record['process']
        descendants = []
        if p.poll() is None:
            try:
                owner = psutil.Process(p.pid)
                if owner.create_time() != record['created']:
                    raise ManagerError('进程身份变化，已拒绝停止。')
                descendants = owner.children(recursive=True)
                state = (await self.call(name, 'muse', 'status', timeout=2)).json()
                if state.get('manager_nonce') != record['nonce']:
                    raise ManagerError('服务归属变化，已拒绝停止。')
                await self.call(name, 'muse', 'stop', {}, timeout=2)
            except httpx.HTTPError:
                pass
            try:
                await asyncio.to_thread(p.wait, 55)
            except subprocess.TimeoutExpired:
                p.terminate()
                await asyncio.to_thread(p.wait, 5)
        for child in reversed(descendants):
            try:
                # psutil Process retains creation time and rejects reused PIDs.
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = await asyncio.to_thread(psutil.wait_procs, descendants, timeout=3)
        for child in alive:
            try: child.kill()
            except psutil.NoSuchProcess: pass
        record['job'].close()
        self.processes.pop(name, None)
        if save_desire:
            self.registry.configure(name, autostart=False)
        self.audit(name, 'stop', 'success')
        return {'ok': True}

    async def stop(self, name, save_desire=True):
        async with self.lock(name):
            return await self._stop(name, save_desire)

    async def restart(self, name):
        async with self.lock(name):
            await self._stop(name, save_desire=False)
            result = await self._start(name)
            self.audit(name, 'restart', 'success')
            return result

    async def snapshot(self):
        return await asyncio.gather(*(self.status(c['instance']) for c in self.registry.all()))
