"""No-history bridge transport for native main chat; exactly the current input."""
import base64
import uuid

import requests

from message_policy import ChatFailure
from media_memory import MemoryAttachment, MAX_FILE, MAX_TOTAL, MAX_FILES, safe_name


class NativeChatManager:
    def __init__(self,settings,key,session,journal):
        self.url=f"http://127.0.0.1:{settings['muse_port']}/v1/chat/completions"
        self.key,self.session,self.journal=key,session,journal
        self.blocked=None

    def clear_all_histories(self):
        pass  # There is no local replay history. Never clear the real main chat.

    def chat(self,user_id,text,message_id=None,attachments=()):
        if self.blocked:
            raise ChatFailure(self.blocked,'本实例真实调用已停止，请在服务器核查本人账号或未确认的提交。不会切换账号。')
        if not isinstance(text,str) or len(text)>16000:
            raise ChatFailure('input_too_long','消息超过 16000 字符，请缩短；本条未提交。')
        rid=uuid.uuid4().hex
        self.journal.api_request(rid,[{'role':'user','content':text}],user_id=user_id,message_id=message_id)
        self.journal.media('api.attachments',[f.metadata() for f in attachments],request_id=rid,message_id=message_id)
        try:
            with self.session.post(self.url,headers={'Authorization':'Bearer '+self.key,'X-Request-ID':rid},
                    json={'model':'muse-spark','messages':[{'role':'user','content':text}],
                        'muse_route':'native_main','muse_attachments':[f.wire() for f in attachments],'stream':False},
                    timeout=(5,300)) as response:
                if response.status_code==401:
                    self.blocked='authentication_failed'
                    raise ChatFailure(self.blocked,'Muse 认证失效，已停止本人实例调用；请在服务器重新认证。')
                if response.status_code==429:
                    self.blocked='upstream_limit'
                    raise ChatFailure(self.blocked,'Muse 额度或限流阻止调用，已停止本人实例；请检查账号，不能借用其他人额度。')
                if response.status_code==409:
                    body=response.json();code=body.get('code')
                    if code in ('native_correlation_missing','native_anchor_ambiguous'):
                        self.blocked = code
                        raise ChatFailure(code,
                            '消息已提交到 Muse 主要聊天，但回复锚点出现歧义或页面变化，已暂停微信转发；请到 Muse 核对。没有自动重发。')
                    if code in ('main_chat_busy','composer_has_user_draft','message_anchor_unavailable','bubble_identity_ambiguous'):
                        raise ChatFailure(code,'主聊天正在生成、有草稿或锚点不唯一，本条尚未提交；请等网页完成并保持主要聊天页面稳定。')
                    if code in ('submission_unresolved','native_timeout','native_operation_unconfirmed','native_generation_failed'):
                        self.blocked='native_generation_failed' if code=='native_generation_failed' else 'submission_unresolved'
                    raise ChatFailure(code if isinstance(code,str) and code in (
                        'submission_unresolved','native_timeout','native_operation_unconfirmed','main_not_bound',
                        'main_route_changed','main_session_changed','main_probe_failed','main_bind_failed','native_generation_failed') else 'main_not_ready',
                        '主聊天未就绪或上一条结果未确认，本条未自动重试；请查看服务器管理页和 Muse 原对话。')
                if response.status_code in (413,422):
                    raise ChatFailure('attachment_rejected','消息或附件核验失败，本条未提交；请检查大小与文件格式。')
                response.raise_for_status();result=response.json()
            if result.get('muse',{}).get('routing')!='native_main' or result.get('muse',{}).get('history_replay') is not False:
                raise ValueError('Wrong routing response')
            text_out=result['choices'][0]['message']['content'];entries=result.get('muse_attachments')
            if not isinstance(text_out,str) or not isinstance(entries,list) or len(entries)>MAX_FILES:
                raise ValueError('Invalid native response')
            files,total=[],0
            for f in entries:
                name=safe_name(f['filename']);encoded=f['data_base64']
                if not isinstance(encoded,str) or len(encoded)>((MAX_FILE+2)//3)*4:
                    raise ValueError('Oversized file response')
                data=base64.b64decode(encoded,validate=True);total+=len(data)
                if len(data)>MAX_FILE or total>MAX_TOTAL:
                    raise ValueError('Oversized file response')
                mime=f['mime']
                if not isinstance(mime,str) or len(mime)>160 or any(ord(c)<32 for c in mime):
                    raise ValueError('Invalid MIME')
                files.append(MemoryAttachment(name,mime,f.get('category','file'),data))
            errors=result.get('media_errors') or []
            if not text_out.strip() and not files and not errors:
                raise ChatFailure('empty_reply','Muse 返回空回复；请查看主要聊天，本次未重新生成。')
            raw_bubbles=result.get('muse_messages')
            if raw_bubbles is None and result.get('muse',{}).get('reply_association')=='guarded_batch_anchor':
                raise ValueError('Batch reply structure missing')
            raw_bubbles=raw_bubbles if raw_bubbles is not None else [{'message_id':None,'text':text_out,
                'attachment_indexes':list(range(len(files))),'media_errors':errors}]
            if not isinstance(raw_bubbles,list) or not 1 <= len(raw_bubbles) <= 8:
                raise ValueError('Invalid reply batch size')
            messages,used,ids=[],[],[]
            for bubble in raw_bubbles:
                text=bubble['text'];indexes=bubble['attachment_indexes'];native_id=bubble.get('message_id')
                if not isinstance(text,str) or not isinstance(indexes,list) or any(type(i) is not int or not 0<=i<len(files) for i in indexes):
                    raise ValueError('Invalid reply bubble')
                if native_id is not None:
                    if not isinstance(native_id,str) or not native_id or len(native_id)>256:raise ValueError('Invalid message ID')
                    ids.append(native_id)
                used.extend(indexes)
                item={'message_id':native_id,'text':text,'attachments':[files[i] for i in indexes],
                    'media_errors':bool(bubble.get('media_errors'))}
                if not text.strip() and not item['attachments'] and not item['media_errors']:
                    raise ChatFailure('empty_reply','Muse 的回复批次包含空气泡，本次未转发；请查看主要聊天，未重新生成。')
                messages.append(item)
            if (sorted(used)!=list(range(len(files))) or len(set(ids))!=len(ids)
                    or (len(messages)>1 and len(ids)!=len(messages))
                    or '\n\n'.join(b['text'] for b in messages)!=text_out):
                raise ValueError('Reply batch ownership or aggregate mismatch')
            if result.get('muse',{}).get('message_ids',ids)!=ids:
                raise ValueError('Reply batch ID order mismatch')
            self.journal.chat('api.reply',text=text_out,user_id=user_id,message_id=message_id,request_id=rid)
            for bubble in messages:
                self.journal.chat('api.reply_bubble',text=bubble['text'],message_id=bubble['message_id'],request_id=rid)
            self.journal.media('api.reply_attachments',[f.metadata() for f in files],request_id=rid,message_id=message_id)
            return {'text':text_out,'attachments':files,'media_errors':bool(errors),'messages':messages,'request_id':rid}
        except ChatFailure as error:
            self.journal.event('api.failed',request_id=rid,reason=error.reason)
            raise
        except requests.Timeout:
            self.blocked='submission_unresolved'
            self.journal.event('api.failed',request_id=rid,reason='timeout_no_retry')
            raise ChatFailure('submission_unresolved','Muse 请求超时，已停止新调用；请核对主要聊天，未自动重试。') from None
        except Exception:
            self.blocked='submission_unresolved'
            self.journal.event('api.failed',request_id=rid,reason='native_result_unconfirmed')
            raise ChatFailure('submission_unresolved','Muse 主聊天结果未确认，已停止新调用；请核对主要聊天。') from None
