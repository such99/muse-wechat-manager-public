"""Fixed native main chat, raw current input, guarded reply anchors and media."""
import base64
import json
import secrets
import time
import re
from pathlib import Path

from message_policy import ChatFailure
from media_memory import MemoryAttachment, MAX_FILE, MAX_TOTAL, MAX_FILES, safe_name, workspace_path

SCRIPT = (Path(__file__).with_name('native_ui.js')).read_text(encoding='utf-8')


class NativeUIError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


class NativeMainChat:
    def __init__(self, engine):
        self.engine = engine
        self.page = None
        self.target_id = None
        self.context_id = None
        self.namespace = '__museWechat_' + secrets.token_hex(16)
        self.bound = False
        self.unresolved = False
        self.halted_reason = None
        self.completed = 0

    def status(self):
        return {'bound': self.bound, 'unresolved': self.unresolved, 'completed': self.completed,
            'halted_reason':self.halted_reason,
            'reply_association':'guarded_batch_anchor', 'completion_rule':'main_idle_stop_absent_batch_stable_3000ms',
            'max_reply_bubbles':8,
            'routing': 'native_main', 'history_replay': False, 'proactive_forwarding': False}

    def prepare(self,wait_seconds=35):
        self.engine.verify_browser()
        if not self.engine.current_context_id:
            raise ChatFailure('isolated_session_missing', '请先在本实例隐身窗口完成 Muse 认证。')
        if self.page and self.context_id == self.engine.current_context_id:
            return self.probe()
        browser = self.engine.browser
        self.context_id = self.engine.current_context_id
        targets=browser.send('Target.getTargets')['result']['targetInfos']
        roots=[t for t in targets if t.get('type')=='page' and t.get('url')=='https://muse.ai/'
            and t.get('browserContextId')==self.context_id]
        if len(roots)>1:
            raise ChatFailure('main_pages_ambiguous','本实例有多个主聊天页，请保留本人确认的一个页面后再核查。')
        if roots:
            # Attach to the user's already hydrated, explicitly confirmed root.
            # Do not navigate, click or close that page during preparation.
            self.target_id=roots[0]['targetId']
        else:
            self.target_id = browser.send('Target.createTarget', {'url':'https://muse.ai/',
                'browserContextId':self.context_id, 'newWindow':True})['result']['targetId']
            browser.send('Target.activateTarget',{'targetId':self.target_id})
            window=browser.send('Browser.getWindowForTarget',{'targetId':self.target_id})['result']
            if window.get('bounds',{}).get('windowState')=='minimized':
                browser.send('Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'normal'}})
        self.page = self.engine._connect(f'ws://127.0.0.1:{self.engine.cfg.cdp_port}/devtools/page/{self.target_id}')
        if not roots:self.page.send('Page.bringToFront')
        deadline = time.monotonic() + wait_seconds
        last_reason='main_not_ready'
        while time.monotonic() < deadline:
            try:
                return self.probe()
            except ChatFailure as error:
                last_reason=error.reason
                time.sleep(.3)
        raise ChatFailure(last_reason, '主聊天尚未就绪，请检查本实例 Muse 页面；不会回退旁聊。')

    def _verify(self):
        self.engine.verify_browser()
        if not self.page or self.context_id != self.engine.current_context_id:
            self.bound = False
            raise ChatFailure('main_session_changed', 'Muse 会话已变化，请重新确认本人的主聊天；不会回退旁聊。')
        targets = self.engine.browser.send('Target.getTargets')['result']['targetInfos']
        matches = [t for t in targets if t.get('targetId') == self.target_id]
        if len(matches) != 1 or matches[0].get('browserContextId') != self.context_id or matches[0].get('url') != 'https://muse.ai/':
            self.bound = False
            raise ChatFailure('main_route_changed', '固定主聊天页面已关闭或改变，请重新确认；不会回退旁聊。')

    def probe(self):
        self._verify()
        try:
            result = self._evaluate('(' + SCRIPT + ').probe()')
            if not isinstance(result, dict) or result.get('main') is not True:
                raise ValueError('Invalid probe')
            return result
        except NativeUIError as error:
            raise ChatFailure('authentication_failed' if error.code=='authentication_required' else error.code,
                '未确认原生主聊天、消息接口或登录状态，请检查本实例页面。') from None
        except Exception:
            raise ChatFailure('main_probe_failed', '未确认原生主聊天、消息接口或登录状态，请检查本实例页面。') from None

    def bind(self):
        if self.halted_reason:
            raise ChatFailure(self.halted_reason,'上次原生生成失败，已停止调用；请先人工检查主要聊天及本人额度，不能重新绑定来重试。')
        if self.bound and self.unresolved:
            raise ChatFailure('submission_unresolved', '上一条提交未确认；请先核对原生主聊天，不能重新绑定来自动重发。')
        self.probe()
        try:
            if self._evaluate('(() => {window[' + json.dumps(self.namespace) + ']=(' + SCRIPT + ').bind(); return true})()') is not True:
                raise ValueError('Invalid binding')
        except Exception:
            raise ChatFailure('main_bind_failed', '主聊天尚有草稿、引用或接口未就绪，请处理后重新确认。') from None
        self.bound, self.unresolved = True, False
        return self.status()

    def _evaluate(self, expr, await_promise=False):
        # Upstream CDP.js returns exceptionDetails as a dict instead of raising.
        # Always inspect the protocol envelope; never treat that dict as success.
        reply = self.page.send('Runtime.evaluate', {'expression': expr, 'returnByValue': True, 'awaitPromise': await_promise}, timeout=30)
        if reply.get('result', {}).get('exceptionDetails'):
            # Classify a closed list of static JS errors; never return the stack.
            description=reply['result']['exceptionDetails'].get('exception',{}).get('description','')
            match=re.match(r'^Error: ([a-z_]+)(?:\n|$)',description)
            code=match.group(1) if match and match.group(1) in {
                'authentication_required','composer_missing','context_ambiguous','main_route_required',
                'effective_route_not_main','main_not_ready','main_identity_missing','main_identity_changed',
                'composer_has_user_draft','previous_submission_unresolved','request_capacity_reached',
                'attachment_path_missing','attachment_path_rejected','invalid_media_payload',
                'main_chat_busy','message_anchor_unavailable','bubble_identity_ambiguous',
                'feed_history_shape_changed','invalid_feed_cursor'} else 'native_ui_changed'
            raise NativeUIError(code)
        value = reply['result']['result']
        if 'value' not in value:
            raise ValueError('Native value missing')
        return value['value']

    def _call(self, method, *args, await_promise=False):
        self._verify()
        expr = 'window[' + json.dumps(self.namespace) + '].' + method + '(' + ','.join(json.dumps(a,ensure_ascii=False) for a in args) + ')'
        try:
            return self._evaluate(expr, await_promise)
        except ChatFailure:
            raise
        except NativeUIError as error:
            if method=='start' and error.code in ('main_chat_busy','composer_has_user_draft','message_anchor_unavailable','bubble_identity_ambiguous'):
                raise ChatFailure(error.code,'主聊天正在生成、有草稿或锚点不唯一，本条尚未提交；请等网页完成并保持主要聊天页面稳定。') from None
            raise ChatFailure('authentication_failed' if error.code=='authentication_required' else 'native_operation_unconfirmed',
                'Muse 认证或主聊天状态已变化，停止本次调用；请检查服务器上的本人账号和原对话。') from None
        except Exception:
            raise ChatFailure('native_operation_unconfirmed', 'Muse 操作结果未确认，未自动重试；请检查主聊天和本机状态。') from None

    def reply(self, request_id, text, files):
        if self.halted_reason:
            raise ChatFailure(self.halted_reason,'上次 Muse 生成失败，已停止新调用；请人工检查主要聊天及本人额度。')
        if not self.bound:
            raise ChatFailure('main_not_bound', '请在服务器管理页确认账号使用者的原生主聊天，本条未提交。')
        if self.unresolved:
            raise ChatFailure('submission_unresolved', '上一条 Muse 提交结果未确认，已停止新调用；请在服务器核对原对话，勿重复发送。')
        if not isinstance(request_id, str) or len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id):
            raise ChatFailure('invalid_request_id', '消息关联标识无效，本条未提交。')
        if not isinstance(text,str) or len(text)>16000 or len(files)>MAX_FILES or sum(len(f.data) for f in files)>MAX_TOTAL:
            raise ChatFailure('input_too_large', '消息或附件超过本实例大小限制，本条未提交。')
        # Latch BEFORE submission. Observer exceptions/timeouts never unlock it.
        self.unresolved = True
        try:
            start = self._call('start', request_id, text, [f.wire() for f in files])
        except ChatFailure as error:
            if error.reason in ('main_chat_busy','composer_has_user_draft','message_anchor_unavailable','bubble_identity_ambiguous'):
                self.unresolved=False # Reviewed start preflight; sendMessage has not run.
            raise
        if start.get('phase') == 'failed':
            self.unresolved = False
            raise ChatFailure('attachment_rejected', '附件未能交给 Muse，本条未提交，请检查文件格式。')
        deadline = time.monotonic() + 210
        while time.monotonic() < deadline:
            result = self._call('read', request_id)
            if result.get('phase') in ('correlation_missing','anchor_ambiguous'):
                self.halted_reason = 'native_anchor_ambiguous' if result['phase']=='anchor_ambiguous' else 'native_correlation_missing'
                # The input WAS submitted. Keep the unresolved latch and do not
                # release, rebind, resend, or report a successful API completion.
                raise ChatFailure(self.halted_reason,
                    '消息已提交到 Muse 主要聊天，但回复锚点出现歧义或页面变化，已暂停微信转发；请到 Muse 核对。没有自动重发。')
            if result.get('phase') == 'completed':
                break
            if result.get('phase') == 'failed':
                self._call('release', request_id)
                self.unresolved = False
                # The native terminal status alone cannot distinguish quota from
                # a temporary backend failure. Stop this instance conservatively
                # without falsely invalidating its authentication.
                self.halted_reason='native_generation_failed'
                raise ChatFailure(self.halted_reason, 'Muse 此次生成失败或被中断，已停止新调用；请人工核查主要聊天与本人额度，未自动重新生成。')
            if result.get('phase') in ('missing','uncertain'):
                raise ChatFailure('submission_unresolved', 'Muse 提交结果未确认，已停止新调用；请在服务器核对主要聊天。')
            time.sleep(.2)
        else:
            raise ChatFailure('native_timeout', 'Muse 主聊天回复超时，结果未确认，已停止新调用；未自动重试。')
        bubbles = result.get('messages') or [{'message_id':result.get('message_id'), 'text':result.get('text') or ''}]
        if (not isinstance(bubbles,list) or not 1 <= len(bubbles) <= 8
                or any(not isinstance(b.get('text'),str) or not isinstance(b.get('message_id'),str) for b in bubbles)
                or len({b['message_id'] for b in bubbles}) != len(bubbles)):
            raise ChatFailure('submission_unresolved','Muse 回复批次结构未确认，已停止新调用；请查看主要聊天，未重新生成。')
        messages=[{'message_id':b['message_id'],'text':b['text'],'attachment_indexes':[],'media_errors':[]} for b in bubbles]
        output, errors, total = [], [], 0
        returned = result.get('files') or []
        if len(returned) > MAX_FILES:
            errors.append('too_many_reply_files')
            for b in messages:b['media_errors'].append('too_many_reply_files')
        for index, info in enumerate(returned[:MAX_FILES]):
            owner=info.get('bubble_index',0)
            if type(owner) is not int or not 0 <= owner < len(messages):
                raise ChatFailure('submission_unresolved','Muse 附件所属气泡未确认，已停止新调用；请查看主要聊天。')
            try:
                workspace_path(info.get('path'))
                name = safe_name(info.get('filename'))
                data = bytearray()
                while True:
                    remaining = min(MAX_FILE-len(data),MAX_TOTAL-total)
                    chunk = self._call('download', request_id, index, len(data), min(1048576,remaining+1), await_promise=True)
                    raw = base64.b64decode(chunk['data_base64'],validate=True)
                    if len(raw)!=chunk['len'] or len(raw)>remaining or (not raw and not chunk['eof']):
                        raise ValueError('Reply file exceeded bounds')
                    data.extend(raw);total+=len(raw)
                    if chunk['eof']:
                        break
                mime = info.get('mime') or 'application/octet-stream'
                if not isinstance(mime,str) or len(mime)>160 or any(ord(c)<32 for c in mime):
                    raise ValueError('Invalid MIME')
                messages[owner]['attachment_indexes'].append(len(output))
                output.append(MemoryAttachment(name,mime,'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'file',bytes(data)))
            except Exception:
                errors.append('reply_attachment_unavailable')
                messages[owner]['media_errors'].append('reply_attachment_unavailable')
        self._call('release',request_id)
        self.unresolved = False
        text = '\n\n'.join(b['text'] for b in messages)
        if any(not b['text'].strip() and not b['attachment_indexes'] and not b['media_errors'] for b in messages):
            raise ChatFailure('empty_reply','Muse 返回空回复，请查看主要聊天；本次未重新生成。')
        self.completed += 1
        return {'text':text,'attachments':[f.wire() for f in output], 'media_errors':errors,
            'message_id':result.get('message_id'),'messages':messages,
            'routing':'native_main','history_replay':False}

    def close(self):
        self.bound=False
        if self.page:
            self.page.close()
        self.page=None
