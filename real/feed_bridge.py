"""Ordered all-assistant outbox sender. WeChat auth/context remain memory only."""
import base64
import hashlib
import json
import threading
import time
import uuid

import requests

from message_policy import ChatFailure
from media_memory import MemoryAttachment
from ilink import ILinkAPIError


class FeedBridge:
    def __init__(self,settings,key,client,journal,stop,binding,state):
        self.settings_instance=settings.get('instance','')
        self.base=f"http://127.0.0.1:{settings['muse_port']}/v1/muse"
        self.key,self.client,self.journal,self.stop,self.binding,self.state=key,client,journal,stop,binding,state
        self.session=requests.Session();self.session.trust_env=False
        self.submit_session=requests.Session();self.submit_session.trust_env=False
        self.thread=None;self.context_version=None;self.context_count=0;self.wait_version=None
        self.state['application_send_limit']=None

    def api(self,path,body=None,session=None,headers=None):
        session=session or self.session
        kw={'headers':{'Authorization':'Bearer '+self.key,**(headers or {})},'timeout':(5,50)}
        response=session.get(self.base+path,**kw) if body is None else session.post(self.base+path,json=body,**kw)
        try:
            if response.status_code==401:raise ChatFailure('authentication_failed','Muse 认证失效，请在服务器重新认证本人实例。')
            if response.status_code>=400:
                data=response.json()
                if response.status_code in (413,422):
                    raise ChatFailure('input_rejected','文字或附件超出支持范围，本条未提交；请缩短文字或改用支持的文件格式。')
                raise ChatFailure(data.get('code','feed_operation_failed'),data.get('notice','主聊天队列操作未完成，请检查服务器管理页。'))
            return response.json()
        finally:response.close()

    def start(self):
        if not self.thread:
            self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
        if hasattr(self,'incoming'):self.incoming.start()

    def readiness(self):return self.api('/ready',session=self.submit_session)

    def clear_all_histories(self):pass

    def chat(self,user_id,text,message_id=None,attachments=()):
        rid=uuid.uuid4().hex
        source=hashlib.sha256(json.dumps([user_id,message_id],ensure_ascii=True).encode()).hexdigest()
        self.journal.api_request(rid,[{'role':'user','content':text}],user_id=user_id,message_id=message_id)
        self.journal.media('api.attachments',[f.metadata() for f in attachments],request_id=rid)
        result=self.api('/submit',{'model':'muse-spark','messages':[{'role':'user','content':text}],
            'muse_attachments':[f.wire() for f in attachments],'stream':False},session=self.submit_session,
            headers={'X-Request-ID':rid,'X-Source-ID':source})
        self.journal.event('api.submission_result',request_id=rid,status=result['phase'])
        return result

    def notice(self,uid,mid,text):
        source=hashlib.sha256(json.dumps([uid,mid,'notice']).encode()).hexdigest()
        self.api('/notice',{'text':text,'message_id':source},session=self.submit_session)

    def resolve_input(self,uid,mid,resolution):
        status=self.api('/feed',session=self.submit_session)
        # Must match chat()'s instance-scoped source identity exactly.
        source=hashlib.sha256(json.dumps([self.settings_instance+':'+uid,mid],ensure_ascii=True).encode()).hexdigest()
        return self.api('/resolve-input',{'source_id':source,'resolution':resolution,
            'owner_fingerprint':status.get('owner_fingerprint'),'confirm_checked_main_chat':True},session=self.submit_session)

    def transition(self,head,action,uid):
        return self.api('/outbox',{'n':head['n'],'action':action,'target':uid})

    def failure_notice(self,uid,context):
        # One distinct notice, never retry the original or the notice itself.
        text='【Muse 消息发送结果未确认，队列已暂停，没有自动重发。请在服务器管理页核对，或到 Muse 主要聊天查看原文。】'
        try:
            self.journal.chat('wechat.outgoing',text=text,user_id=uid,exclude=(context,))
            self.client.send_text(uid,text,context)
            self.context_count+=1;self.state['accepted_notices']+=1
        except Exception:
            self.journal.event('wechat.failure_notice_unconfirmed',user_id=uid,reason='no_retry')

    def loop(self):
        # Keep sends paced, without adding a whole second to every queued item.
        while not self.stop.wait(.4):
            try:
                uid,context,version=self.binding()
                status=self.api('/feed');self.state['main_feed']=status
                if not uid or not self.client.logged_in:continue
                if not context:
                    self.state['queue_waiting_reason']='wechat_reply_context_required';continue
                if version!=self.context_version:
                    self.context_version=version;self.context_count=0
                head=status.get('head')
                if not head:
                    self.state['queue_waiting_reason']=None;continue
                if head['target'] and head['target']!=uid:
                    self.state['queue_waiting_reason']='target_changed_manual_review_required';continue
                if head['state']=='waiting':
                    if head['reason']=='wechat_requires_user_interaction' and version!=self.wait_version:
                        self.transition(head,'resume',uid)
                    else:self.state['queue_waiting_reason']=head['reason']
                    continue
                if head['state']!='pending':
                    if head['state']=='sending':
                        self.transition(head,'uncertain',uid);self.failure_notice(uid,context)
                    self.state['queue_waiting_reason']='send_result_unknown_manual_review_required';continue
                # No local send-count ceiling. Let the actual API response
                # decide whether this context can deliver the next item.
                claimed=self.transition(head,'claim',uid)['head']
                if self.stop.is_set():
                    self.transition(head,'uncertain',uid);break
                try:
                    if claimed['kind']=='text':
                        text=claimed['body']['text']
                        self.journal.chat('wechat.outgoing',text=text,user_id=uid,message_id=head['message_id'],exclude=(context,))
                        self.client.send_text(uid,text,context)
                        self.state['accepted_text_bubbles']+=1
                    else:
                        f=claimed['attachment'];raw=base64.b64decode(f['data_base64'],validate=True)
                        attachment=MemoryAttachment(f['filename'],f['mime'],f['category'],raw)
                        self.journal.media('wechat.outgoing_attachment',[attachment.metadata()],message_id=head['message_id'])
                        if attachment.mime.startswith('image/'):self.client.send_image(uid,raw,context)
                        elif attachment.mime in ('video/mp4','video/quicktime'):self.client.send_video(uid,raw,context)
                        else:self.client.send_file(uid,raw,context,file_name=attachment.filename)
                        self.state['accepted_media_sends']+=1
                except Exception as e:
                    # Only a parsed, definite iLink business rejection is retryable
                    # AFTER a new inbound context. Timeouts are uncertain forever.
                    if isinstance(e,ILinkAPIError) and (e.errcode==-2 or e.ret==-2):
                        self.transition(head,'rejected',uid);self.wait_version=version
                        self.state['queue_waiting_reason']='wechat_requires_user_interaction'
                        self.journal.event('wechat.send_rejected',message_id=head['message_id'],
                            reason='ilink_business_rejected',status=f'ret={e.ret};errcode={e.errcode}')
                    else:
                        self.transition(head,'uncertain',uid)
                        self.context_count+=1 # Diagnostic count; this send may have arrived.
                        self.failure_notice(uid,context)
                        self.state['queue_waiting_reason']='send_result_unknown_manual_review_required'
                    self.state['failed']+=1;continue
                self.context_count+=1
                # If this ACK write fails the item remains sending/uncertain;
                # it will never be sent twice automatically, even across restart.
                self.transition(head,'sent',uid)
                is_notice=head['message_id'].startswith('notice:')
                self.state['accepted_notices' if is_notice else 'accepted_sends']+=1
                self.state['queue_waiting_reason']=None
                self.journal.event('wechat.feed_send_accepted',user_id=uid,message_id=head['message_id'],status=str(head['n']))
            except ChatFailure as e:
                self.state['last_error']=e.reason
                if e.reason=='authentication_failed':
                    self.state['queue_waiting_reason']='authentication_failed';break
            except Exception:
                self.state['last_error']='feed_operation_unconfirmed'
                # A claim with unknown local result must not be blindly repeated.
                self.stop.wait(2)

    def close(self):
        if hasattr(self,'incoming'):self.incoming.close()
        if self.thread:self.thread.join(3)
        self.key=''
        if not self.thread or not self.thread.is_alive():
            self.session.close();self.submit_session.close()
