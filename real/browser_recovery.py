"""Same-instance browser recovery and explicit RAM-only Bridge handoff.

Require exact entrypoint, creation time, ancestry, loopback and shared RAM key.
No credential is serialized. A listening port alone never proves ownership.
"""
import hashlib
import secrets
import time
from pathlib import Path
import psutil
from common import ROOT
from message_policy import ChatFailure
from native_chat import SCRIPT

OWNER_EXPR = '(' + SCRIPT.replace('return {probe, bind, diagnose};',
    'return {owner:()=>{const c=discover();return JSON.stringify([c.auth.userId,c.state.activity.currentChatRootAgentId??null]);}};') + ').owner()'


def engine_owner(engine):
    result=engine.page.send('Runtime.evaluate',{'expression':OWNER_EXPR,'returnByValue':True},timeout=30)['result']
    if result.get('exceptionDetails') or not isinstance(result.get('result',{}).get('value'),str):
        raise ValueError('Native durable owner unavailable')
    return result['result']['value']


def verify_owner(native, expected):
    identity = native._evaluate(OWNER_EXPR)
    if (not isinstance(identity, str) or not expected or
            hashlib.sha256(identity.encode()).hexdigest() != expected):
        raise ChatFailure('recovery_owner_mismatch',
            '当前 Muse 账号与本实例原账号不一致，未恢复转发；请登录原账号。')


def recovery_idle(native, feed):
    head = feed.store.head()
    if native.unresolved or feed.submission_uncertain or feed.store.unresolved_input():
        raise ChatFailure('submission_unresolved', '上一条提交未确认，请先核对主要聊天；没有自动重发。')
    if native.halted_reason or feed.generation_halted:
        raise ChatFailure('native_generation_failed', '请先核查此前生成失败；不能通过恢复浏览器绕过保护。')
    if head and head['state'] == 'sending':
        raise ChatFailure('send_result_unknown', '仍有发送中的消息，请先核查送达情况。')


def open_recovery(engine, native, feed):
    recovery_idle(native, feed)
    if engine.proc and engine.proc.poll() is None:
        raise ChatFailure('browser_still_running', '专用浏览器仍在运行，请先检查原窗口，不重复创建会话。')
    if any(c.status == psutil.CONN_LISTEN and c.laddr.port == engine.cfg.cdp_port
           for c in psutil.net_connections(kind='tcp')):
        raise ChatFailure('cdp_port_occupied', 'CDP 端口仍被占用，请核查进程归属。')
    feed.enabled = False
    feed.error = 'browser_reauthentication_required'
    native.close()
    for connection in (engine.page, engine.browser):
        if connection:
            connection.close()  # No dispose commands over a dead CDP socket.
    engine.page = engine.browser = engine.proc = None
    engine.current_context_id = None
    engine._active_contexts.clear()
    engine.browser_verified = False
    native.page = native.context_id = native.target_id = None
    engine.login_window()


def exact_process(pid, created, script, instance):
    process = psutil.Process(pid)
    args = process.cmdline()
    if (not process.is_running() or process.create_time() != created or
            Path(process.cwd()).resolve() != ROOT.resolve() or
            len(args) != 4 or args[1:] != ['-I', str(ROOT / 'real' / script), instance] or
            Path(process.exe()).name.lower() not in ('python.exe', 'pythonw.exe')):
        raise ValueError('Retained process identity mismatch')
    return process


class RetainedBridge:
    """Popen-compatible handle for the verified pre-existing Bridge tree."""
    def __init__(self, settings, record, key):
        self.settings, self.record = settings, dict(record)
        if record.get('instance') != settings['instance'] or not key:
            raise ValueError('Retained instance rejected')
        self.pid = record['supervised_pid']
        serving, launcher = self.processes()
        if serving.pid != launcher.pid and serving.ppid() != launcher.pid:
            raise ValueError('Retained redirector ancestry changed')
        if launcher.ppid() != record['previous_muse_pid']:
            raise ValueError('Original Muse ancestry changed')
        rows = [c for c in psutil.net_connections(kind='tcp')
                if c.status == psutil.CONN_LISTEN and c.laddr.port == settings['bridge_port']]
        if len(rows) != 1 or rows[0].laddr.ip != '127.0.0.1' or rows[0].pid != serving.pid:
            raise ValueError('Retained listener mismatch')
        shared = serving.environ().get('MUSE2API_API_KEY', '')
        if not secrets.compare_digest(shared, key):
            raise ValueError('Retained runtime key mismatch')

    def processes(self):
        r = self.record
        serving = exact_process(r['serving_pid'], r['serving_created'], 'bridge_service.py', self.settings['instance'])
        launcher = exact_process(r['supervised_pid'], r['supervised_created'], 'bridge_service.py', self.settings['instance'])
        if serving.pid != launcher.pid and not Path(launcher.exe()).samefile(ROOT / '.venv/Scripts/python.exe'):
            raise ValueError('Retained launcher executable mismatch')
        return serving, launcher

    def poll(self):
        r = self.record
        for pid, created in {(r['serving_pid'], r['serving_created']),
                             (r['supervised_pid'], r['supervised_created'])}:
            try:
                exact_process(pid, created, 'bridge_service.py', self.settings['instance'])
                return None
            except (psutil.Error, ValueError, OSError):
                continue
        return 0

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('Retained Bridge did not exit')
            time.sleep(.1)
        return 0

    def terminate(self):
        r = self.record
        processes = []
        for pid, created in dict.fromkeys(((r['serving_pid'], r['serving_created']),
                                          (r['supervised_pid'], r['supervised_created']))):
            try:
                processes.append(exact_process(pid, created, 'bridge_service.py', self.settings['instance']))
            except psutil.NoSuchProcess:
                continue
        # Validate every remaining PID before terminating any of them.
        for process in processes:
            process.terminate()


def owned_bridge(settings, muse, bridge, muse_status):
    if bridge.pid in {p.pid for p in muse.children(recursive=True)}:
        return
    record = muse_status.get('retained_bridge')
    if not isinstance(record, dict):
        raise ValueError('Bridge is not supervised by this instance')
    retained = RetainedBridge(settings, record, muse.environ().get('MUSE2API_API_KEY', ''))
    if retained.record['serving_pid'] != bridge.pid:
        raise ValueError('Wrong retained listener')
