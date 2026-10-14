"""Main-chat submission ACKs and all-new-assistant observation are separate.

The OpenAI completion path stays available. This internal API never reports an
ACK as an assistant completion. Credentials and attachment bytes stay in RAM.
"""
import base64
import hashlib
import json
import secrets
import time
from pathlib import Path

from feed_store import FeedStore
from native_chat import SCRIPT,NativeUIError
from message_policy import ChatFailure
from media_memory import MemoryAttachment,MAX_FILE,safe_name,workspace_path

FEED_SCRIPT=SCRIPT.replace('return {probe, bind, diagnose};',
    Path(__file__).with_name('native_feed.js').read_text(encoding='utf-8')+'\nreturn {feedBind};')


class MainFeed:
    def __init__(self,native,settings,journal,credentials=lambda:()):
        self.native,self.settings,self.journal=native,settings,journal
        self.credentials=credentials
        self.namespace='__museFeed_'+secrets.token_hex(16)
        self.store=FeedStore(':memory:');self.store_on_disk=False
        self.enabled=False;self.error=None;self.submission_uncertain=self.store.unresolved_input()
        self.generation_halted=False
        self.submitted=0;self.observed=0
        self.poll_delay=1.0

    def scrub(self,text):
        for secret in self.credentials():
            if isinstance(secret,str) and len(secret)>=8:text=text.replace(secret,'[credential omitted]')
        return text

    def call(self,method,*args,await_promise=False):
        self.native._verify()
        try:
            return self.native._evaluate('window['+json.dumps(self.namespace)+'].'+method+'('+
                ','.join(json.dumps(a,ensure_ascii=True) for a in args)+')',await_promise)
        except NativeUIError as e:
            self.error='authentication_failed' if e.code=='authentication_required' else e.code
            raise ChatFailure(self.error,'Muse 主聊天监听未就绪，请检查专用页面与本人认证；不会回退或重新生成。') from None

    def enable(self):
        # A failed rebind must never leave a newly selected account enabled.
        self.enabled=False;self.error='main_feed_not_ready'
        self.native.probe()
        # Rebuilding a context must load the latest reviewed local media parser.
        current_script=SCRIPT.replace('return {probe, bind, diagnose};',
            Path(__file__).with_name('native_feed.js').read_text(encoding='utf-8')+'\nreturn {feedBind};')
        self.native._evaluate('window['+json.dumps(self.namespace)+']=('+current_script+').feedBind();true')
        snapshot=self.call('scan',None,await_promise=True)
        # First activation starts at NOW, never dumps old private history.
        # Later process restarts keep the durable cursor and backfill via after_seq.
        cursor=max((r['seq'] for r in snapshot['rows']),default=0)
        if not self.store_on_disk:
            self.store.close();self.store=FeedStore(self.settings['runtime']/'main-feed.sqlite3');self.store_on_disk=True
        self.submission_uncertain=self.store.unresolved_input()
        self.store.initialize(snapshot['owner'],cursor)
        self.enabled=True;self.error=None
        try:self._recover_reviewed_reply(snapshot['owner'])
        except Exception:
            self.enabled=False;self.error='recovery_reply_unconfirmed';raise
        return self.status()

    def _recover_reviewed_reply(self,owner):
        import hashlib
        path=self.settings['runtime']/'feed-recovery-request.json'
        if not path.is_file():return
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('owner_fingerprint')!=hashlib.sha256(owner.encode()).hexdigest():
            raise ChatFailure('recovery_owner_mismatch','待补发项所属 Muse 账号不一致，已保留原文并停止启用；请人工核查。')
        seq=data['seq'];snapshot=self.call('scan',seq-1,await_promise=True)
        time.sleep(3.1);snapshot=self.call('scan',seq-1,await_promise=True)
        row=next((r for r in snapshot['rows'] if r['seq']==seq and r['message_id']==data['message_id']),None)
        if not row or not row['ready'] or row['text']!=data['text'] or row['role']!='assistant' or not row['main']:
            raise ChatFailure('recovery_reply_unconfirmed','待补发原生回复尚未核对一致，不会重新生成或插入未经确认的文本。')
        self.store.ingest([row],self.scrub,recover=True)
        self.journal.chat('muse.feed_recovered',text=row['text'],message_id=row['message_id'])

    def poll(self):
        if not self.enabled:return
        try:
            snapshot=self.call('scan',self.store.get('cursor'),await_promise=True)
            # Poll faster during generation/settling, retaining the quieter
            # cadence when idle. Each poll still verifies browser ownership.
            self.poll_delay=.4 if snapshot.get('busy') or any(not r['ready'] for r in snapshot['rows']) else 1.0
            self.store.initialize(snapshot['owner'],self.store.get('cursor'))
            before=self.store.get('cursor')
            self.store.ingest(snapshot['rows'],self.scrub)
            if any(r['seq']>before and r['role']=='assistant' and r.get('status') in ('failed','interrupted') for r in snapshot['rows']):
                self.generation_halted=True
            self.observed+=sum(r['seq']>before and r['seq']<=self.store.get('cursor') and r['role']=='assistant' and r['main'] for r in snapshot['rows'])
            for row in snapshot['rows']:
                if before<row['seq']<=self.store.get('cursor') and row['role']=='assistant' and row['main']:
                    self.journal.chat('muse.feed_observed',text=row['text'],message_id=row['message_id'])
            self.error=None
        except ChatFailure as e:self.error=e.reason;raise
        except Exception:
            self.error='main_feed_read_failed'
            raise ChatFailure(self.error,'Muse 新消息读取失败，待发送队列已保留；请核查主要聊天页面。') from None

    def submit(self,rid,text,files,source_id):
        if not self.enabled:raise ChatFailure('main_feed_not_ready','主聊天转发尚未启用，本条未提交。')
        if self.submission_uncertain:raise ChatFailure('submission_unresolved','上一条提交结果未确认，请核查主要聊天；没有自动重发。')
        if self.generation_halted:raise ChatFailure('native_generation_failed','Muse 有生成失败记录，请人工核查本人账号；未自动重新生成。')
        readiness=self.readiness()
        if not readiness['ready']:
            raise ChatFailure(readiness['reason'],'Muse 正在处理消息或有网页草稿，本条尚未提交；入站队列会等待。')
        fingerprint=hashlib.sha256(json.dumps([text,[f.metadata() for f in files]],ensure_ascii=True,sort_keys=True).encode()).hexdigest()
        if not self.store.claim_input(source_id,fingerprint):return {'phase':'duplicate','request_id':rid}
        try:
            self.submission_uncertain=True
            result=self.call('submit',rid,text,[f.wire() for f in files])
            deadline=time.monotonic()+35
            while result['phase']=='submitting' and time.monotonic()<deadline:
                time.sleep(.2);result=self.call('attempt',rid)
            if result['phase']!='acknowledged':raise ChatFailure('submission_unresolved','消息提交结果未确认，已停止新提交，请到 Muse 核查；没有自动重发。')
            self.submission_uncertain=False;self.submitted+=1
            self.store.input_result(source_id,'acknowledged')
            self.journal.event('api.submission_acknowledged',request_id=rid,message_id=result['message_id'])
            return {'phase':'acknowledged','message_id':result['message_id'],'request_id':rid,
                'assistant_completion_verified':False}
        except NativeUIError as e:
            if e.code in ('main_chat_busy','composer_has_user_draft'):
                self.submission_uncertain=False;self.store.input_result(source_id,'not_submitted')
            raise
        except ChatFailure as e:
            if e.reason in ('main_chat_busy','composer_has_user_draft'):
                self.submission_uncertain=False
            self.store.input_result(source_id,'uncertain' if self.submission_uncertain else 'not_submitted')
            raise
        except Exception:
            self.store.input_result(source_id,'uncertain')
            raise ChatFailure('submission_unresolved','Muse 提交结果未确认，已停止新提交；请到主要聊天核对，没有自动重发。') from None

    def readiness(self):
        if not self.enabled:return {'ready':False,'reason':'main_feed_not_ready'}
        if self.submission_uncertain or self.store.unresolved_input():return {'ready':False,'reason':'submission_unresolved'}
        if self.generation_halted:return {'ready':False,'reason':'native_generation_failed'}
        probe=self.native.probe()
        if probe['main_busy']:return {'ready':False,'reason':'main_chat_busy'}
        if probe['has_user_draft']:return {'ready':False,'reason':'composer_has_user_draft'}
        return {'ready':True,'reason':None}

    def attachment(self,info):
        workspace_path(info['path']);name=safe_name(info['filename']);data=bytearray()
        while True:
            chunk=self.call('download',info['path'],len(data),min(1048576,MAX_FILE-len(data)+1),await_promise=True)
            raw=base64.b64decode(chunk['data_base64'],validate=True)
            if len(raw)!=chunk['len'] or len(data)+len(raw)>MAX_FILE or (not raw and not chunk['eof']):
                raise ChatFailure('reply_file_too_large','Muse 附件超过当前内存传输能力，请到主要聊天下载原件。')
            data.extend(raw)
            if chunk['eof']:break
        mime=info['mime']
        return MemoryAttachment(name,mime,'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'file',bytes(data))

    def status(self):
        return {'enabled':self.enabled,'error':self.error,'submission_uncertain':self.submission_uncertain,
            'generation_halted':self.generation_halted,
            'submitted_acknowledged':self.submitted,'assistant_messages_observed':self.observed,**self.store.status()}

    def close(self):
        self.enabled=False;self.store.close()
