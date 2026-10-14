"""Ordered RAM input payloads; durable metadata never contains auth or bytes.

Readiness failures are read-only waits. An ambiguous POST is never retried.
Queued media cannot survive restart under the no-credential/binary-disk policy;
its durable record explicitly says not submitted and needs manual resending.
"""
import hashlib
import json
import sqlite3
import threading
import time
from collections import deque
from message_policy import ChatFailure


class IncomingQueue:
    def __init__(self,settings,prepare,ready,send,notice,journal,stop,clock=time.monotonic,capacity=100):
        self.prepare,self.ready,self.send,self.notice=prepare,ready,send,notice
        self.journal,self.stop,self.clock,self.capacity=journal,stop,clock,capacity
        self.lock=threading.RLock();self.tasks=deque();self.thread=None;self.last_submit=-100
        self.db=sqlite3.connect(settings['runtime']/'incoming.sqlite3',check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS incoming(k TEXT PRIMARY KEY,mid TEXT,state TEXT,reason TEXT,metadata TEXT)')
        self.db.execute("UPDATE incoming SET state='not_submitted_restart',reason='manual_resend_required' WHERE state IN ('queued','preparing','waiting')")
        self.db.execute("UPDATE incoming SET state='uncertain',reason='interrupted_submission_no_retry' WHERE state='submitting'")
        self.db.commit()
        self.halted=bool(self.db.execute("SELECT 1 FROM incoming WHERE state='uncertain' LIMIT 1").fetchone())

    def enqueue(self,uid,mid,items):
        key=hashlib.sha256(json.dumps([uid,mid],ensure_ascii=True).encode()).hexdigest()
        with self.lock,self.db:
            if self.db.execute('SELECT 1 FROM incoming WHERE k=?',(key,)).fetchone():return 'duplicate'
            if len(self.tasks)>=self.capacity:
                self.db.execute("INSERT INTO incoming VALUES(?,?,'not_submitted','queue_capacity',NULL)",(key,mid))
                return 'queue_capacity'
            self.db.execute("INSERT INTO incoming VALUES(?,?,'queued',NULL,NULL)",(key,mid))
            self.tasks.append({'key':key,'uid':uid,'mid':mid,'items':items,'payload':None,'state':'queued','reason':None,'notices':set()})
        self.journal.event('wechat.inbound_queued',message_id=mid)
        return 'queued'

    def record(self,task,state,reason=None):
        if task['state']==state and task['reason']==reason:return
        with self.lock,self.db:
            task.update(state=state,reason=reason)
            self.db.execute('UPDATE incoming SET state=?,reason=? WHERE k=?',(state,reason,task['key']))
        self.journal.event('wechat.inbound_state',message_id=task['mid'],status=state,reason=reason)

    def notify(self,task,reason,text):
        if reason in task['notices']:return
        task['notices'].add(reason)
        try:self.notice(task['uid'],task['mid']+':'+reason,text)
        except Exception:self.journal.event('wechat.inbound_notice_unconfirmed',message_id=task['mid'],reason='no_retry')

    def finish(self,task,state,reason=None):
        self.record(task,state,reason)
        with self.lock:
            assert self.tasks[0] is task
            self.tasks.popleft();task['payload']=None;task['items']=None

    def step(self):
        with self.lock:task=self.tasks[0] if self.tasks else None
        if task is None or self.halted or task['state']=='uncertain' or self.stop.is_set():return
        if task['payload'] is None:
            self.record(task,'preparing')
            try:
                text,files=self.prepare(task['items'])
                if not text.strip() and not files:raise ChatFailure('empty_input','没有读到文字或文件，本条未提交 Muse。')
                task['payload']=(text,files);task['items']=None
                metadata=[f.metadata() for f in files]
                self.journal.media('wechat.attachments',metadata,message_id=task['mid'])
                # The journal performs the exact credential-value scrub. Do not
                # copy filenames/text into a second unguarded persistent store.
            except ChatFailure as e:
                self.notify(task,e.reason,e.notice);self.finish(task,'not_submitted',e.reason);return
            except Exception:
                self.notify(task,'media_prepare_failed','附件准备失败，本条未提交 Muse；请检查后手动重发。')
                self.finish(task,'not_submitted','media_prepare_failed');return
        if self.stop.is_set():return
        try:readiness=self.ready()
        except ChatFailure as e:
            self.record(task,'waiting',e.reason)
            if e.reason=='authentication_failed':self.notify(task,e.reason,'Muse 认证失效，入站队列已暂停；请在服务器重新认证本人账号。')
            return
        except Exception:
            self.record(task,'waiting','readiness_network_failed');return
        if not readiness.get('ready'):
            reason=readiness.get('reason') or 'main_not_ready'
            self.record(task,'waiting',reason)
            if reason not in ('main_chat_busy','main_not_ready'):
                self.notify(task,reason,'消息已保留在入站队列，Muse 暂未就绪；请检查网页草稿、账号或未确认的提交。没有重复调用。')
            return
        if self.clock()-self.last_submit<2:
            self.record(task,'waiting','internal_spacing');return
        self.record(task,'submitting')
        try:
            text,files=task['payload']
            result=self.send(task['uid'],text,message_id=task['mid'],attachments=files)
            if result.get('phase') not in ('acknowledged','duplicate'):raise ValueError('Submission not confirmed')
        except ChatFailure as e:
            if e.reason in ('main_chat_busy','composer_has_user_draft','authentication_failed'):
                # These exact server errors guarantee that no native send ran.
                self.record(task,'waiting',e.reason);return
            if e.reason in ('input_rejected','input_too_long'):
                self.notify(task,e.reason,e.notice);self.finish(task,'not_submitted',e.reason);return
            self.record(task,'uncertain','submission_unconfirmed_no_retry')
            self.halted=True
            self.notify(task,'submission_uncertain','本条提交结果未确认，入站队列已暂停。请到 Muse 核对，没有自动重发。');return
        except Exception:
            self.record(task,'uncertain','submission_unconfirmed_no_retry')
            self.halted=True
            self.notify(task,'submission_uncertain','本条提交结果未确认，入站队列已暂停。请到 Muse 核对，没有自动重发。');return
        self.last_submit=self.clock();self.finish(task,'acknowledged' if result['phase']=='acknowledged' else 'deduplicated')

    def loop(self):
        while not self.stop.wait(.3):
            try:self.step()
            except Exception:
                self.journal.event('wechat.inbound_worker_failed',reason='details_suppressed')
                self.stop.wait(1)

    def start(self):
        if not self.thread:self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def status(self):
        with self.lock:
            head=self.tasks[0] if self.tasks else None
            return {'pending':len(self.tasks),'head':{k:head[k] for k in ('mid','state','reason')} if head else None,
                'halted':self.halted,
                'uncertain_inputs':[{'message_id':r[0],'reason':r[1]} for r in self.db.execute("SELECT mid,reason FROM incoming WHERE state='uncertain'")],
                'counts':{r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM incoming GROUP BY state')},
                'payloads_in_memory_only':True,'restart_requires_manual_resend_for_unsubmitted_media':True}

    def resolve(self,mid,resolution):
        if resolution not in ('acknowledged','not_submitted_reviewed'):raise ValueError('Manual resolution required')
        with self.lock,self.db:
            rows=self.db.execute("SELECT k FROM incoming WHERE mid=? AND state='uncertain'",(mid,)).fetchall()
            if len(rows)!=1:raise ValueError('Unique uncertain input required')
            self.db.execute("UPDATE incoming SET state=?,reason='manual_review_no_retry' WHERE k=?",(resolution,rows[0][0]))
            if self.tasks and self.tasks[0]['key']==rows[0][0]:
                task=self.tasks.popleft();task['payload']=None;task['items']=None
            self.halted=bool(self.db.execute("SELECT 1 FROM incoming WHERE state='uncertain' LIMIT 1").fetchone())

    def close(self):
        if self.thread:self.thread.join(3)
        if self.thread and self.thread.is_alive():return
        with self.lock,self.db:
            self.db.execute("UPDATE incoming SET state='not_submitted_shutdown',reason='manual_resend_required' WHERE state IN ('queued','preparing','waiting')")
            self.tasks.clear()
        self.db.close()
