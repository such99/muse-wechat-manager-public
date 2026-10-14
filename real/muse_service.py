"""Single-owner Muse API and local human authentication portal."""
import asyncio
import functools
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, CODE_ROOT, OneAccount, load_instance, quiet, secure_local, persist_routing
from journal import InstanceJournal
from strict_engine import StrictEngine, MuseAuthError, authentication_failure_code
from native_chat import NativeMainChat
from main_feed import MainFeed
from media_memory import decode_uploads
from message_policy import ChatFailure
from browser_recovery import RetainedBridge, verify_owner, open_recovery, recovery_idle, engine_owner
from credential_vault import Vault, VaultError


def build_prompt(messages):
    if len(messages) == 1 and messages[0]['role'] == 'user':
        return messages[0]['content']
    labels = {'system': '背景与任务设定', 'user': '用户', 'assistant': '助手'}
    return '\n\n'.join(labels[m['role']] + '：\n' + m['content'] for m in messages)


def create_service(settings, engine=None):
    prefix = 'MUSE_' + settings['instance'].upper().replace('-', '_')
    key = os.environ.get(prefix + '_API_KEY') or secrets.token_urlsafe(32)
    os.environ['MUSE2API_API_KEY'] = key
    account = OneAccount(settings['instance'], prefix + '_COOKIES_JSON')
    if os.environ.get(prefix + '_COOKIES_JSON'):
        try:
            account.inject(json.loads(os.environ.pop(prefix + '_COOKIES_JSON')))
        except Exception:
            raise ValueError('Invalid runtime Cookie input') from None
    engine = engine or StrictEngine(settings)
    native = NativeMainChat(engine)
    routing_mode = settings.get('routing',{}).get('mode','branch_replay')
    journal = InstanceJournal(settings, 'muse', lambda: (key, *account.cookies.values()))
    feed = MainFeed(native,settings,journal,lambda:(key,*account.cookies.values()))
    journal.event('service.started')
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=settings['instance'])
    busy = asyncio.Lock()
    state = {'instance': settings['instance'], 'authenticated': account.available,
        'owner_confirmed': False, 'real_test': 'not_run', 'bridge_started': False,
        'backend': 'czg86389-hub/muse2api/engine.py', 'model': 'muse-spark',
        'api_requests_completed': 0, 'last_error': None}
    retained_raw = os.environ.pop('MUSE_RETAINED_BRIDGE', None)
    retained_record = json.loads(retained_raw) if retained_raw else None
    bridge = RetainedBridge(settings, retained_record, key) if retained_record else None
    recovery = {'pending': False, 'expected_owner': retained_record.get('owner_fingerprint') if retained_record else None}
    vault = Vault(ROOT) if settings.get('credential_storage') == 'windows_dpapi' else None
    state['manager_nonce'] = os.environ.get('MUSE_MANAGER_NONCE')
    state['credential_recovery'] = 'not_saved' if vault else 'memory_only'
    saved_owner = None

    def save_login():
        nonlocal saved_owner
        if not vault or not native.bound or not account.available:
            return
        identity = engine_owner(engine)
        owner = hashlib.sha256(identity.encode()).hexdigest()
        account_owner = hashlib.sha256(str(json.loads(identity)[0]).encode()).hexdigest()
        if saved_owner and owner != saved_owner:
            raise ValueError('Instance owner changed')
        vault.write(settings['instance'] + '.muse',
                    {'cookies': account.cookies, 'expires': account.expires, 'owner': owner, 'account_owner': account_owner})
        saved_owner = owner
        state['credential_recovery'] = 'saved'

    async def restore_login():
        nonlocal saved_owner
        if not vault:
            return
        try:
            saved = vault.read(settings['instance'] + '.muse')
            if not saved:
                return
            saved_owner = saved['owner']
            recovery['expected_owner'] = saved_owner
            state['credential_recovery'] = 'restoring'
            def restore():
                engine.login_window()
                engine._apply_cookies(engine.page, saved['cookies'], saved['expires'])
                engine.page.send('Page.navigate', {'url': settings['site']})
                native.prepare()
                verify_owner(native, saved_owner)
                values, expires = engine.capture()
                account.inject(values, expires)
                native.bind()
                if settings.get('routing', {}).get('forward_all'):
                    feed.enable()
                save_login()
            await work(restore)
            state.update(authenticated=True, owner_confirmed=True, credential_recovery='restored', last_error=None)
            await start_bridge()
        except Exception:
            account.invalidate()
            state.update(authenticated=False, owner_confirmed=False, credential_recovery='login_required',
                         last_error='saved_session_needs_login')
            # Never replace another instance, replay input, or silently retry login.


    async def work(fn, *args):
        async with busy:
            return await asyncio.get_running_loop().run_in_executor(executor, functools.partial(fn, *args))

    @asynccontextmanager
    async def lifespan(app):
        restoration = asyncio.create_task(restore_login())
        async def keepalive():
            while True:
                await asyncio.sleep(60)
                if account.available and state['owner_confirmed'] and time.time() - engine._last_http_renew.get(account.identity, 0) >= 6 * 3600:
                    try:
                        await work(engine.renew_for_account, account)
                        await work(save_login)
                    except MuseAuthError as error:
                        account.invalidate()
                        state.update(authenticated=False, last_error=authentication_failure_code(error))
                    except Exception:
                        state['last_error'] = 'renewal_temporarily_failed'
        heartbeat = asyncio.create_task(keepalive())
        async def observe():
            while True:
                await asyncio.sleep(feed.poll_delay)
                if feed.enabled and account.available:
                    try:await work(feed.poll)
                    except ChatFailure as e:
                        state['last_error']=e.reason
                        if e.reason=='authentication_failed':
                            account.invalidate()
                            feed.store.notice(uuid.uuid4().hex,'【Muse 认证已失效，已停止本人实例的新调用。请在服务器重新认证；不会切换账号。】')
                    except Exception:state['last_error']='main_feed_read_failed'
        observer = asyncio.create_task(observe())
        yield
        journal.event('service.stopping')
        heartbeat.cancel()
        observer.cancel()
        restoration.cancel()
        try: await restoration
        except asyncio.CancelledError: pass
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        try:await observer
        except asyncio.CancelledError:pass
        if bridge and bridge.poll() is None:
            try:
                async with httpx.AsyncClient(trust_env=False, verify=False, timeout=4) as client:
                    await client.post(f"http://127.0.0.1:{settings['bridge_port']}/internal/stop",
                        headers={'Authorization': 'Bearer ' + key})
                await asyncio.to_thread(bridge.wait, 5)
            except Exception:
                bridge.terminate()
                if isinstance(bridge, RetainedBridge):
                    await asyncio.to_thread(bridge.wait, 5)
        await work(native.close)
        feed.close()
        await work(engine.stop)
        account.clear()
        os.environ.pop('MUSE2API_API_KEY', None)
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.account, app.state.engine, app.state.status = account, engine, state
    app.state.journal = journal
    app.state.native = native
    app.state.feed = feed
    secure_local(app, settings['muse_port'])

    def auth(request):
        if not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + key):
            raise HTTPException(401, 'API authentication required')

    @app.get('/')
    async def portal():
        from instance_pages import instance_page
        return HTMLResponse(instance_page(settings, 'portal.html'))

    # Every httpx request in this service targets a fixed plain-HTTP loopback
    # endpoint. Do not initialize a TLS certificate store for local polling.
    @app.get('/local/status')
    async def status():
        bridge_running = bool(bridge and bridge.poll() is None)
        wechat_connected, bound, delivered, multi_turn = False, False, False, False
        bridge_wait=None
        if bridge_running:
            try:
                async with httpx.AsyncClient(trust_env=False, verify=False, timeout=1) as client:
                    child = (await client.get(f"http://127.0.0.1:{settings['bridge_port']}/local/status")).json()
                if child.get('process_id') == bridge.pid and child.get('instance') == settings['instance']:
                    wechat_connected, bound = bool(child['logged_in']), bool(child['bound'])
                    delivered, multi_turn = bool(child['human_delivery_confirmed']), bool(child['multi_turn_human_confirmed'])
                    bridge_wait=child.get('queue_waiting_reason')
            except Exception:
                pass
        return {**state, 'authenticated': account.available,
            'routing': routing_mode, 'native_main': native.status(), 'main_feed':{**feed.status(),'wechat_waiting_reason':bridge_wait},
            'logging': journal.status(),
            'bridge_started': bridge_running,
            'stages': {'service_running': True, 'muse_authenticated': account.available and state['owner_confirmed'],
                'muse_real_validated': ((feed.observed>0 if feed.enabled else native.completed>0) if routing_mode=='native_main' else state['real_test']=='PASS'), 'wechat_connected': wechat_connected,
                'wechat_user_bound': bound, 'real_roundtrip_verified': account.available and ((feed.observed>0 if feed.enabled else native.completed>0) if routing_mode=='native_main' else state['real_test']=='PASS') and
                    wechat_connected and bound and delivered and multi_turn},
            'browser_verified': getattr(engine, 'browser_verified', False),
            'browser_recovery_pending': recovery['pending'],
            'retained_bridge': retained_record if bridge_running else None,
            'ports': {k: settings[k] for k in ('muse_port', 'bridge_port', 'cdp_port')}}

    @app.get('/local/logs')
    async def logs():
        return {'logging': journal.status(), 'entries': journal.tail()}

    @app.post('/local/resolve-outbox')
    async def resolve_outbox(request:Request):
        body=await request.json();head=feed.store.head()
        if (not head or head['state'] not in ('uncertain','waiting') or body.get('n')!=head['n']
                or body.get('confirm_checked_phone_and_muse') is not True
                or body.get('resolution') not in ('confirmed_received','cancelled')):
            raise HTTPException(409,'Check the exact queued item on phone and Muse before resolving it')
        feed.store.transition(head['n'],head['state'],body['resolution'],head['target'],'manual_review')
        journal.event('wechat.outbox_manually_resolved',message_id=head['message_id'],status=body['resolution'])
        return {'ok':True,'notice':'本条不会重新发送，队列继续按序处理。'}

    @app.post('/local/restore-feed')
    async def restore_feed(request:Request):
        body=await request.json()
        if not account.available or not state['owner_confirmed'] or body.get('confirm_main') is not True or settings.get('routing',{}).get('forward_all') is not True:
            raise HTTPException(409,'Confirm this owner and main chat first')
        try:return await work(feed.enable)
        except ChatFailure as e:return JSONResponse({'code':e.reason,'notice':e.notice},status_code=409)

    @app.post('/local/prepare-main')
    async def prepare_main():
        if not account.available or not state['owner_confirmed']:
            raise HTTPException(409,'Authenticate this instance and confirm its owner first')
        try:
            return await work(native.prepare)
        except ChatFailure as error:
            if error.reason=='authentication_failed':
                account.invalidate();state.update(authenticated=False,last_error=error.reason)
            return JSONResponse({'code':error.reason,'notice':error.notice},status_code=401 if error.reason=='authentication_failed' else 409)

    @app.post('/local/recover-browser')
    async def recover_browser(request: Request):
        body = await request.json()
        expected = feed.store.get('owner') or recovery['expected_owner']
        if body.get('confirm_same_owner') is not True or not expected:
            raise HTTPException(409, 'Confirm the previously bound owner before browser recovery')
        if recovery['pending']:
            raise HTTPException(409, 'Finish login in the already opened recovery window')
        if bridge and bridge.poll() is None:
            async with httpx.AsyncClient(trust_env=False, verify=False, timeout=3) as client:
                child = (await client.get(f"http://127.0.0.1:{settings['bridge_port']}/local/status")).json()
            if child.get('process_id') != bridge.pid or (child.get('incoming_queue') or {}).get('pending'):
                raise HTTPException(409, 'Review pending input before browser recovery')
        def begin():
            recovery_idle(native, feed)
            open_recovery(engine, native, feed)
            account.invalidate()
            recovery.update(pending=True, expected_owner=expected)
            state.update(authenticated=False, last_error='browser_reauthentication_required')
        try:
            await work(begin)
        except ChatFailure as error:
            return JSONResponse({'code':error.reason,'notice':error.notice},status_code=409)
        return {'ok':True, 'next':'在专用隐身窗口登录原 Muse 账号，再确认恢复主聊天；微信连接保留。'}

    @app.post('/local/recover-main')
    async def recover_main(request: Request):
        body = await request.json()
        if not recovery['pending'] or body.get('confirm_same_owner') is not True or body.get('confirm_main') is not True:
            raise HTTPException(409, 'Confirm same owner and main page in the recovery window')
        def finish():
            recovery_idle(native, feed)
            native.prepare()
            verify_owner(native, recovery['expected_owner'])
            values, expires = engine.capture()
            native.bind()
            if settings.get('routing', {}).get('forward_all') is True:
                feed.enable()
            account.inject(values, expires)
            recovery['pending'] = False
            state.update(authenticated=True, owner_confirmed=True, last_error=None)
        try:
            await work(finish)
        except ChatFailure as error:
            return JSONResponse({'code':error.reason,'notice':error.notice},status_code=409)
        return {'ok':True, 'same_owner_verified':True, 'credentials':'memory only', 'cursor':feed.store.get('cursor')}

    @app.post('/local/bind-main')
    async def bind_main(request: Request):
        nonlocal routing_mode
        body=await request.json()
        if not account.available or not state['owner_confirmed'] or body.get('confirm_main') is not True:
            raise HTTPException(409,'Confirm this authenticated owner and actual main chat first')
        if bridge and bridge.poll() is None:
            raise HTTPException(409,'Stop the old Bridge before switching routing; Muse login can remain active')
        try:
            await work(native.bind)
            if settings.get('routing',{}).get('forward_all') is True:
                await work(feed.enable)
            persist_routing(settings,'native_main')
            routing_mode='native_main'
            await work(save_login)
            journal.event('muse.main_bound')
            return native.status()
        except ChatFailure as error:
            if error.reason=='authentication_failed':
                account.invalidate();state.update(authenticated=False,last_error=error.reason)
            return JSONResponse({'code':error.reason,'notice':error.notice},status_code=401 if error.reason=='authentication_failed' else 409)

        except VaultError:
            account.invalidate()
            feed.enabled = False
            state.update(authenticated=False, owner_confirmed=False, credential_recovery='login_required',
                         last_error='credential_save_or_owner_conflict')
            raise HTTPException(409, '账号重复绑定或加密保存失败，请检查本组账号归属。') from None

    @app.post('/local/login')
    async def login():
        if state.get('credential_recovery') == 'restoring':
            raise HTTPException(409, '正在恢复原账号，请等待完成后再操作。')
        if state['owner_confirmed']:
            raise HTTPException(409, 'Stop and restart this instance before changing identity')
        if hasattr(engine, 'cfg') and not Path(engine.cfg.chromium).is_file():
            return JSONResponse({'code': 'browser_component_missing'}, status_code=409)
        await work(engine.login_window)
        return {'ok': True, 'next': 'Finish the account owner Muse login in the new incognito Chrome window'}

    @app.post('/local/capture')
    async def capture(request: Request):
        body = await request.json()
        if state.get('credential_recovery') == 'restoring':
            raise HTTPException(409, '正在恢复原账号，请等待完成后再操作。')
        if body.get('confirm_owner') is not True or state['owner_confirmed']:
            raise HTTPException(400, 'Confirm account owner identity once; restart before replacing it')
        def collect():
            if recovery['expected_owner']:
                native.prepare()
                verify_owner(native, recovery['expected_owner'])
            values, expires = engine.capture()
            account.inject(values, expires)
        try:
            await work(collect)
        except Exception:
            raise HTTPException(409, 'Login not ready: finish Muse authentication and retry') from None
        state.update(authenticated=True, owner_confirmed=True, real_test='not_run', last_error=None)
        journal.event('muse.authenticated', status='browser_capture')
        return {'ok': True, 'credentials': 'memory only; no values returned'}

    @app.post('/local/import')
    async def import_cookies(request: Request):
        if state['owner_confirmed']:
            raise HTTPException(409, 'Restart before replacing identity')
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, 'Credential input too large')
        try:
            data = json.loads(raw)
            if data.get('confirm_owner') is not True:
                raise ValueError()
            account.inject(json.loads(data['cookies_json']))
        except Exception:
            raise HTTPException(400, 'Invalid credential input; values not returned') from None
        state.update(authenticated=True, owner_confirmed=True, real_test='not_run')
        journal.event('muse.authenticated', status='hidden_input')
        return {'ok': True, 'credentials': 'process environment and memory only'}

    @app.post('/local/confirm-env')
    async def confirm_env(request: Request):
        body = await request.json()
        if not account.available or body.get('confirm_owner') is not True:
            raise HTTPException(400, 'Runtime credentials and identity confirmation required')
        state['owner_confirmed'] = True
        journal.event('muse.authenticated', status='runtime_environment')
        return {'ok': True}

    @app.post('/local/renew')
    async def renew():
        if not account.available or not state['owner_confirmed']:
            raise HTTPException(409, 'Authenticate and confirm this account first')
        before = dict(account.cookies)
        try:
            await work(engine.renew_for_account, account)
            await work(save_login)
        except MuseAuthError as error:
            journal.event('muse.renewal_failed', reason=authentication_failure_code(error))
            account.invalidate()
            state.update(authenticated=False, last_error=authentication_failure_code(error))
            raise HTTPException(401, 'This account needs reauthentication') from None
        except Exception:
            journal.event('muse.renewal_failed', reason='temporary_failure')
            state['last_error'] = 'renewal_temporarily_failed'
            raise HTTPException(502, 'Renewal temporarily failed; credentials not declared invalid') from None
        result = {'result': 'PASS', 'cookie_values_changed': before != account.cookies,
            'known_expiry_count': len(account.expires), 'timestamp': int(time.time()),
            'restart_requires_reauthentication': not bool(vault)}
        (settings['runtime'] / 'renewal-verification.json').write_text(json.dumps(result), encoding='utf-8')
        journal.event('muse.renewed')
        return result

    class Message(BaseModel):
        role: str
        content: str = Field(max_length=16000)

    class Chat(BaseModel):
        model_config = ConfigDict(extra='ignore')
        model: str = 'muse-spark'
        messages: list[Message] = Field(min_length=1, max_length=24)
        stream: bool = False
        muse_route: Literal['native_main','isolated_test'] | None = None
        muse_attachments: list[dict] = Field(default_factory=list,max_length=10)

    @app.get('/v1/muse/feed')
    async def feed_status(request:Request):
        auth(request)
        # Read/sending of already verified ordinary outbox text is independent
        # from Muse authentication, so expiry notices can still reach WeChat.
        return {**feed.status(),'muse_authenticated':account.available}

    @app.get('/v1/muse/ready')
    async def feed_readiness(request:Request):
        auth(request)
        if not account.available or not state['owner_confirmed']:raise HTTPException(401,'Instance authentication required')
        try:return await work(feed.readiness)
        except ChatFailure as e:
            if e.reason=='authentication_failed':
                account.invalidate();raise HTTPException(401,'Instance authentication expired')
            return {'ready':False,'reason':e.reason}

    @app.post('/v1/muse/submit')
    async def feed_submit(request:Request,body:Chat):
        auth(request)
        if not account.available or not state['owner_confirmed']:raise HTTPException(401,'Instance authentication required')
        if not feed.enabled or body.model!='muse-spark' or body.stream or len(body.messages)!=1 or body.messages[0].role!='user':
            raise HTTPException(409,'All-assistant feed accepts exactly the current raw user input')
        source=request.headers.get('x-source-id','')
        rid=request.headers.get('x-request-id','')
        if not re.fullmatch('[a-f0-9]{64}',source) or not re.fullmatch('[a-f0-9]{32}',rid):
            raise HTTPException(422,'Stable input identity required')
        try:
            files=decode_uploads(body.muse_attachments)
            journal.api_request(rid,[m.model_dump() for m in body.messages])
            journal.media('api.attachments',[f.metadata() for f in files],request_id=rid)
            result=await work(feed.submit,rid,body.messages[0].content,files,source)
            # 202 is a submission ACK, never a completed/model-success response.
            return JSONResponse(result,status_code=202)
        except ChatFailure as e:
            journal.event('api.failed',request_id=rid,reason=e.reason)
            if e.reason=='authentication_failed':account.invalidate()
            return JSONResponse({'code':e.reason,'notice':e.notice},status_code=401 if e.reason=='authentication_failed' else 409)

    @app.post('/v1/muse/outbox')
    async def feed_action(request:Request):
        auth(request)
        if not feed.enabled:raise HTTPException(409,'Verified main feed required')
        body=await request.json();action=body.get('action');n=body.get('n');target=body.get('target')
        if type(n) is not int or not isinstance(target,str) or not target or len(target)>200:
            raise HTTPException(422,'Action identity required')
        transitions={'claim':('pending','sending'),'sent':('sending','sent'),
            'rejected':('sending','waiting'),'uncertain':('sending','uncertain'),
            'resume':('waiting','pending'),'media_failed':('sending','waiting')}
        if action not in transitions:raise HTTPException(422,'Unsupported transition')
        reasons={'rejected':'wechat_requires_user_interaction','uncertain':'send_result_unknown',
            'media_failed':'attachment_unavailable'}
        try:feed.store.transition(n,*transitions[action],target,reasons.get(action))
        except ValueError:raise HTTPException(409,'Queue order, state or target mismatch') from None
        head=feed.store.head() if action=='claim' else None
        if head and head['kind']=='file':
            try:
                if not account.available:raise ValueError('No authenticated media read')
                attachment=await work(feed.attachment,head['body'])
                head['attachment']=attachment.wire()
            except Exception:
                feed.store.media_notice(n,target)
                raise HTTPException(409,'Attachment unavailable; item retained for manual recovery') from None
        return {'ok':True,'head':head}

    @app.post('/v1/muse/notice')
    async def feed_notice(request:Request):
        auth(request)
        body=await request.json();text=body.get('text');mid=body.get('message_id')
        if not isinstance(text,str) or len(text)>2000 or not isinstance(mid,str) or not re.fullmatch('[a-f0-9]{64}',mid):
            raise HTTPException(422,'Invalid notice')
        feed.store.notice(mid,feed.scrub(text))
        return {'ok':True}

    @app.post('/v1/chat/completions')
    async def chat(request: Request, body: Chat):
        auth(request)
        if body.model != 'muse-spark' or any(m.role not in ('system', 'user', 'assistant') for m in body.messages):
            raise HTTPException(400, 'Only muse-spark text chat is enabled')
        if not account.available or not state['owner_confirmed']:
            raise HTTPException(401, 'Account owner authentication required')
        if sum(len(m.content) for m in body.messages) > 24000:
            raise HTTPException(413, 'Conversation too long; shorten input')
        request_id = request.headers.get('x-request-id', '')
        if len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id):
            request_id = uuid.uuid4().hex
        native_requested=body.muse_route=='native_main' or (body.muse_route is None and routing_mode=='native_main')
        if body.muse_route=='isolated_test' and native.bound:
            return JSONResponse({'code':'isolated_test_disabled_while_main_bound','notice':'主聊天已绑定，请用正常微信输入验收；隔离测试入口保留给绑定前的旁聊。'},status_code=409)
        if native_requested:
            if feed.enabled:
                return JSONResponse({'code':'all_assistant_feed_enabled','notice':'本实例已启用主聊天全部助手消息队列，请通过专用提交入口发送；提交受理与真实回复分别记录。'},status_code=409)
            if routing_mode!='native_main' or not native.bound:
                return JSONResponse({'code':'main_not_bound','notice':'请确认本实例原生主聊天；不会回退旁聊。'},status_code=409)
            if body.stream or len(body.messages)!=1 or body.messages[0].role!='user':
                raise HTTPException(422,'Native main chat accepts only this current user message; no history or system prompt')
            try:
                files=decode_uploads(body.muse_attachments)
            except ChatFailure:
                raise HTTPException(422,'Attachment validation failed; not submitted') from None
            journal.api_request(request_id,[m.model_dump() for m in body.messages])
            journal.media('api.attachments',[f.metadata() for f in files],request_id=request_id)
            try:
                result=await work(native.reply,request_id,body.messages[0].content,files)
            except ChatFailure as error:
                journal.event('api.failed',request_id=request_id,reason=error.reason)
                state['last_error']=error.reason
                if error.reason=='authentication_failed':
                    account.invalidate();state['authenticated']=False
                return JSONResponse({'code':error.reason,'notice':error.notice},status_code=401 if error.reason=='authentication_failed' else 409)
            state['api_requests_completed']+=1
            journal.chat('api.reply',text=result['text'],request_id=request_id)
            bubbles=result.get('messages') or [{'message_id':result['message_id'],'text':result['text'],
                'attachment_indexes':list(range(len(result['attachments']))),'media_errors':result['media_errors']}]
            for bubble in bubbles:
                journal.chat('api.reply_bubble',text=bubble['text'],message_id=bubble['message_id'],request_id=request_id)
            return {'id':'chatcmpl-'+uuid.uuid4().hex,'object':'chat.completion','created':int(time.time()),
                'model':'muse-spark','choices':[{'index':0,'message':{'role':'assistant','content':result['text']},'finish_reason':'stop'}],
                'muse':{'routing':'native_main','history_replay':False,'message_id':result['message_id'],
                    'message_ids':[b['message_id'] for b in bubbles],'reply_association':'guarded_batch_anchor'},
                'muse_messages':bubbles,'muse_attachments':result['attachments'],'media_errors':result['media_errors']}
        if body.muse_attachments:
            raise HTTPException(422,'Attachments require native_main; no text placeholder fallback')
        if any(not m.content.strip() for m in body.messages):
            raise HTTPException(422,'Empty isolated text message')
        journal.api_request(request_id, [m.model_dump() for m in body.messages])
        try:
            reply = await work(engine.reply, account, build_prompt([m.model_dump() for m in body.messages]))
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError('Empty upstream reply')
        except MuseAuthError as error:
            journal.event('api.failed', request_id=request_id, reason=authentication_failure_code(error))
            account.invalidate()
            state.update(authenticated=False, last_error=authentication_failure_code(error))
            raise HTTPException(401, 'This account needs reauthentication; no failover permitted') from None
        except Exception:
            journal.event('api.failed', request_id=request_id, reason='upstream_reply_failed')
            state['last_error'] = 'upstream_reply_failed'
            raise HTTPException(502, 'Real Muse reply failed; no failover permitted') from None
        state['api_requests_completed'] += 1
        journal.chat('api.reply', text=reply, request_id=request_id)
        cid, created = 'chatcmpl-' + uuid.uuid4().hex, int(time.time())
        if body.stream:
            async def chunks():
                for i in range(0, len(reply), 80):
                    data = {'id': cid, 'object': 'chat.completion.chunk', 'created': created,
                        'model': 'muse-spark', 'choices': [{'index': 0, 'delta': {'content': reply[i:i+80]}, 'finish_reason': None}]}
                    yield 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
                yield 'data: ' + json.dumps({'id': cid, 'model': 'muse-spark', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}) + '\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(chunks(), media_type='text/event-stream')
        return {'id': cid, 'object': 'chat.completion', 'created': created, 'model': 'muse-spark',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': reply}, 'finish_reason': 'stop'}]}

    @app.get('/v1/models')
    async def models(request: Request):
        auth(request)
        return {'object': 'list', 'data': [{'id': 'muse-spark', 'object': 'model', 'owned_by': settings['instance']}]}

    @app.post('/local/test')
    async def test():
        if native.bound:
            raise HTTPException(409,'Main chat is bound; use normal WeChat acceptance, not the isolated replay test')
        if not account.available or not state['owner_confirmed']:
            raise HTTPException(409, 'Finish Muse login and confirm owner first')
        marker = '秋月' + str(secrets.randbelow(900000) + 100000)
        async with httpx.AsyncClient(trust_env=False, verify=False, timeout=330) as client:
            async def query(messages):
                result = await client.post(f"http://127.0.0.1:{settings['muse_port']}/v1/chat/completions",
                    headers={'Authorization': 'Bearer ' + key},
                    json={'model': 'muse-spark', 'messages': messages, 'stream': False,'muse_route':'isolated_test'})
                result.raise_for_status()
                return result.json()['choices'][0]['message']['content']
            try:
                first = await query([{'role': 'user', 'content': '请只回复：中文连接成功'}])
                if '中文连接成功' not in first:
                    raise ValueError('Chinese acceptance failed')
                one = [{'role': 'user', 'content': f'请记住本轮测试暗号 {marker}，只回答已记住。'}]
                second = await query(one)
                third = await query(one + [{'role': 'assistant', 'content': second},
                    {'role': 'user', 'content': '刚才我让你记住的暗号是什么？请仅回答暗号。'}])
                passed = '中文连接成功' in first and marker in third
                result = {'instance': settings['instance'], 'backend': state['backend'],
                    'chinese': 'PASS' if '中文连接成功' in first else 'FAIL',
                    'explicit_history_replay': 'PASS' if marker in third else 'FAIL',
                    'context_mode': 'messages serialized into a fresh Muse thread per API request',
                    'native_thread_memory_tested': False,
                    'request_count': 3, 'reply_lengths': [len(first), len(second), len(third)],
                    'result': 'PASS' if passed else 'FAIL', 'timestamp': int(time.time())}
            except Exception:
                result = {'instance': settings['instance'], 'result': 'FAIL', 'reason': 'real_api_test_failed', 'timestamp': int(time.time())}
        state['real_test'] = result['result']
        # Only synthetic test outcomes, never prompts, responses, account identifiers or credentials.
        (settings['runtime'] / 'muse-verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result

    @app.post('/local/start-bridge')
    async def start_bridge():
        nonlocal bridge, retained_record
        if not account.available or not state['owner_confirmed'] or (routing_mode=='native_main' and not native.bound) or (routing_mode!='native_main' and state['real_test']!='PASS'):
            raise HTTPException(409, 'Confirm native main chat (native mode), or pass isolated real tests (branch mode), before starting Bridge')
        if settings.get('routing',{}).get('forward_all') and not feed.enabled:
            raise HTTPException(409,'Verify and enable the owner-specific main feed before WeChat authorization')
        if not bridge or bridge.poll() is not None:
            with socket.socket() as check:
                try:
                    check.bind(('127.0.0.1', settings['bridge_port']))
                except OSError:
                    raise HTTPException(409, 'Bridge port occupied; stop the conflicting process first') from None
            env = dict(os.environ)
            for name in list(env):
                if 'COOKIE' in name.upper() or (name.startswith('MUSE_') and name.endswith('_API_KEY')):
                    env.pop(name, None)
            bridge = subprocess.Popen([sys.executable, '-I', str(CODE_ROOT / 'real' / 'bridge_service.py'), settings['instance']],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            retained_record = None
            ready = False
            async with httpx.AsyncClient(trust_env=False, verify=False, timeout=1) as client:
                for _ in range(40):
                    if bridge.poll() is not None:
                        break
                    try:
                        response = await client.get(f"http://127.0.0.1:{settings['bridge_port']}/local/status")
                        if response.json().get('process_id') == bridge.pid:
                            ready = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(.1)
            if not ready:
                if bridge.poll() is None:
                    bridge.terminate()
                raise HTTPException(502, 'Bridge startup failed; no QR authorization requested')
        state['bridge_started'] = True
        return {'ok': True, 'url': f"http://127.0.0.1:{settings['bridge_port']}/"}

    @app.post('/local/stop')
    async def stop():
        app.state.server.should_exit = True
        return {'ok': True}

    return app


if __name__ == '__main__':
    quiet()
    try:
        settings = load_instance(sys.argv[1])
        app = create_service(settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=settings['muse_port'],
            access_log=False, log_level='critical'))
        app.state.server = server
        server.run()
    except Exception:
        print('Muse instance stopped: startup or runtime failed; no credential diagnostics emitted.')
        sys.exit(1)
