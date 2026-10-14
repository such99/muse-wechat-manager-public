"""No network/auth: restart cursor, queue ordering, lossless chunks and uncertainty."""
import json
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import os
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parent))
from feed_store import FeedStore,split_text
from main_feed import MainFeed
from message_policy import ChatFailure
from feed_bridge import FeedBridge
from ilink import ILinkAPIError
from journal import InstanceJournal
from test_feed_helpers import node_executable, FakeEngine, cookies, service_settings

def row(seq,text='正文',role='assistant',**kw):
    return {'seq':seq,'message_id':'native-'+str(seq),'text':text,'role':role,'main':True,'ready':True,
        'files':[],'unsupported':False,'status':'completed',**kw}

def test_tail_restart_and_no_web_user_echo(tmp_path):
    path=tmp_path/'feed.sqlite3';store=FeedStore(path);store.initialize('FAKE-OWNER',100)
    store.ingest([row(50,'旧历史'),row(101,'用户的网页输入',role='user'),row(102,'Muse 新回复')])
    assert store.get('cursor')==102 and store.head()['body']['text']=='Muse 新回复'
    h=store.head();store.transition(h['n'],'pending','sending','only-owner');store.transition(h['n'],'sending','sent','only-owner')
    store.close();store=FeedStore(path);store.initialize('FAKE-OWNER',200)
    store.ingest([row(102,'旧回复不应重发'),row(103,'下一条')]);assert store.get('cursor')==103
    assert store.head()['body']['text']=='下一条';store.close()

def test_long_text_more_than_eight_bubbles_stays_lossless_and_ordered(tmp_path):
    text='中文😀\n'*5000;parts=list(split_text(text));assert ''.join(parts)==text
    assert len(parts)>8 and all(len(p.encode('utf-8'))<=3500 for p in parts)
    s=FeedStore(tmp_path/'q');s.initialize('owner',0);s.ingest([row(1,text),row(2,'第二气泡')])
    got=[]
    while h:=s.head():
        got.append((h['message_id'],h['body']['text']));s.transition(h['n'],'pending','sending','owner');s.transition(h['n'],'sending','sent','owner')
    assert ''.join(t for mid,t in got if mid=='native-1')==text and got[-1]==('native-2','第二气泡')
    s.close()

def test_restart_mid_send_never_retries_or_advances_past_uncertain(tmp_path):
    p=tmp_path/'q';s=FeedStore(p);s.initialize('owner',0);s.ingest([row(1),row(2)])
    n=s.head()['n'];s.transition(n,'pending','sending','owner');s.close();s=FeedStore(p)
    assert s.head()['n']==n and s.head()['state']=='uncertain'
    with pytest.raises(ValueError):s.transition(n,'pending','sending','owner')
    assert s.status()['counts']['pending']==1;s.close()

def test_platform_rejection_waits_and_preserves_target_and_order(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0);s.ingest([row(1),row(2)])
    n=s.head()['n'];s.transition(n,'pending','sending','wechat-a');s.transition(n,'sending','waiting','wechat-a','wechat_requires_user_interaction')
    with pytest.raises(ValueError):s.transition(n,'waiting','pending','wechat-b')
    s.transition(n,'waiting','pending','wechat-a');assert s.head()['n']==n;s.close()

def test_cursor_never_advances_past_unfinished_reply(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0);s.ingest([row(1,ready=False),row(2)])
    assert s.get('cursor')==0 and s.head() is None
    s.ingest([row(1),row(2)]);assert s.get('cursor')==2;s.close()

def test_fallback_and_later_native_id_dedup_by_canonical_sequence(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0)
    s.ingest([row(1,message_id='fallback:1:100')]);assert s.head()['message_id'].startswith('fallback:1:100:')
    # A replay from an older cursor cannot create another action at the same seq.
    with s.db:s._set('cursor',0)
    s.ingest([row(1,message_id='real-id')]);assert s.status()['counts']['pending']==1;s.close()

def test_incoming_dedup_and_unresolved_submission_survive_restart(tmp_path):
    p=tmp_path/'q';s=FeedStore(p);assert s.claim_input('input-id') and s.unresolved_input();s.close();s=FeedStore(p)
    assert not s.claim_input('input-id') and s.unresolved_input();s.input_result('input-id','acknowledged');assert not s.unresolved_input();s.close()

