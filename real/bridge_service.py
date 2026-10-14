"""WeChat-Bridge iLink + its OpenAI-compatible AIChatManager, with memory-only state."""
import asyncio
import io
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from types import SimpleNamespace

import qrcode
import psutil
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, CODE_ROOT, load_instance, quiet, secure_local
from journal import InstanceJournal
from credential_vault import Vault, VaultError

sys.path.insert(0, str(CODE_ROOT / 'vendor' / 'WeChat-Bridge' / 'app'))
from ilink import ILinkClient
from message_policy import SafeChatManager, ChatFailure, MessagePolicy, fit_reply
from media_memory import MemoryMedia
from native_policy import NativeChatManager
from feed_bridge import FeedBridge
from incoming_queue import IncomingQueue


class MemoryILink(ILinkClient):
    def __init__(self, vault=None, instance=None):
        self.vault, self.scope = vault, (instance + '.wechat') if instance else None
        self.saved_binding = None
        self.checkpoint = ''
        super().__init__(token_file=None, load_token=False, save_on_login=False)
        self._session.trust_env = False
        if vault:
            saved = vault.read(self.scope)
            if saved:
                parsed = urlsplit(saved['base_url'])
                if (parsed.scheme != 'https' or parsed.hostname != 'ilinkai.weixin.qq.com'
                        or parsed.port not in (None, 443) or parsed.username or parsed.query or parsed.fragment
                        or parsed.path not in ('', '/')):
                    raise ValueError('Unrecognized iLink host; rescan required')
                if not saved.get('bot_token') or not saved.get('user_id'):
                    raise ValueError('Incomplete saved WeChat identity')
                for field in ('bot_token', 'base_url', 'bot_id', 'user_id', 'get_updates_buf'):
                    setattr(self, field, saved.get(field))
                self.checkpoint = self.get_updates_buf or ''
                self.saved_binding = saved.get('binding')

    def _save_token(self):
        if self.vault and self.bot_token:
            self.vault.write(self.scope, {field: getattr(self, field) for field in
                ('bot_token', 'base_url', 'bot_id', 'user_id')} |
                {'get_updates_buf': self.checkpoint, 'binding': self.saved_binding})

    def commit_batch(self):
        self.checkpoint = self.get_updates_buf
        self._save_token()

    def _load_token(self):
        pass


def custom_ai_config(settings, key):
    return {'enabled': True, 'provider': 'openai', 'model': 'muse-spark',
        'base_url': f"http://127.0.0.1:{settings['muse_port']}/v1", 'api_key': key,
        'system_prompt': '请用中文简洁回复。', 'max_history': 10,
        'max_tokens_per_day': 100000, 'usage': {}, 'telemetry_enabled': False}


def bridge_process_identity(settings):
    """Windows venv redirector is the supervised Popen process, not the server.

    Accept that extra process only after verifying executable, exact entrypoint,
    instance, direct ancestry and the Muse parent's owned loopback listener.
    """
    serving = psutil.Process(os.getpid())
    parent = serving.parent()
    bridge_script = str(CODE_ROOT / 'real' / 'bridge_service.py')
    muse_script = str(CODE_ROOT / 'real' / 'muse_service.py')
    supervised = serving.pid
    mode = 'direct'
    if parent and bridge_script in parent.cmdline():
        args = parent.cmdline()
        launcher = ROOT / '.venv' / 'Scripts' / 'python.exe'
        owner = parent.parent()
        if (not launcher.is_file() or not Path(parent.exe()).samefile(launcher)
                or args[-1] != settings['instance'] or '-I' not in args
                or owner is None or muse_script not in owner.cmdline()
                or owner.cmdline()[-1] != settings['instance']):
            raise RuntimeError('Bridge launcher ownership failed')
        listeners = [c for c in psutil.net_connections(kind='tcp')
            if c.status == psutil.CONN_LISTEN and c.laddr.port == settings['muse_port']]
        if not listeners or any(c.laddr.ip not in ('127.0.0.1', '::1') or c.pid != owner.pid for c in listeners):
            raise RuntimeError('Muse parent listener ownership failed')
        supervised, mode = parent.pid, 'verified_windows_venv_redirector'
    return {'process_id': supervised, 'serving_process_id': serving.pid, 'supervision_mode': mode}


