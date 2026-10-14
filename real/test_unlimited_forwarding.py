"""Offline sender checks using fake delivery responses; no real sends."""
import base64
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests
from feed_bridge import FeedBridge
from feed_store import FeedStore
from ilink import ILinkAPIError


def row(n, media=False):
    return {'seq':n,'message_id':f'native-{n}','text':f'body-{n}',
        'role':'assistant','main':True,'ready':True,'unsupported':False,
        'status':'completed','files':([{'path':f'workspace/sample{n}.txt',
            'filename':f'sample{n}.txt','mime':'text/plain'}] if media else [])}


class SenderTests(unittest.TestCase):
    def run_sender(self, *, count=125, media=False, fault=None, fresh_at=None,
                   initial_count=0, context='FAKE-CONTEXT'):
        temporary = tempfile.TemporaryDirectory(prefix='unlimited-test-')
        self.addCleanup(temporary.cleanup)
        store = FeedStore(Path(temporary.name)/'queue.sqlite3')
        self.addCleanup(store.close)
        store.initialize('FAKE-OWNER',0)
        store.ingest([row(n,media) for n in range(1,count+1)])
        class Stop:
            step=0
            def wait(self,seconds):
                self.step+=1
                return self.step>500
            def is_set(self):return False
        stop=Stop()
        attempts=[]
        events=[]
        raised=False
        def send(uid,body,reply_context,**kwargs):
            nonlocal raised
            attempts.append((uid,body,reply_context,stop.step))
            if body=='body-11' and not raised and fault in ('rejected','timeout'):
                raised=True
                if fault=='rejected':
                    raise ILinkAPIError('sendmessage',ret=-2,errmsg='PRIVATE-RAW-ERROR')
                raise requests.Timeout('PRIVATE-RAW-ERROR')
        client=SimpleNamespace(logged_in=True,send_text=send,
            send_file=lambda uid,raw,ctx,file_name:send(uid,file_name,ctx))
        state={'accepted_text_bubbles':0,'accepted_media_sends':0,
            'accepted_notices':0,'accepted_sends':0,'failed':0}
        journal=SimpleNamespace(chat=lambda *a,**k:None,media=lambda *a,**k:None,
            event=lambda name,**kwargs:events.append((name,kwargs)))
        binding=lambda:('FAKE-OWNER',context,'v2' if fresh_at and stop.step>=fresh_at else 'v1')
        bridge=FeedBridge({'muse_port':1},'FAKE-KEY',client,journal,stop,binding,state)
        self.addCleanup(bridge.close)
        bridge.context_version='v1'
        bridge.context_count=initial_count
        def api(path,body=None,**kwargs):
            if path=='/feed':return store.status()
            action=body['action']
            head=store.head()
            if fault=='ack_lost' and action=='sent' and head['message_id']=='native-11':
                raise requests.Timeout('PRIVATE-RAW-ERROR')
            transitions={'claim':('pending','sending'),'sent':('sending','sent'),
                'rejected':('sending','waiting'),'uncertain':('sending','uncertain'),
                'resume':('waiting','pending')}
            store.transition(body['n'],*transitions[action],body['target'],
                'wechat_requires_user_interaction' if action=='rejected' else None)
            head=store.head()
            if action=='claim' and head['kind']=='file':
                head['attachment']={'filename':head['body']['filename'],'mime':'text/plain',
                    'category':'file','data_base64':base64.b64encode(b'fake').decode()}
            return {'head':head,'ok':True}
        bridge.api=api
        if fault=='foreign_target':
            n=store.head()['n']
            store.transition(n,'pending','sending','OTHER-OWNER')
            store.transition(n,'sending','waiting','OTHER-OWNER')
        bridge.loop()
        self.assertNotIn('PRIVATE-RAW-ERROR',repr(events))
        return bridge,store,state,attempts,events

    def test_125_text_messages_same_context_in_order(self):
        bridge,store,state,attempts,_=self.run_sender()
        self.assertEqual([a[1] for a in attempts],[f'body-{n}' for n in range(1,126)])
        self.assertTrue(all(a[:1]==('FAKE-OWNER',) and a[2]=='FAKE-CONTEXT' for a in attempts))
        self.assertIsNone(store.head())
        self.assertEqual(state['accepted_sends'],125)
        self.assertIsNone(state['application_send_limit'])
        self.assertEqual(bridge.context_count,125)

    def test_existing_count_above_100_does_not_pause(self):
        _,store,state,attempts,_=self.run_sender(count=3,initial_count=1000)
        self.assertEqual(len(attempts),3)
        self.assertIsNone(store.head())

    def test_130_interleaved_text_and_files_same_context(self):
        _,store,state,attempts,_=self.run_sender(count=65,media=True)
        self.assertEqual([a[1] for a in attempts],
            [body for n in range(1,66) for body in (f'body-{n}',f'sample{n}.txt')])
        self.assertIsNone(store.head())
        self.assertEqual((state['accepted_text_bubbles'],state['accepted_media_sends']),(65,65))

    def test_actual_rejection_stops_at_11_without_new_inbound(self):
        _,store,state,attempts,events=self.run_sender(fault='rejected')
        self.assertEqual([a[1] for a in attempts],[f'body-{n}' for n in range(1,12)])
        self.assertEqual(store.head()['state'],'waiting')
        self.assertEqual(store.head()['message_id'],'native-11')
        self.assertEqual(state['queue_waiting_reason'],'wechat_requires_user_interaction')
        self.assertEqual(len([e for e in events if e[0]=='wechat.send_rejected']),1)

    def test_actual_rejection_resumes_once_only_after_new_inbound(self):
        _,store,state,attempts,_=self.run_sender(fault='rejected',fresh_at=150)
        self.assertEqual([a[3] for a in attempts if a[1]=='body-11'],[11,151])
        self.assertIsNone(store.head())
        self.assertEqual(state['accepted_sends'],125)

    def test_timeout_after_10_never_retries_or_advances(self):
        _,store,state,attempts,_=self.run_sender(fault='timeout')
        self.assertEqual(sum(a[1]=='body-11' for a in attempts),1)
        self.assertFalse(any(a[1]=='body-12' for a in attempts))
        self.assertEqual(store.head()['state'],'uncertain')
        self.assertEqual(state['accepted_notices'],1)

    def test_lost_ack_after_10_never_retries_or_advances(self):
        _,store,state,attempts,_=self.run_sender(fault='ack_lost')
        self.assertEqual(sum(a[1]=='body-11' for a in attempts),1)
        self.assertFalse(any(a[1]=='body-12' for a in attempts))
        self.assertEqual(store.head()['state'],'uncertain')

    def test_foreign_recipient_stays_blocked(self):
        _,store,state,attempts,_=self.run_sender(fault='foreign_target')
        self.assertFalse(attempts)
        self.assertEqual(state['queue_waiting_reason'],'target_changed_manual_review_required')

    def test_initial_context_requirement_is_explicit(self):
        _,store,state,attempts,_=self.run_sender(context=None)
        self.assertFalse(attempts)
        self.assertEqual(state['queue_waiting_reason'],'wechat_reply_context_required')
        self.assertEqual(store.head()['state'],'pending')


if __name__=='__main__':unittest.main(verbosity=2)