def test_unknown_and_empty_content_yield_visible_notices(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0);s.ingest([row(1,'',unsupported=True)])
    assert '主要聊天' in s.head()['body']['text'];s.close()

def test_reviewed_media_batch_fixes_suffix_and_missing_image_once(tmp_path):
    s=FeedStore(tmp_path/'repair');s.initialize('owner',0)
    old_file={'path':'workspace/清单.docx','filename':'清单','mime':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    old=[row(1,'清单',files=[old_file]),row(2,'图片',unsupported=True)]
    s.ingest(old)
    while h:=s.head():
        s.transition(h['n'],'pending','sending','fixed-owner');s.transition(h['n'],'sending','sent','fixed-owner')
    fixed=[row(1,'清单',files=[{**old_file,'filename':'清单.docx'}]),
        row(2,'图片',files=[{'path':'workspace/清单.png','filename':'清单.png','mime':'image/png'}])]
    assert s.repair_reviewed_files(fixed,s.get('owner'),2)==2
    assert s.get('cursor')==2 and s.head()['target']=='fixed-owner'
    assert s.head()['body']['filename']=='清单.docx'
    assert s.repair_reviewed_files(fixed,s.get('owner'),2)==0
    repaired=list(s.db.execute("SELECT kind,reason FROM actions WHERE state='pending'"))
    assert [tuple(r) for r in repaired]==[('file','reviewed_filename_suffix'),('file','reviewed_missing_media')]
    s.close()

@pytest.mark.parametrize('fault',['owner','cursor','uncertain','unrelated_pending'])
def test_reviewed_file_repair_rejects_wrong_owner_or_unresolved_delivery(tmp_path,fault):
    s=FeedStore(tmp_path/'repair');s.initialize('owner',0)
    old={'path':'workspace/清单.txt','filename':'清单','mime':'text/plain'}
    s.ingest([row(1,'清单',files=[old])]);n=s.head()['n']
    s.transition(n,'pending','sending','fixed-owner')
    s.transition(n,'sending','uncertain' if fault=='uncertain' else 'sent','fixed-owner')
    if fault=='unrelated_pending':s.notice('unrelated','必须先按序处理')
    fixed=row(1,'清单',files=[{**old,'filename':'清单.txt'}])
    with pytest.raises(ValueError):
        s.repair_reviewed_files([fixed],'wrong' if fault=='owner' else s.get('owner'),
            999 if fault=='cursor' else 1)
    assert s.db.execute("SELECT count(*) FROM actions WHERE reason LIKE 'reviewed_%'").fetchone()[0]==0
    s.close()

def test_queue_scrubs_exact_credentials_before_chunking(tmp_path):
    p=tmp_path/'q';s=FeedStore(p);s.initialize('owner',0)
    secret='FAKE-RUNTIME-ONLY-SECRET';s.ingest([row(1,'x'*3490+secret+'z')],lambda text:text.replace(secret,'[credential omitted]'))
    s.close();assert secret.encode() not in p.read_bytes()

def test_owner_change_fails_closed(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('fixture',0)
    with pytest.raises(ValueError):s.initialize('other-person',5)
    assert s.get('cursor')==0;s.close()

def test_native_feed_javascript_contract():
    node=node_executable()
    r=subprocess.run([str(node),str(Path(__file__).with_name('test_feed_ui.cjs'))],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
    assert json.loads(r.stdout)['checks']==33


def test_observer_uses_short_cadence_only_while_busy_or_settling(tmp_path):
    feed=MainFeed(SimpleNamespace(),{'runtime':tmp_path},SimpleNamespace(chat=lambda *a,**k:None))
    feed.store.initialize('fake-owner',0)
    feed.enabled=True
    snapshot={'owner':'fake-owner','busy':True,'rows':[row(1,ready=False)]}
    feed.call=lambda *a,**kw:snapshot
    feed.poll();assert feed.poll_delay==.4 and feed.store.head() is None
    snapshot.update(busy=False,rows=[row(1,ready=False)])
    feed.poll();assert feed.poll_delay==.4 and feed.store.head() is None
    snapshot['rows']=[row(1)]
    feed.poll();assert feed.poll_delay==1.0 and feed.store.head()['body']['text']=='正文'
    snapshot['rows']=[]
    feed.poll();assert feed.poll_delay==1.0
    feed.close()


@pytest.mark.parametrize('fault',[None,'platform_limit','timeout','ack_write_lost','foreign_target'])
def test_sender_platform_resume_order_and_uncertain_no_retry(tmp_path,fault):
    store=FeedStore(tmp_path/'q');store.initialize('owner',0)
    store.ingest([row(i,'气泡'+str(i)) for i in range(1,16)])
    class Stop:
        step=0
        def wait(self,seconds):self.step+=1;return self.step>35
        def is_set(self):return False
    stop=Stop();attempts=[];accepted=[];failed=[False]
    def send(uid,text,context):
        attempts.append((uid,text,stop.step))
        if text=='气泡3' and not failed[0] and fault in ('platform_limit','timeout'):
            failed[0]=True
            if fault=='platform_limit':raise ILinkAPIError('sendmessage',ret=-2,errmsg='FAKE-MUST-NOT-LOG')
            raise requests.Timeout('FAKE-UNCONFIRMED')
        accepted.append(text)
    import requests
    state={'accepted_text_bubbles':0,'accepted_media_sends':0,'accepted_notices':0,'accepted_sends':0,'failed':0}
    client=SimpleNamespace(logged_in=True,send_text=send)
    binding=lambda:('only-owner','FAKE-CONTEXT','v3' if stop.step>=28 else 'v2' if stop.step>=15 else 'v1')
    bridge=FeedBridge({'muse_port':18611},'FAKE-API',client,
        InstanceJournal({'runtime':tmp_path,'instance':'fixture','logging':{'enabled':True,'chat_bodies':True}},'bridge',lambda:('FAKE-API','FAKE-CONTEXT')),stop,binding,state)
    def api(path,body=None,**kw):
        if path=='/feed':return store.status()
        action=body['action'];h=store.head()
        if fault=='ack_write_lost' and action=='sent' and h['message_id']=='native-3':raise requests.Timeout()
        transitions={'claim':('pending','sending'),'sent':('sending','sent'),'rejected':('sending','waiting'),'uncertain':('sending','uncertain'),'resume':('waiting','pending')}
        store.transition(body['n'],*transitions[action],body['target'],'wechat_requires_user_interaction' if action=='rejected' else None)
        return {'head':store.head(),'ok':True}
    bridge.api=api
    if fault=='foreign_target':
        n=store.head()['n'];store.transition(n,'pending','sending','other-person');store.transition(n,'sending','waiting','other-person')
    bridge.loop()
    if fault in (None,'platform_limit'):
        assert accepted==['气泡'+str(i) for i in range(1,16)] and store.head() is None
        if fault is None:assert [text for _,text,step in attempts if step<15]==['气泡'+str(i) for i in range(1,15)]
        if fault=='platform_limit':assert [step for _,text,step in attempts if text=='气泡3']==[3,16]
    elif fault in ('timeout','ack_write_lost'):
        assert len([a for a in attempts if a[1]=='气泡3'])==1
        assert not any(a[1]=='气泡4' for a in attempts)
        assert store.head()['state'] in ('sending','uncertain')
    else:assert not attempts
    bridge.close();store.close()
    saved=''.join(p.read_text(encoding='utf-8') for p in (tmp_path/'logs').glob('*.jsonl'))
    assert 'FAKE-CONTEXT' not in saved and 'FAKE-API' not in saved and 'FAKE-MUST-NOT-LOG' not in saved

def test_media_failure_keeps_original_metadata_and_sends_clear_notice(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0)
    s.ingest([row(1,'',files=[{'path':'workspace/example.txt','filename':'example.txt','mime':'text/plain','token':'FAKE-DO-NOT-PERSIST'}])])
    n=s.head()['n'];assert 'token' not in s.head()['body'];s.transition(n,'pending','sending','only-owner');s.media_notice(n,'only-owner')
    assert s.head()['state']=='pending' and s.head()['kind']=='text' and '主要聊天' in s.head()['body']['text']
    assert s.head()['body']['unavailable_file']['filename']=='example.txt';s.close()

def test_submit_ack_is_not_assistant_completion_and_duplicate_never_resubmits(tmp_path):
    native=SimpleNamespace();feed=MainFeed(native,{'runtime':tmp_path},SimpleNamespace(event=lambda *a,**k:None));feed.enabled=True
    feed.readiness=lambda:{'ready':True,'reason':None}
    calls=[]
    def call(method,*args,**kw):calls.append(method);return {'phase':'acknowledged','message_id':'fake-native-input'}
    feed.call=call
    r=feed.submit('a'*32,'当前原文',[],'source')
    assert r['assistant_completion_verified'] is False and r['phase']=='acknowledged'
    assert feed.submit('b'*32,'不得重试',[],'source')['phase']=='duplicate' and calls==['submit'];feed.close()

@pytest.mark.parametrize('reason,unresolved',[('main_chat_busy',False),('composer_has_user_draft',False),('native_operation_unconfirmed',True)])
def test_submit_preflight_vs_ambiguous_latch(tmp_path,reason,unresolved):
    feed=MainFeed(SimpleNamespace(),{'runtime':tmp_path},SimpleNamespace(event=lambda *a,**k:None));feed.enabled=True
    feed.readiness=lambda:{'ready':True,'reason':None}
    def call(*args,**kw):raise ChatFailure(reason,'假失败')
    feed.call=call
    with pytest.raises(ChatFailure):feed.submit('a'*32,'原文',[],'source')
    assert feed.submission_uncertain is unresolved and feed.store.unresolved_input() is unresolved;feed.close()

def test_reviewed_historical_recovery_does_not_rewind_cursor_or_duplicate(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',250)
    s.ingest([row(215,'经过核对的历史回复')],recover=True)
    s.ingest([row(215,'经过核对的历史回复')],recover=True)
    assert s.get('cursor')==250 and s.status()['counts']['pending']==1;s.close()


def test_reviewed_missing_file_repair_keeps_cursor_text_and_dedup(tmp_path):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0);s.ingest([row(1,'example.txt')])
    n=s.head()['n'];s.transition(n,'pending','sending','only-owner');s.transition(n,'sending','sent','only-owner')
    card=row(1,'example.txt',files=[{'path':'workspace/example.txt','filename':'example.txt','mime':'text/plain','secret':'FAKE-SECRET'}])
    assert s.recover_missing_files(card,s.get('owner'),1)==1
    assert s.get('cursor')==1 and s.head()['kind']=='file' and 'secret' not in s.head()['body']
    assert s.recover_missing_files(card,s.get('owner'),1)==0
    assert s.status()['counts']=={'pending':1,'sent':1};s.close()


@pytest.mark.parametrize('fault',['owner','cursor','text','role','unfinished','pending','invalid_path'])
def test_missing_file_repair_refuses_ambiguous_or_foreign_state(tmp_path,fault):
    s=FeedStore(tmp_path/'q');s.initialize('owner',0);s.ingest([row(1,'example.txt')])
    if fault!='pending':
        n=s.head()['n'];s.transition(n,'pending','sending','owner');s.transition(n,'sending','sent','owner')
    card=row(1,'example.txt',files=[{'path':'workspace/example.txt','filename':'example.txt','mime':'text/plain'}])
    owner=s.get('owner');cursor=1
    if fault=='owner':owner='foreign-owner'
    if fault=='cursor':cursor=2
    if fault=='text':card['text']='different card'
    if fault=='role':card['role']='user'
    if fault=='unfinished':card['ready']=False
    if fault=='invalid_path':card['files'][0]['path']='https://fake.invalid/?token=FAKE'
    with pytest.raises((ValueError,ChatFailure)):s.recover_missing_files(card,owner,cursor)
    assert s.db.execute("SELECT count(*) FROM actions WHERE kind='file'").fetchone()[0]==0;s.close()


def test_live_store_open_does_not_change_inflight_actions(tmp_path):
    path=tmp_path/'q';s=FeedStore(path);s.initialize('owner',0);s.ingest([row(1)])
    s.transition(s.head()['n'],'pending','sending','owner')
    reader=FeedStore(path,recover_interrupted=False)
    assert reader.head()['state']=='sending';reader.close();s.close()

def test_authenticated_api_separates_submission_and_outbox_and_requires_manual_resolution(tmp_path,monkeypatch):
    from muse_service import create_service
    settings=service_settings(tmp_path)
    monkeypatch.delenv('MUSE_RETAINED_BRIDGE', raising=False)
    monkeypatch.delenv('MUSE_FIXTURE_COOKIES_JSON', raising=False)
    monkeypatch.setenv('MUSE_FIXTURE_API_KEY','FAKE-FEED-API-KEY')
    app=create_service(settings,FakeEngine());app.state.account.inject(cookies('FAKE-FEED-COOKIE'));app.state.status['owner_confirmed']=True
    feed=app.state.feed;feed.enabled=True;feed.store.initialize('owner',0)
    feed.readiness=lambda:{'ready':True,'reason':None}
    feed.call=lambda method,*a,**k:({'phase':'acknowledged','message_id':'native-input'} if method=='submit' else {'owner':'owner','rows':[]})
    auth={'Authorization':'Bearer FAKE-FEED-API-KEY'}
    body={'model':'muse-spark','messages':[{'role':'user','content':'仅此一条'}]}
    headers={**auth,'X-Source-ID':'a'*64,'X-Request-ID':'b'*32}
    with TestClient(app,base_url='http://127.0.0.1:18611') as client:
        assert client.get('/v1/muse/feed').status_code==401
        assert client.get('/v1/muse/ready').status_code==401
        assert client.get('/v1/muse/ready',headers=auth).json()=={'ready':True,'reason':None}
        assert client.post('/v1/muse/submit',headers=headers,json=body).json()['assistant_completion_verified'] is False
        assert app.state.status['api_requests_completed']==0
        assert client.post('/v1/muse/submit',headers=headers,json=body).json()['phase']=='duplicate'
        feed.store.ingest([row(1,'原生回复')]);n=feed.store.head()['n']
        assert client.post('/v1/muse/outbox',headers=auth,json={'n':n,'target':'wechat-owner','action':'claim'}).status_code==200
        assert client.post('/v1/muse/outbox',headers=auth,json={'n':n,'target':'other-owner','action':'sent'}).status_code==409
        client.post('/v1/muse/outbox',headers=auth,json={'n':n,'target':'wechat-owner','action':'uncertain'})
        local={'Origin':'http://127.0.0.1:18611','X-Local-Action':'1'}
        resolve={'n':n,'resolution':'cancelled','confirm_checked_phone_and_muse':True}
        assert client.post('/local/resolve-outbox',json=resolve).status_code==403
        assert client.post('/local/resolve-outbox',headers=local,json=resolve).status_code==200
        assert feed.store.head() is None
        app.state.account.invalidate()
        assert client.post('/v1/muse/submit',headers={**headers,'X-Source-ID':'c'*64},json=body).status_code==401
        assert client.get('/v1/muse/feed',headers=auth).json()['muse_authenticated'] is False
        assert client.post('/v1/muse/notice',headers=auth,json={'text':'认证失效，请重新认证','message_id':'d'*64}).status_code==200
        n=feed.store.head()['n']
        assert client.post('/v1/muse/outbox',headers=auth,json={'n':n,'target':'wechat-owner','action':'claim'}).status_code==200
        assert client.post('/v1/muse/outbox',headers=auth,json={'n':n,'target':'wechat-owner','action':'sent'}).status_code==200
        status=client.get('/local/status').text
        assert 'FAKE-FEED-COOKIE' not in status and 'FAKE-FEED-API-KEY' not in status

def test_unfinished_ignored_placeholder_does_not_skip_later_reply(tmp_path):
    store=FeedStore(tmp_path/'q');store.initialize('fake-owner',0)
    placeholder=row(1,'',main=False,ready=False,ignore_reason='empty_placeholder')
    store.ingest([placeholder,row(2,'later reply')])
    assert store.get('cursor')==0 and store.head() is None
    placeholder['ready']=True
    store.ingest([placeholder,row(2,'later reply')])
    assert store.get('cursor')==2 and store.head()['message_id']=='native-2'
    assert store.status()['counts']=={'pending':1}
    store.close()