def create_bridge(settings, client=None, policy=None):
    key = os.environ['MUSE2API_API_KEY']
    storage = Vault(ROOT) if settings.get('credential_storage') == 'windows_dpapi' else None
    storage_error = False
    if client is None:
        try: client = MemoryILink(storage, settings['instance'])
        except Exception:
            client, storage_error = MemoryILink(), True
            # Damaged/expired credentials must still permit fresh QR authorization.
            client.vault, client.scope = storage, settings['instance'] + '.wechat'
    config = custom_ai_config(settings, key)
    qr = {}
    current_context = {}
    journal = InstanceJournal(settings, 'bridge', lambda: (key, getattr(client, 'bot_token', None),
        qr.get('code'), qr.get('link'),current_context.get('token')))
    local_requests = requests.Session()
    local_requests.trust_env = False
    native_mode=settings.get('routing',{}).get('mode','branch_replay')=='native_main'
    all_feed=native_mode and settings.get('routing',{}).get('forward_all') is True
    manager = NativeChatManager(settings,key,local_requests,journal) if native_mode else SafeChatManager(config, local_requests, journal)
    media_session=requests.Session()
    media_session.trust_env=False
    media=MemoryMedia(media_session)
    policy = policy or MessagePolicy()
    state = {'instance': settings['instance'], **bridge_process_identity(settings), 'logged_in': False, 'bound': False,
        'muse_model': 'muse-spark', 'muse_base': config['base_url'],
        'received': 0, 'accepted_sends': 0, 'accepted_notices': 0, 'accepted_media_sends':0, 'failed': 0,
        'routing':'native_main' if native_mode else 'branch_replay', 'history_replay':not native_mode,
        'truncated_replies': 0, 'human_delivery_confirmed': False,
        'multi_turn_human_confirmed': False, 'binding_role': None,
        'temporary_delivery_confirmed': False, 'temporary_multi_turn_confirmed': False,
        'accepted_text_bubbles': 0, 'last_reply_batch_count': 0,
        'last_batch_messages_accepted': 0, 'delivery_halted': False, 'last_send_failure': None,
        'last_error': None}
    if storage_error: state['last_error'] = 'saved_wechat_needs_scan'
    pending_users = {}
    allowed_user = None
    saved_binding = getattr(client, 'saved_binding', None)
    if client.logged_in and isinstance(saved_binding, dict) and saved_binding.get('user_id') == client.user_id:
        allowed_user = client.user_id
        state.update(bound=True, binding_role='temporary_tester' if saved_binding.get('role') == 'temporary_tester' else 'owner')
    state['logged_in'] = client.logged_in
    stop = threading.Event()
    poll_started = False
    binding_lock = threading.Lock()
    worker = None
    def feed_binding():
        with binding_lock:return allowed_user,current_context.get('token'),current_context.get('version')
    if all_feed:
        manager=FeedBridge(settings,key,client,journal,stop,feed_binding,state)
        manager.incoming=IncomingQueue(settings,media.incoming,manager.readiness,
            lambda uid,text,**kw:manager.chat(settings['instance']+':'+uid,text,**kw),manager.notice,journal,stop)
        state.update(forward_all=True,queue_waiting_reason=None,main_feed=None)
    journal.event('service.started')

    def loop():
        while not stop.is_set() and client.logged_in:
            try:
                messages = client.get_updates()
                for msg in messages:
                    if stop.is_set() or msg.get('message_type') != 1:
                        continue
                    uid = msg.get('from_user_id')
                    if not uid:
                        continue
                    with binding_lock:
                        if uid not in pending_users and len(pending_users) < 10:
                            pending_users[uid] = {'token':msg.get('context_token'),'version':str(msg.get('message_id') or msg.get('msg_id'))}
                        target = allowed_user
                    if uid != target:
                        continue
                    mid = msg.get('message_id') or msg.get('msg_id')
                    decision = policy.accept(uid, mid)
                    if decision == 'duplicate':
                        journal.event('wechat.duplicate', user_id=uid, message_id=str(mid))
                        continue
                    if decision != 'accepted':
                        journal.event('wechat.rejected', user_id=uid, reason=decision)
                        state.update(last_error=decision, failed=state['failed'] + 1)
                        # A missing ID cannot be safely deduplicated. Full capacity stops
                        # polling instead of evicting IDs and risking repeated generations.
                        if decision == 'dedupe_capacity_reached':
                            stop.set()
                        continue
                    items = msg.get('item_list') or []
                    unsupported = any(item.get('type') != 1 for item in items)
                    text = ('' if native_mode else '\n').join(item.get('text_item', {}).get('text', '')
                        for item in items if item.get('type') == 1)
                    context = msg.get('context_token')
                    with binding_lock:current_context.update(token=context,version=str(mid))
                    journal.chat('wechat.incoming', text=text, user_id=uid, message_id=str(mid),
                        item_types=[item.get('type') for item in items], exclude=(context,))
                    state['received'] += 1
                    if not context:
                        journal.event('wechat.failed', user_id=uid, message_id=str(mid), reason='missing_reply_context')
                        state.update(last_error='missing_reply_context', failed=state['failed'] + 1)
                        continue
                    if all_feed:
                        try:
                            queued=manager.incoming.enqueue(uid,str(mid),items)
                            if queued=='queue_capacity':
                                manager.notice(uid,str(mid),'入站队列已满，本条尚未提交 Muse；请稍后手动重发。')
                        except Exception:
                            journal.event('wechat.inbound_enqueue_failed',message_id=str(mid),reason='not_submitted')
                            state.update(last_error='input_queue_failed',failed=state['failed']+1)
                            try:manager.notice(uid,str(mid),'入站排队失败，本条未调用 Muse；请检查服务器后手动重发。')
                            except Exception:journal.event('wechat.inbound_notice_unconfirmed',message_id=str(mid),reason='no_retry')
                        continue
                    notice = False
                    reply_messages=[]
                    batch_request_id=None
                    try:
                        if state['delivery_halted']:
                            raise ChatFailure('reply_delivery_unconfirmed_no_retry',
                                '上一批微信发送结果未确认，已停止新 Muse 调用；请到主要聊天核对。没有自动补发。')
                        attachments_in=[]
                        if native_mode:
                            text,attachments_in=media.incoming(items)
                            journal.media('wechat.attachments',[f.metadata() for f in attachments_in],message_id=str(mid))
                        elif unsupported:
                            # Reject a mixed caption + image too: never imply that
                            # Muse saw an attachment when only text was forwarded.
                            raise ChatFailure('unsupported_attachment',
                                '当前微信接入仅支持文字，图片、语音、视频和文件尚未接入 Muse。本条未调用 Muse，请先用文字描述。')
                        if not text.strip() and not attachments_in:
                            raise ChatFailure('empty_input', '没有读到可处理的文字，请发送文字消息。')
                        if not native_mode and text.strip() == '/清空':
                            manager.clear_all_histories()
                            reply, notice = '本实例的短期聊天上下文已清空。', True
                        else:
                            if not policy.can_generate():
                                raise ChatFailure('rate_limited', '消息发送较快，请间隔至少 2 秒再试。本次没有调用 Muse。')
                            if native_mode:
                                native_reply=manager.chat(settings['instance']+':'+uid,text,message_id=str(mid),attachments=attachments_in)
                                if all_feed:
                                    continue # Submission ACK only; the outbox sends actual assistant messages.
                                reply_messages=native_reply['messages']
                                batch_request_id=native_reply['request_id']
                            else:
                                reply = manager.chat(settings['instance'] + ':' + uid, text, message_id=str(mid))
                        if not reply_messages:
                            reply_messages=[{'text':reply,'attachments':[],'media_errors':False,'message_id':None}]
                        for bubble in reply_messages:
                            if bubble['media_errors']:
                                bubble['text']+='\n【部分 Muse 附件未能获取，请到主要聊天查看；本次未重新生成。】'
                            bubble['text'],truncated=fit_reply(bubble['text'])
                            state['truncated_replies']+=int(truncated)
                            if truncated:
                                journal.event('wechat.reply_truncated',user_id=uid,message_id=str(mid),truncated=True)
                        actions=sum(int(bool(b['text']))+len(b['attachments']) for b in reply_messages)
                        if len(reply_messages)>8 or actions>8:
                            raise ChatFailure('reply_batch_too_large',
                                'Muse 本次回复超过安全发送上限（8 个气泡或发送动作），已保留在主要聊天；微信未转发本批，也未重新生成。')
                    except ChatFailure as exc:
                        journal.event('wechat.failed', user_id=uid, message_id=str(mid), reason=exc.reason)
                        reply_messages=[{'text':exc.notice,'attachments':[],'media_errors':False,'message_id':None}]
                        notice=True
                        state.update(last_error=exc.reason, failed=state['failed'] + 1)
                        if all_feed:
                            manager.notice(uid,str(mid),exc.notice)
                            continue
                    if stop.is_set():
                        break
                    state.update(last_reply_batch_count=len(reply_messages),last_batch_messages_accepted=0)
                    try:
                        # Exact whitelisted sender and context from THIS message only.
                        # Native boundaries only: never split a list/newline or
                        # regenerate to obtain more bubbles. Fail mid-batch closed.
                        actions_sent=0
                        for index,bubble in enumerate(reply_messages):
                            if bubble['text']:
                                if actions_sent and stop.wait(.4):raise RuntimeError('stopping')
                                journal.chat('wechat.outgoing',text=bubble['text'],user_id=uid,message_id=str(mid),exclude=(context,))
                                client.send_text(uid,bubble['text'],context)
                                actions_sent+=1
                                if not notice:state['accepted_text_bubbles']+=1
                            for attachment in bubble['attachments']:
                                if actions_sent and stop.wait(.4):raise RuntimeError('stopping')
                                journal.media('wechat.outgoing_attachment',[attachment.metadata()],message_id=str(mid))
                                if attachment.mime.startswith('image/'):
                                    client.send_image(uid,attachment.data,context)
                                elif attachment.mime in ('video/mp4','video/quicktime'):
                                    client.send_video(uid,attachment.data,context)
                                else:
                                    client.send_file(uid,attachment.data,context,file_name=attachment.filename)
                                actions_sent+=1
                                state['accepted_media_sends']+=1
                            state['last_batch_messages_accepted']+=1
                            if not notice:
                                journal.event('wechat.bubble_send_accepted',user_id=uid,message_id=bubble['message_id'],
                                    request_id=batch_request_id,status=str(index+1)+'_of_'+str(len(reply_messages)))
                        counter = 'accepted_notices' if notice else 'accepted_sends'
                        state[counter] += 1
                        journal.event('wechat.send_accepted', user_id=uid, message_id=str(mid),
                            status='notice' if notice else 'reply')
                    except Exception:
                        journal.event('wechat.send_unconfirmed', user_id=uid, message_id=str(mid), reason='no_retry')
                        state.update(last_error='reply_delivery_unconfirmed_no_retry', failed=state['failed'] + 1,delivery_halted=True)
                        if state['last_send_failure'] is None:
                            state['last_send_failure']={'request_id':batch_request_id,'incoming_message_id':str(mid),
                                'native_message_ids':[b['message_id'] for b in reply_messages],
                                'complete_bubbles_accepted':state['last_batch_messages_accepted'],
                                'uncertain_bubble_number':index+1,'total_bubbles':len(reply_messages),
                                'remaining_original_bubbles_not_attempted':len(reply_messages)-index-1}
                        if not notice and not stop.is_set():
                            # A distinct failure notice, never retry the uncertain
                            # bubble/media or continue later original messages.
                            failure_notice=('Muse 回复发送结果未确认：已受理 '+str(state['last_batch_messages_accepted'])+'/'+
                                str(len(reply_messages))+' 个原生气泡，其余未继续发送。请到主要聊天查看；本次未自动重发，已停止新调用。')
                            try:
                                journal.chat('wechat.outgoing',text=failure_notice,user_id=uid,message_id=str(mid),exclude=(context,))
                                client.send_text(uid,failure_notice,context)
                                state['accepted_notices']+=1
                            except Exception:
                                pass
                if hasattr(client, 'commit_batch'): client.commit_batch()
            except Exception as exc:
                journal.event('wechat.operation_failed', exception_type=type(exc).__name__)
                state.update(last_error='ilink_or_muse_operation_failed', failed=state['failed'] + 1)
                stop.wait(2)
        state['logged_in'] = False
        if storage and not stop.is_set():
            storage.delete(settings['instance'] + '.wechat')
            state['last_error'] = 'wechat_authorization_expired'
        journal.event('wechat.worker_stopped')

    @asynccontextmanager
    async def lifespan(app):
        nonlocal worker, poll_started
        if client.logged_in:
            poll_started = True
            worker = threading.Thread(target=loop, daemon=True)
            worker.start()
            if all_feed: manager.start()
        yield
        stop.set()
        journal.event('service.stopping')
        if worker:
            await asyncio.to_thread(worker.join, 46)
        manager.clear_all_histories()
        if all_feed:await asyncio.to_thread(manager.close)
        client.clear_token()
        config['api_key'] = ''
        local_requests.close()
        media_session.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.client, app.state.config, app.state.status = client, config, state
    app.state.manager = manager
    app.state.journal = journal
    secure_local(app, settings['bridge_port'])

    @app.get('/')
    async def portal():
        from instance_pages import instance_page
        return HTMLResponse(instance_page(settings, 'wechat.html'))

    @app.get('/local/status')
    async def status():
        with binding_lock:
            users = list(pending_users)
        return {**state, 'incoming_queue':manager.incoming.status() if all_feed else None,
            'logging': journal.status(), 'pending_senders': users, 'bound_user': allowed_user,
            'login_user': client.user_id, 'bot_id': client.bot_id}

    @app.get('/local/logs')
    async def logs():
        return {'logging': journal.status(), 'entries': journal.tail()}

    @app.post('/local/resolve-incoming')
    async def resolve_incoming(request:Request):
        body=await request.json()
        if not all_feed or body.get('confirm_checked_main_chat') is not True:
            raise HTTPException(400,'Manually inspect the original main-chat input first')
        try:manager.incoming.resolve(body.get('message_id'),body.get('resolution'))
        except ValueError:raise HTTPException(409,'Uncertain input or resolution changed')
        return manager.incoming.status()

    @app.post('/local/qr')
    async def get_qr(request: Request):
        body = await request.json()
        if body.get('reviewed_ilink_flow') is not True:
            raise HTTPException(400, 'Read iLink authorization instructions before generating QR')
        if client.logged_in:
            raise HTTPException(409, 'Already logged in; restart before changing account')
        if poll_started:
            # A previous worker may still be leaving its long poll. Require the
            # owned instance restart instead of racing it or reusing stale binding.
            return JSONResponse({'code': 'wechat_reconnect_required'}, status_code=409)
        result = await asyncio.to_thread(client.get_qrcode)
        # qrcode_img_content is the authorization link despite its upstream field name.
        link = result.get('qrcode_img_content') or result.get('url')
        if not link or not result.get('qrcode'):
            raise HTTPException(502, 'iLink did not return a QR authorization link')
        qr.clear()
        qr.update(code=result['qrcode'], link=link)
        journal.event('wechat.qr_requested')
        return {'ok': True}

    @app.get('/local/qr.png')
    async def qr_image(request: Request):
        if not qr:
            raise HTTPException(404, 'Generate QR first')
        output = io.BytesIO()
        qrcode.make(qr['link']).save(output, format='PNG')
        return Response(output.getvalue(), media_type='image/png')

    @app.post('/local/poll')
    async def poll(request: Request):
        nonlocal poll_started, worker
        if client.logged_in:
            return {'status': 'confirmed'}
        body = await request.json()
        if not qr or body.get('reviewed_phone_authorization') is not True:
            raise HTTPException(400, 'Review the actual phone authorization screen and confirm it first')
        result = await asyncio.to_thread(client.poll_qrcode_status, qr['code'])
        status = result.get('status')
        journal.event('wechat.login_result', status=status if status in
            ('wait', 'scaned', 'confirmed', 'expired', 'scaned_but_redirect') else 'unknown')
        if status == 'confirmed':
            state['logged_in'] = True
            if not poll_started:
                poll_started = True
                worker = threading.Thread(target=loop, daemon=True)
                worker.start()
                if all_feed:manager.start()
            qr.clear()
        return {'status': status, 'next': 'The scanning person sends one message, then confirm the binding purpose and sender'}

    @app.post('/local/bind')
    async def bind(request: Request):
        nonlocal allowed_user
        body = await request.json()
        with binding_lock:
            uid = body.get('user_id')
            owner = body.get('confirm_owner') is True
            temporary = body.get('confirm_temporary_tester') is True
            if (allowed_user or not client.logged_in or owner == temporary
                    or not isinstance(uid, str) or uid not in pending_users
                    or not client.user_id or uid != client.user_id):
                raise HTTPException(400, 'Confirm exactly one purpose and the observed scanning sender; restart before rebinding')
            if hasattr(client, 'saved_binding'):
                client.saved_binding = {'user_id': uid, 'role': 'temporary_tester' if temporary else 'owner'}
                try: client.commit_batch()
                except VaultError:
                    client.saved_binding = None
                    raise HTTPException(409, '该微信已属于其他实例或加密保存失败，请先核对绑定。') from None
            allowed_user = uid
            if all_feed:
                current_context.update(pending_users[uid])
            state.update(bound=True, binding_role='temporary_tester' if temporary else 'owner')
        journal.event('wechat.user_bound', user_id=uid, status=state['binding_role'])
        return {'ok': True, 'binding_role': state['binding_role'],
            'next': 'Only this sender can use the bound Muse account; the initial binding message was not forwarded'}

    @app.post('/local/confirm-results')
    async def confirm(request: Request):
        body = await request.json()
        if state['accepted_sends'] < 2 or not state['bound']:
            raise HTTPException(409, 'At least two accepted replies required before human confirmation')
        temporary = state['binding_role'] == 'temporary_tester'
        # Existing Muse status reads the formal flags. A temporary tester must
        # never mark the account owner's final acceptance as complete.
        state['human_delivery_confirmed'] = not temporary and body.get('received_on_phone') is True
        state['multi_turn_human_confirmed'] = not temporary and body.get('multi_turn_correct') is True
        state['temporary_delivery_confirmed'] = temporary and body.get('received_on_phone') is True
        state['temporary_multi_turn_confirmed'] = temporary and body.get('multi_turn_correct') is True
        outcome = {k: v for k, v in state.items() if k != 'last_error'}
        outcome['timestamp'] = int(time.time())
        verified = (state['temporary_delivery_confirmed'] and state['temporary_multi_turn_confirmed']) if temporary else (
            state['human_delivery_confirmed'] and state['multi_turn_human_confirmed'])
        outcome['result'] = 'PASS' if verified else 'UNCONFIRMED'
        outcome['acceptance_scope'] = 'temporary_tester_only' if temporary else 'owner'
        filename = 'wechat-temporary-verification.json' if temporary else 'wechat-verification.json'
        (settings['runtime'] / filename).write_text(json.dumps(outcome, indent=2), encoding='utf-8')
        return outcome

    @app.post('/local/stop')
    async def local_stop():
        app.state.server.should_exit = True
        return {'ok': True}

    @app.post('/internal/stop')
    async def internal_stop(request: Request):
        if not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + key):
            raise HTTPException(401, 'Authentication required')
        app.state.server.should_exit = True
        return {'ok': True}

    return app


if __name__ == '__main__':
    quiet()
    try:
        settings = load_instance(sys.argv[1])
        app = create_bridge(settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=settings['bridge_port'], access_log=False, log_level='critical'))
        app.state.server = server
        server.run()
    except Exception:
        print('WeChat instance stopped: operation failed; credential diagnostics suppressed.')
        sys.exit(1)
