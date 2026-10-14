"""Local Chinese control panel and explicit confirmed operations."""
import argparse
import asyncio
import hashlib
import os
from pathlib import Path
import secrets
import socket
import sys
import time
from contextlib import asynccontextmanager

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from common import ROOT, secure_local, secret_matches
from credential_vault import Vault, VaultError
from manager_core import Supervisor, ManagerError
from mimo_manager import MiMoAssistant, diagnostic

VERSION = '0.1.0-preview'


def create_manager(root=ROOT, port=18700, supervisor=None):
    control = supervisor or Supervisor(root)
    vault = Vault(root)
    pending = {}
    session = secrets.token_urlsafe(32)

    def plan(action, name, source='human', erase=False):
        cfg = control.registry.get(name)
        if not cfg.get('managed'): raise ManagerError('现有独立实例只能查看，请使用原管理入口。')
        if action not in ('start', 'stop', 'restart', 'unbind', 'delete'): raise ManagerError('操作不受支持。')
        now = time.monotonic()
        for key in list(pending):
            if pending[key]['expires'] < now: pending.pop(key)
        if len(pending) >= 64: raise ManagerError('待确认操作过多，请先处理现有操作。')
        token = secrets.token_urlsafe(24)
        pending[token] = {'action': action, 'instance': name, 'source': source, 'erase': erase is True, 'expires': now + 180}
        return {'id': token, 'action': action, 'instance': name, 'display_name': cfg.get('display_name', name),
                'erase': erase is True, 'expires_in': 180, 'source': source}

    assistant = MiMoAssistant(vault, control, plan)

    @asynccontextmanager
    async def lifespan(app):
        async def resume():
            for cfg in control.registry.all():
                if cfg.get('managed') and cfg.get('autostart'):
                    try: await control.start(cfg['instance'])
                    except Exception: control.errors[cfg['instance']] = 'autostart_failed'
        restoration = asyncio.create_task(resume())
        yield
        restoration.cancel()
        try: await restoration
        except asyncio.CancelledError: pass
        for name in list(control.processes):
            try: await control.stop(name, save_desire=False)
            except Exception: control.audit(name, 'shutdown', 'needs_manual_review')

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.control, app.state.assistant, app.state.pending = control, assistant, pending
    app.state.version = VERSION
    secure_local(app, port)

    @app.middleware('http')
    async def manager_session(request, next_handler):
        if request.url.path.startswith('/local/') and not secret_matches(request.cookies.get('manager_session', ''), session):
            return JSONResponse({'detail': '请先打开本机管理首页。'}, status_code=401)
        if request.method == 'POST':
            try:
                if int(request.headers.get('content-length', '0')) > 65536:
                    return JSONResponse({'detail': '请求过大。'}, status_code=413)
            except ValueError: return JSONResponse({'detail': '无效请求。'}, status_code=400)
        return await next_handler(request)

    @app.exception_handler(ManagerError)
    @app.exception_handler(VaultError)
    async def expected_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(httpx.HTTPError)
    async def network_error(request, error):
        return JSONResponse({'detail': '实例连接失败，请检查服务是否启动。'}, status_code=502)

    @app.get('/')
    async def home():
        response = HTMLResponse(Path(__file__).with_name('manager.html').read_text(encoding='utf-8'))
        response.set_cookie('manager_session', session, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/health')
    async def health():
        return {'product': 'MuseWeChatManager', 'version': VERSION,
                'root_id': hashlib.sha256(str(Path(root).resolve()).lower().encode()).hexdigest()}

    @app.get('/local/overview')
    async def overview():
        return {'version': VERSION, 'instances': await control.snapshot(), 'mimo': assistant.settings(),
                'real_acceptance': '每组需完成手机端真实验收，状态不自动沿用上次运行。'}

    @app.post('/local/instances')
    async def add(request: Request):
        body = await request.json()
        cfg = control.registry.add(body.get('instance'), body.get('display_name'))
        control.audit(cfg['instance'], 'create', 'success')
        return await control.status(cfg['instance'])

    @app.post('/local/plan')
    async def make_plan(request: Request):
        body = await request.json()
        return plan(body.get('action'), body.get('instance'), erase=body.get('erase') is True)

    @app.post('/local/execute')
    async def execute(request: Request):
        body = await request.json()
        if body.get('confirm') is not True: raise HTTPException(400, '请在本机确认具体操作。')
        item = pending.pop(body.get('id'), None)
        if not item or item['expires'] < time.monotonic(): raise HTTPException(409, '操作已过期或已执行，请重新发起。')
        name, action = item['instance'], item['action']
        try:
            if action in ('start', 'stop', 'restart'): return await getattr(control, action)(name)
            async with control.lock(name):
                await control._stop(name)
                vault.delete(name + '.wechat')
                if action == 'delete':
                    vault.delete(name + '.muse')
                    control.registry.remove(name, item['erase'])
                control.audit(name, action, 'success')
                return {'ok': True, 'notice': '已清除本机绑定。撤销平台授权请在微信手机端操作。'}
        except Exception:
            control.audit(name, action, 'failed')
            raise

    async def require_owned(name):
        cfg = control.registry.get(name)
        if not cfg.get('managed') or not (await control.status(name))['owned']:
            raise ManagerError('请先从管理器启动此实例。现有独立进程只能查看。')
        await control.authenticated_state(name)

    @app.post('/local/instances/{name}/rename')
    async def rename(name, request: Request):
        if not control.registry.get(name).get('managed'):
            raise ManagerError('现有独立实例请使用原入口。')
        body = await request.json()
        control.registry.configure(name, display_name=body.get('display_name'))
        control.audit(name, 'rename', 'success')
        return {'ok': True}

    @app.post('/local/instances/{name}/login')
    async def login(name):
        await require_owned(name)
        await control.call(name, 'muse', 'login', {})
        return {'ok': True, 'notice': '请在本机弹出的专用窗口登录本实例 Muse，再确认主聊天。'}

    @app.post('/local/instances/{name}/capture')
    async def capture(name, request: Request):
        await require_owned(name)
        if (await request.json()).get('confirm_owner') is not True: raise ManagerError('请确认窗口中的 Muse 属于当前实例。')
        status = await control.status(name)
        if not status['muse_authenticated']:
            await control.call(name, 'muse', 'capture', {'confirm_owner': True})
        if not status['main_bound']:
            await control.call(name, 'muse', 'prepare-main', {}, timeout=55)
            await control.call(name, 'muse', 'bind-main', {'confirm_main': True})
        await control.call(name, 'muse', 'start-bridge', {})
        return {'ok': True, 'notice': 'Muse 与主聊天已确认，可以绑定微信。'}

    @app.post('/local/instances/{name}/qr')
    async def qr(name, request: Request):
        await require_owned(name)
        if (await request.json()).get('reviewed_ilink_flow') is not True: raise ManagerError('请先阅读微信 Bot 授权说明。')
        await control.call(name, 'muse', 'start-bridge', {})
        await control.call(name, 'bridge', 'qr', {'reviewed_ilink_flow': True})
        return {'ok': True}

    @app.get('/local/instances/{name}/qr.png')
    async def qr_image(name):
        await require_owned(name)
        r = await control.call(name, 'bridge', 'qr.png', timeout=5)
        return Response(r.content, media_type='image/png')

    @app.post('/local/instances/{name}/poll')
    async def poll(name, request: Request):
        await require_owned(name)
        if (await request.json()).get('reviewed_phone_authorization') is not True: raise ManagerError('请核对手机授权提示。')
        return (await control.call(name, 'bridge', 'poll', {'reviewed_phone_authorization': True}, timeout=55)).json()

    @app.post('/local/instances/{name}/bind')
    async def bind(name, request: Request):
        await require_owned(name)
        if (await request.json()).get('confirm_owner') is not True: raise ManagerError('请确认扫码者属于当前实例。')
        state = (await control.call(name, 'bridge', 'status', timeout=3)).json()
        if not state.get('logged_in') or state.get('bound'): raise ManagerError('请先扫码，已绑定实例需要先解绑。')
        owner = state.get('login_user')
        if not owner or owner not in state.get('pending_senders', []):
            raise ManagerError('请扫码者先给微信 Bot 发送一条消息，再点击确认绑定。')
        return (await control.call(name, 'bridge', 'bind', {'user_id': owner, 'confirm_owner': True})).json()

    @app.post('/local/instances/{name}/acceptance')
    async def acceptance(name, request: Request):
        await require_owned(name)
        body = await request.json()
        if body.get('received_on_phone') is not True or body.get('multi_turn_correct') is not True:
            raise ManagerError('请完成两轮真实收发并在手机核对后确认。')
        r = await control.call(name, 'bridge', 'confirm-results', {'received_on_phone': True, 'multi_turn_correct': True})
        return {'result': r.json()['result']}

    @app.get('/local/instances/{name}/logs')
    async def logs(name):
        if not control.registry.get(name).get('managed'): raise ManagerError('请通过原入口查看现有实例日志。')
        return (await control.call(name, 'muse', 'logs', timeout=4)).json()

    @app.get('/local/instances/{name}/diagnostic')
    async def diagnose(name): return await diagnostic(control, name)

    @app.post('/local/mimo/settings')
    async def mimo_settings(request: Request): return assistant.save(await request.json())

    @app.post('/local/mimo/ask')
    async def ask(request: Request): return await assistant.ask((await request.json()).get('text'))

    @app.post('/local/shutdown')
    async def shutdown(request: Request):
        if (await request.json()).get('confirm_stop_managed_instances') is not True:
            raise ManagerError('关闭管理器会停止由它启动的实例，请确认。')
        app.state.server.should_exit = True
        return {'ok': True}
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18700)
    args = parser.parse_args()
    sock = socket.socket()
    try: sock.bind(('127.0.0.1', args.port))
    except OSError:
        print('管理端口已占用，请打开已启动的管理器或检查占用程序。')
        raise SystemExit(2)
    sock.listen(128)
    app = create_manager(port=args.port)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, access_log=False, log_level='critical'))
    app.state.server = server
    try: server.run(sockets=[sock])
    finally: sock.close()


if __name__ == '__main__': main()
